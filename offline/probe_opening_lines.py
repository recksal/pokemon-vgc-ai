"""Compare selected turn-1 lines while leaving later play to the normal VGC policy.

This is a matchup-lab diagnostic. Team preview is forced to a known cell, then Team A's
first in-battle choice is optionally forced. From the following request onward both sides
use the normal VGC policy. Every line reuses the same Showdown seed stream and alternates
seats, so opening-line comparisons are paired.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from offline.analyze_preview_matrix import A_QUICK, B_QUICK, ForcedPreviewAgent, _make_forced  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.rl.env import SimWorker, choice_string  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402


@dataclass(frozen=True)
class OpeningLine:
    name: str
    choice: str | None
    note: str


LINES = (
    OpeningLine(
        "policy",
        None,
        "No override: normal VGC policy chooses turn 1.",
    ),
    OpeningLine(
        "FO Sneas + TR",
        "move fakeout 1, move trickroom",
        "Fake Out opposing slot 1 (Sneasler in this probe) and set Trick Room; preserve Mega.",
    ),
    OpeningLine(
        "Mega FO Sneas + TR",
        "move fakeout mega 1, move trickroom",
        "Mega Kangaskhan immediately, Fake Out Sneasler, and set Trick Room.",
    ),
    OpeningLine(
        "FO Chomp + TR",
        "move fakeout 2, move trickroom",
        "Fake Out opposing slot 2 (Garchomp) and set Trick Room; tests guaranteed TR versus Sneasler pressure.",
    ),
    OpeningLine(
        "Mega Ice Punch Chomp + TR",
        "move icepunch mega 2, move trickroom",
        "Attack Garchomp while setting Trick Room; this was a searched alternative in diagnostics.",
    ),
)


class ForcedOpeningAgent(ForcedPreviewAgent):
    """Force one first in-battle choice, then return control to the normal policy."""

    def __init__(self, base, plan, opening_choice: str | None) -> None:
        super().__init__(base, plan)
        self.opening_choice = opening_choice
        self._opening_used = False

    def choose(self, battle) -> str:
        if battle.teampreview:
            return super().choose(battle)
        if self.opening_choice is not None and not self._opening_used:
            self._opening_used = True
            return choice_string(self.opening_choice)
        self._opening_used = True
        return choice_string(self.player.choose_move(battle))


def _make_a(team: str, battle_format: str, line: OpeningLine):
    base = _make_forced(team, battle_format, A_QUICK[0], "team-a")
    agent = ForcedOpeningAgent(base, A_QUICK[0], line.choice)
    agent.name = "team-a"
    return agent


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    wins = sum(r["winner_agent"] == "team-a" for r in records)
    losses = sum(r["winner_agent"] == "team-b" for r in records)
    low, high = wilson_interval(wins, n)
    return {
        "games": n,
        "a_wins": wins,
        "b_wins": losses,
        "draws": n - wins - losses,
        "a_win_rate": wins / n if n else 0.0,
        "a_wilson_95": [low, high],
        "mean_turns": sum(r["turns"] for r in records) / n if n else 0.0,
        "fallbacks": {
            "team-a": sum(r["a_fallbacks"] for r in records),
            "team-b": sum(r["b_fallbacks"] for r in records),
        },
    }


def run_probe(
    team_a: str,
    team_b: str,
    *,
    games_per_line: int,
    seed: int,
    battle_format: str,
) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    # Problem cell from the diagnostic run: Kang/Farig -> Tork/Maw versus Sneas/Chomp
    # -> Dragonite/Basculegion.
    a_plan = A_QUICK[0]
    b_plan = B_QUICK[0]

    rng = random.Random(seed)
    paired_seeds = [[rng.randrange(1, 2**31) for _ in range(4)] for _ in range(games_per_line)]
    rows: list[dict[str, Any]] = []

    with SimWorker() as worker:
        for line_index, line in enumerate(LINES):
            records: list[dict[str, Any]] = []
            for game_index, battle_seed in enumerate(paired_seeds):
                a_side = "p1" if game_index % 2 == 0 else "p2"
                b_side = "p2" if a_side == "p1" else "p1"
                a_agent = _make_a(team_a, battle_format, line)
                b_agent = _make_forced(team_b, battle_format, b_plan, "team-b")
                outcome = play_battle(
                    worker,
                    f"opening-{line_index}-{game_index}",
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
                        "index": game_index,
                        "seed": battle_seed,
                        "a_side": a_side,
                        "winner_agent": winner_agent,
                        "winner_side": outcome.winner,
                        "turns": outcome.turns,
                        "decisions": outcome.decisions,
                        "a_fallbacks": int(getattr(a_agent.player, "fallback_count", 0) or 0),
                        "b_fallbacks": int(getattr(b_agent.player, "fallback_count", 0) or 0),
                    }
                )
            rows.append({"line": asdict(line), "summary": _summary(records), "games": records})

    rows.sort(key=lambda row: (-row["summary"]["a_win_rate"], row["line"]["name"]))
    return {
        "format": battle_format,
        "seed": seed,
        "games_per_line": games_per_line,
        "paired_seed_stream": paired_seeds,
        "timestamp": datetime.now(UTC).isoformat(),
        "a_plan": asdict(a_plan),
        "b_plan": asdict(b_plan),
        "lines": rows,
    }


def print_report(result: dict[str, Any]) -> None:
    print(
        f"Opening probe: {len(result['lines'])} lines x {result['games_per_line']} games "
        f"against {result['b_plan']['name']}"
    )
    for row in result["lines"]:
        s = row["summary"]
        lo, hi = s["a_wilson_95"]
        print(
            f"  {row['line']['name']:<30} {s['a_wins']:>3}/{s['games']:<3} "
            f"{s['a_win_rate']:.1%} (95% {lo:.1%}..{hi:.1%})"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--games-per-line", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.games_per_line <= 0:
        raise SystemExit("--games-per-line must be > 0")

    result = run_probe(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        games_per_line=args.games_per_line,
        seed=args.seed,
        battle_format=args.format,
    )
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / f"opening_probe_{args.games_per_line}_seed{args.seed}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
