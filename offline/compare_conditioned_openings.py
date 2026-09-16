"""Compare fixed Team A turn-1 openings against diversified opponent branches.

This matchup-lab runner fixes Team A to one candidate opening on turn 1, then lets
both sides diversify among plausible scored legal joint orders for the remaining
opening horizon. Every candidate opening reuses the exact same trajectory policy
seeds, Showdown battle seeds, and seat assignments, so comparisons are paired.

The default cell is Basculegion + Sneasler versus Kangaskhan + Farigiraf and the
candidate lines focus on the Trick Room openings surfaced by the trajectory explorer.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from offline.analyze_preview_matrix import A_QUICK, B_QUICK, ForcedPlan, _make_forced  # noqa: E402
from offline.explore_opening_trajectories import DiverseOpeningAgent  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.rl.env import SimWorker, choice_string  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402


@dataclass(frozen=True)
class ConditionedLine:
    name: str
    choice: str | None
    note: str


LINES = (
    ConditionedLine(
        "policy",
        None,
        "No forced Team A turn 1; diversified policy chooses normally.",
    ),
    ConditionedLine(
        "FO Basc + TR",
        "move fakeout 1, move trickroom",
        "Scrappy Kangaskhan Fake Out into Basculegion while Farigiraf sets Trick Room.",
    ),
    ConditionedLine(
        "FO Sneas + TR",
        "move fakeout 2, move trickroom",
        "Scrappy Kangaskhan Fake Out into Sneasler while Farigiraf sets Trick Room.",
    ),
    ConditionedLine(
        "Mega FO Sneas + TR",
        "move fakeout mega 2, move trickroom",
        "Mega Kangaskhan Fake Out into Sneasler while Farigiraf sets Trick Room.",
    ),
    ConditionedLine(
        "FO Basc + Psychic Noise Sneas",
        "move fakeout 1, move psychicnoise 2",
        "Reference line from the earlier deterministic diagnostic.",
    ),
)


class ConditionedOpeningAgent(DiverseOpeningAgent):
    """Force Team A's first in-battle order, then diversify subsequent turns."""

    def __init__(self, *args, forced_turn1: str | None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.forced_turn1 = forced_turn1
        self._forced_used = False

    def choose(self, battle) -> str:
        if battle.teampreview:
            return super().choose(battle)
        turn = int(getattr(battle, "turn", 0) or 0)
        if self.forced_turn1 is not None and not self._forced_used and turn == 1:
            self._forced_used = True
            self.sampled_history.append(
                {
                    "turn": 1,
                    "rank": 0,
                    "choice": self.forced_turn1,
                    "score": None,
                    "best_score": None,
                    "score_gap": None,
                    "candidate_count": 0,
                    "qualified_count": 0,
                    "forced": True,
                    "candidates": [],
                }
            )
            return choice_string(self.forced_turn1)
        self._forced_used = True
        return super().choose(battle)


def _make_conditioned(
    team: str,
    battle_format: str,
    plan: ForcedPlan,
    name: str,
    *,
    line: ConditionedLine,
    policy_seed: int,
    diversify_turns: int,
    score_gap: float,
    max_candidates: int,
    temperature: float,
) -> ConditionedOpeningAgent:
    base = _make_forced(team, battle_format, plan, name)
    agent = ConditionedOpeningAgent(
        base,
        plan,
        forced_turn1=line.choice,
        policy_seed=policy_seed,
        diversify_turns=diversify_turns,
        score_gap=score_gap,
        max_candidates=max_candidates,
        temperature=temperature,
    )
    agent.name = name
    return agent


def _make_diverse_b(
    team: str,
    battle_format: str,
    plan: ForcedPlan,
    name: str,
    *,
    policy_seed: int,
    diversify_turns: int,
    score_gap: float,
    max_candidates: int,
    temperature: float,
) -> DiverseOpeningAgent:
    base = _make_forced(team, battle_format, plan, name)
    agent = DiverseOpeningAgent(
        base,
        plan,
        policy_seed=policy_seed,
        diversify_turns=diversify_turns,
        score_gap=score_gap,
        max_candidates=max_candidates,
        temperature=temperature,
    )
    agent.name = name
    return agent


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    wins = sum(r["winner_agent"] == "team-a" for r in records)
    losses = sum(r["winner_agent"] == "team-b" for r in records)
    lo, hi = wilson_interval(wins, n)
    return {
        "games": n,
        "a_wins": wins,
        "b_wins": losses,
        "draws": n - wins - losses,
        "a_win_rate": wins / n if n else 0.0,
        "a_wilson_95": [lo, hi],
        "mean_turns": sum(r["turns"] for r in records) / n if n else 0.0,
        "fallbacks": {
            "team-a": sum(r["a_fallbacks"] for r in records),
            "team-b": sum(r["b_fallbacks"] for r in records),
        },
    }


def run_comparison(
    team_a: str,
    team_b: str,
    *,
    trajectories: int,
    repeats: int,
    diversify_turns: int,
    score_gap: float,
    max_candidates: int,
    temperature: float,
    seed: int,
    battle_format: str,
) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    a_plan = A_QUICK[0]
    b_plan = B_QUICK[1]  # Basculegion + Sneasler | Dragonite + Garchomp
    master = random.Random(seed)

    specs = [
        {
            "trajectory_id": i,
            "policy_seed_a": master.randrange(1, 2**31),
            "policy_seed_b": master.randrange(1, 2**31),
            "battle_seeds": [[master.randrange(1, 2**31) for _ in range(4)] for _ in range(repeats)],
        }
        for i in range(trajectories)
    ]

    line_rows: list[dict[str, Any]] = []
    with SimWorker() as worker:
        for line_index, line in enumerate(LINES):
            records: list[dict[str, Any]] = []
            for spec in specs:
                for repeat_index, battle_seed in enumerate(spec["battle_seeds"]):
                    global_index = spec["trajectory_id"] * repeats + repeat_index
                    a_side = "p1" if global_index % 2 == 0 else "p2"
                    b_side = "p2" if a_side == "p1" else "p1"
                    a_agent = _make_conditioned(
                        team_a,
                        battle_format,
                        a_plan,
                        "team-a",
                        line=line,
                        policy_seed=spec["policy_seed_a"],
                        diversify_turns=diversify_turns,
                        score_gap=score_gap,
                        max_candidates=max_candidates,
                        temperature=temperature,
                    )
                    b_agent = _make_diverse_b(
                        team_b,
                        battle_format,
                        b_plan,
                        "team-b",
                        policy_seed=spec["policy_seed_b"],
                        diversify_turns=diversify_turns,
                        score_gap=score_gap,
                        max_candidates=max_candidates,
                        temperature=temperature,
                    )
                    outcome = play_battle(
                        worker,
                        f"conditioned-{line_index}-{spec['trajectory_id']}-{repeat_index}",
                        {a_side: a_agent, b_side: b_agent},
                        {a_side: team_a, b_side: team_b},
                        battle_format=battle_format,
                        seed=battle_seed,
                    )
                    winner_agent = (
                        "team-a" if outcome.winner == a_side else "team-b" if outcome.winner == b_side else None
                    )
                    records.append(
                        {
                            "trajectory_id": spec["trajectory_id"],
                            "repeat_index": repeat_index,
                            "battle_seed": battle_seed,
                            "policy_seed_a": spec["policy_seed_a"],
                            "policy_seed_b": spec["policy_seed_b"],
                            "a_side": a_side,
                            "winner_agent": winner_agent,
                            "winner_side": outcome.winner,
                            "turns": outcome.turns,
                            "decisions": outcome.decisions,
                            "a_fallbacks": int(getattr(a_agent.player, "fallback_count", 0) or 0),
                            "b_fallbacks": int(getattr(b_agent.player, "fallback_count", 0) or 0),
                            "a_opening": a_agent.sampled_history,
                            "b_opening": b_agent.sampled_history,
                        }
                    )

            b_first = Counter(
                r["b_opening"][0]["choice"] for r in records if r["b_opening"]
            )
            line_rows.append(
                {
                    "line": asdict(line),
                    "summary": _summary(records),
                    "b_first_turn_frequencies": b_first.most_common(),
                    "games": records,
                }
            )

    # Paired trajectory comparison: mean Team A win rate across each line on identical seeds.
    for row in line_rows:
        per_traj = []
        for trajectory_id in range(trajectories):
            games = [g for g in row["games"] if g["trajectory_id"] == trajectory_id]
            per_traj.append(
                {
                    "trajectory_id": trajectory_id,
                    "a_win_rate": sum(g["winner_agent"] == "team-a" for g in games) / len(games),
                }
            )
        row["trajectory_results"] = per_traj

    line_rows.sort(key=lambda r: (-r["summary"]["a_win_rate"], r["line"]["name"]))
    return {
        "format": battle_format,
        "cell": "basc-sneas",
        "seed": seed,
        "trajectories": trajectories,
        "repeats": repeats,
        "diversify_turns": diversify_turns,
        "score_gap": score_gap,
        "max_candidates": max_candidates,
        "temperature": temperature,
        "timestamp": datetime.now(UTC).isoformat(),
        "a_plan": asdict(a_plan),
        "b_plan": asdict(b_plan),
        "paired": True,
        "lines": line_rows,
    }


def print_report(result: dict[str, Any]) -> None:
    print(
        f"Conditioned opening comparison: {len(result['lines'])} lines x "
        f"{result['trajectories']} trajectories x {result['repeats']} repeats"
    )
    print(
        f"Opponent diversification: turns 1-{result['diversify_turns']} | "
        f"score gap {result['score_gap']} | cap {result['max_candidates']} | "
        f"temperature {result['temperature']}"
    )
    for row in result["lines"]:
        s = row["summary"]
        lo, hi = s["a_wilson_95"]
        print(
            f"  {row['line']['name']:<32} {s['a_wins']:>4}/{s['games']:<4} "
            f"{s['a_win_rate']:.1%} (95% {lo:.1%}..{hi:.1%})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--trajectories", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--diversify-turns", type=int, default=5)
    parser.add_argument("--score-gap", type=float, default=100.0)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=35.0)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.trajectories <= 0 or args.repeats <= 0 or args.diversify_turns <= 0:
        raise SystemExit("trajectory, repeat, and turn counts must all be > 0")
    if args.score_gap < 0 or args.max_candidates <= 0:
        raise SystemExit("score gap must be >= 0 and max candidates must be > 0")

    result = run_comparison(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        trajectories=args.trajectories,
        repeats=args.repeats,
        diversify_turns=args.diversify_turns,
        score_gap=args.score_gap,
        max_candidates=args.max_candidates,
        temperature=args.temperature,
        seed=args.seed,
        battle_format=args.format,
    )
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / (
        f"conditioned_openings_{args.trajectories}x{args.repeats}_seed{args.seed}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
