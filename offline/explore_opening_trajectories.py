"""Explore many plausible early-game trajectories instead of repeating one line.

For a fixed forced-preview matchup cell, each side samples among plausible scored legal
joint orders for the first N turns. Plausibility is defined by evaluator score distance
from the current best action, not by a fixed top-k rank. A trajectory seed fixes that
policy sampling; each trajectory is then replayed several times with different Showdown
simulator seeds. This spends an evaluation budget on breadth first (many different
plausible openings) and uses a small number of repeats to measure RNG sensitivity inside
each opening family.

After the diversification horizon, both sides return to the normal VGC policy.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from poke_env.battle.double_battle import DoubleBattle

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from offline.analyze_preview_matrix import A_QUICK, B_QUICK, ForcedPlan, ForcedPreviewAgent, _make_forced  # noqa: E402
from vgc.actions import describe_order  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.own_team import apply_own_spreads  # noqa: E402
from vgc.rl.env import SimWorker, choice_string  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402
from vgc.search import search_joint_orders  # noqa: E402


CELLS: dict[str, tuple[int, int]] = {
    "sneas-chomp": (0, 0),
    "basc-sneas": (0, 1),
    "floette-basc": (0, 2),
    "king-sneas": (0, 3),
}


class DiverseOpeningAgent(ForcedPreviewAgent):
    """Sample plausible scored actions for early turns, then use normal policy."""

    def __init__(
        self,
        base,
        plan: ForcedPlan,
        *,
        policy_seed: int,
        diversify_turns: int,
        score_gap: float,
        max_candidates: int,
        temperature: float,
    ) -> None:
        super().__init__(base, plan)
        self._rng = random.Random(policy_seed)
        self.diversify_turns = diversify_turns
        self.score_gap = score_gap
        self.max_candidates = max_candidates
        self.temperature = temperature
        self.sampled_history: list[dict[str, Any]] = []

    def _sample_scored(self, battle: DoubleBattle):
        config = self.player.config
        memory = self.player._memory_for(battle)
        scored = search_joint_orders(battle, config)
        if not scored:
            return None

        best = float(scored[0].score)
        cutoff = best - self.score_gap
        plausible = [row for row in scored if float(row.score) >= cutoff]
        pool = plausible[: max(1, min(self.max_candidates, len(plausible)))]
        if not pool:
            pool = [scored[0]]

        # Temperature is in evaluator score units. A low temperature hugs the policy
        # argmax; a higher one explores more of the plausible score-gap neighborhood.
        if self.temperature <= 0:
            chosen_index = 0
        else:
            weights = [math.exp((float(row.score) - best) / self.temperature) for row in pool]
            chosen_index = self._rng.choices(range(len(pool)), weights=weights, k=1)[0]
        chosen = pool[chosen_index]
        label = describe_order(chosen.order)
        memory.record_choice(int(getattr(battle, "turn", 0) or 0), label)
        self.sampled_history.append(
            {
                "turn": int(getattr(battle, "turn", 0) or 0),
                "rank": chosen_index + 1,
                "choice": label,
                "score": round(float(chosen.score), 3),
                "best_score": round(best, 3),
                "score_gap_from_best": round(best - float(chosen.score), 3),
                "score_cutoff": round(cutoff, 3),
                "eligible_before_cap": len(plausible),
                "candidate_count": len(pool),
                "candidate_cap": self.max_candidates,
                "candidates": [
                    {
                        "rank": i + 1,
                        "choice": describe_order(row.order),
                        "score": round(float(row.score), 3),
                        "gap": round(best - float(row.score), 3),
                    }
                    for i, row in enumerate(pool)
                ],
            }
        )
        return chosen.order

    def choose(self, battle) -> str:
        if battle.teampreview:
            return super().choose(battle)

        config = getattr(self.player, "config", None)
        if config is not None and getattr(config, "use_own_team_spreads", False):
            apply_own_spreads(battle)

        turn = int(getattr(battle, "turn", 0) or 0)
        if turn <= self.diversify_turns and isinstance(battle, DoubleBattle):
            order = self._sample_scored(battle)
            if order is not None:
                return choice_string(order)

        return choice_string(self.player.choose_move(battle))


def _make_diverse(
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


def run_explorer(
    team_a: str,
    team_b: str,
    *,
    cell: str,
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
    a_idx, b_idx = CELLS[cell]
    a_plan, b_plan = A_QUICK[a_idx], B_QUICK[b_idx]
    master = random.Random(seed)

    trajectory_specs = [
        {
            "trajectory_id": i,
            "policy_seed_a": master.randrange(1, 2**31),
            "policy_seed_b": master.randrange(1, 2**31),
            "battle_seeds": [
                [master.randrange(1, 2**31) for _ in range(4)] for _ in range(repeats)
            ],
        }
        for i in range(trajectories)
    ]

    records: list[dict[str, Any]] = []
    with SimWorker() as worker:
        for spec in trajectory_specs:
            for repeat_index, battle_seed in enumerate(spec["battle_seeds"]):
                # Alternate seats across the total stream, not just within one trajectory.
                global_index = spec["trajectory_id"] * repeats + repeat_index
                a_side = "p1" if global_index % 2 == 0 else "p2"
                b_side = "p2" if a_side == "p1" else "p1"
                a_agent = _make_diverse(
                    team_a,
                    battle_format,
                    a_plan,
                    "team-a",
                    policy_seed=spec["policy_seed_a"],
                    diversify_turns=diversify_turns,
                    score_gap=score_gap,
                    max_candidates=max_candidates,
                    temperature=temperature,
                )
                b_agent = _make_diverse(
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
                    f"traj-{cell}-{spec['trajectory_id']}-{repeat_index}",
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

    grouped: list[dict[str, Any]] = []
    for trajectory_id in range(trajectories):
        group = [r for r in records if r["trajectory_id"] == trajectory_id]
        grouped.append(
            {
                "trajectory_id": trajectory_id,
                "policy_seed_a": trajectory_specs[trajectory_id]["policy_seed_a"],
                "policy_seed_b": trajectory_specs[trajectory_id]["policy_seed_b"],
                "summary": _summary(group),
                # First repeat is a compact representative of the sampled branch.
                "representative_a_opening": group[0]["a_opening"] if group else [],
                "representative_b_opening": group[0]["b_opening"] if group else [],
            }
        )

    # Surface which early choices occur often and whether breadth actually materialized.
    a_first = Counter(r["a_opening"][0]["choice"] for r in records if r["a_opening"])
    b_first = Counter(r["b_opening"][0]["choice"] for r in records if r["b_opening"])
    unique_signatures = {
        (
            tuple(step["choice"] for step in r["a_opening"]),
            tuple(step["choice"] for step in r["b_opening"]),
        )
        for r in records
    }

    return {
        "format": battle_format,
        "cell": cell,
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
        "overall": _summary(records),
        "unique_opening_signatures": len(unique_signatures),
        "a_first_turn_frequencies": a_first.most_common(),
        "b_first_turn_frequencies": b_first.most_common(),
        "trajectory_summaries": grouped,
        "games": records,
    }


def print_report(result: dict[str, Any]) -> None:
    s = result["overall"]
    print(
        f"Opening trajectory explorer: {result['trajectories']} trajectories x "
        f"{result['repeats']} repeats = {s['games']} games"
    )
    print(
        f"Cell: {result['cell']} | diversify turns 1-{result['diversify_turns']} | "
        f"score gap {result['score_gap']} | cap {result['max_candidates']} | "
        f"temperature {result['temperature']}"
    )
    print(
        f"Team A: {s['a_wins']}/{s['games']} ({s['a_win_rate']:.1%}); "
        f"unique opening signatures: {result['unique_opening_signatures']}"
    )
    print("Team A turn-1 choices:")
    for choice, count in result["a_first_turn_frequencies"][:10]:
        print(f"  {count:>4}  {choice}")
    print("Team B turn-1 choices:")
    for choice, count in result["b_first_turn_frequencies"][:10]:
        print(f"  {count:>4}  {choice}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--cell", choices=tuple(CELLS), default="basc-sneas")
    parser.add_argument("--trajectories", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--diversify-turns", type=int, default=5)
    parser.add_argument("--score-gap", type=float, default=100.0)
    parser.add_argument("--max-candidates", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=35.0)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if (
        args.trajectories <= 0
        or args.repeats <= 0
        or args.diversify_turns <= 0
        or args.max_candidates <= 0
        or args.score_gap < 0
    ):
        raise SystemExit(
            "trajectory, repeat, turn, and candidate counts must be > 0; score gap must be >= 0"
        )

    result = run_explorer(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        cell=args.cell,
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
        f"opening_trajectories_{args.cell}_{args.trajectories}x{args.repeats}_seed{args.seed}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
