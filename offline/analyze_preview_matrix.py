"""Explore forced team-preview plans across an exact two-team Champions matchup.

This is a matchup-lab tool, not a replacement for the policy's normal team preview.
It deliberately forces curated bring-4/lead plans while keeping the normal in-battle
`vgc` policy and exact direct Showdown environment.

Every matrix cell reuses the same simulator-seed stream and alternates seats, making
plan-vs-plan comparisons substantially less noisy.
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

from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.damage import to_id  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.own_team import apply_own_spreads  # noqa: E402
from vgc.rl.agents import DirectAgent, make_direct_agent  # noqa: E402
from vgc.rl.env import SimWorker, choice_string  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402
from vgc.team_preview import PreviewPlan  # noqa: E402


@dataclass(frozen=True)
class ForcedPlan:
    name: str
    order: str
    default_mega: str | None
    note: str = ""


A_QUICK = (
    ForcedPlan(
        "Kang+Farig | Tork+Maw | Mega Kang",
        "3416",
        "kangaskhan",
        "Original policy-selected line from the 100-game baseline.",
    ),
    ForcedPlan(
        "Kang+Farig | Tork+Maw | Mega Maw",
        "3416",
        "mawile",
        "Same four and leads, but commits the preview plan toward Mega Mawile.",
    ),
    ForcedPlan(
        "Tork+Vile | Farig+Maw | Mega Maw",
        "1246",
        "mawile",
        "Sun lead with Trick Room and physical endgame in back.",
    ),
    ForcedPlan(
        "Tork+Vile | Typh+Farig | no Mega",
        "1254",
        None,
        "Fast sun mode with Scarf Typhlosion plus a Trick Room fallback.",
    ),
)

A_FULL = A_QUICK + (
    ForcedPlan(
        "Kang+Maw | Farig+Tork | Mega Maw",
        "3641",
        "mawile",
        "Immediate physical pressure with Trick Room plus Torkoal in back.",
    ),
    ForcedPlan(
        "Kang+Farig | Tork+Typh | Mega Kang",
        "3415",
        "kangaskhan",
        "Original Trick Room lead but replaces Mawile with Scarf Typhlosion.",
    ),
)

B_QUICK = (
    ForcedPlan(
        "Sneas+Chomp | Dragonite+Basc | Mega Dragonite",
        "4613",
        "dragonite",
        "Original policy-selected line from the 100-game baseline.",
    ),
    ForcedPlan(
        "Basc+Sneas | Dragonite+Chomp | Mega Dragonite",
        "3416",
        "dragonite",
        "Priority-heavy Basculegion/Sneasler lead.",
    ),
    ForcedPlan(
        "Floette+Basc | Sneas+Chomp | Mega Floette",
        "2346",
        "floetteeternal",
        "Mega Floette offense with Basculegion lead pressure.",
    ),
    ForcedPlan(
        "King+Sneas | Basc+Chomp | no Mega",
        "5436",
        None,
        "Slow priority attacker plus Sneasler; no Mega brought.",
    ),
)

B_FULL = B_QUICK + (
    ForcedPlan(
        "Dragonite+Sneas | Basc+Chomp | Mega Dragonite",
        "1436",
        "dragonite",
        "Mega Dragonite lead paired with Sneasler support.",
    ),
    ForcedPlan(
        "Floette+Chomp | Basc+Sneas | Mega Floette",
        "2634",
        "floetteeternal",
        "Mega Floette plus Scarf Garchomp with priority-heavy backline.",
    ),
)


def _parse_order(order: str) -> tuple[int, int, int, int]:
    digits = tuple(int(ch) for ch in order)
    if len(digits) != 4 or len(set(digits)) != 4 or any(i < 1 or i > 6 for i in digits):
        raise ValueError(f"invalid bring-4 order {order!r}; expected four unique digits 1..6")
    return digits  # type: ignore[return-value]


def _team_species(team: str) -> list[str]:
    species: list[str] = []
    for entry in team.split("]"):
        if not entry:
            continue
        first = entry.split("|", 1)[0]
        species.append(first)
    if len(species) != 6:
        raise ValueError(f"expected 6 packed Pokemon, found {len(species)}")
    return species


def _plan_species(plan: ForcedPlan, species: list[str]) -> tuple[list[str], list[str]]:
    idx = _parse_order(plan.order)
    picked = [species[i - 1] for i in idx]
    return picked[:2], picked


class ForcedPreviewAgent(DirectAgent):
    """Force preview order while preserving the evaluator's preview-plan state."""

    def __init__(self, base: DirectAgent, plan: ForcedPlan) -> None:
        super().__init__(base.player, name=base.name, preview_order=None)
        self.plan = plan

    def choose(self, battle) -> str:
        config = getattr(self.player, "config", None)
        if config is not None and getattr(config, "use_own_team_spreads", False):
            apply_own_spreads(battle)

        if not battle.teampreview:
            return choice_string(self.player.choose_move(battle))

        order = _parse_order(self.plan.order)
        team = list(battle.team.values())
        if len(team) < 6:
            raise RuntimeError(f"forced preview needs 6 Pokemon, battle has {len(team)}")

        for mon in team:
            mon._selected_in_teampreview = False
        for i in order:
            team[i - 1]._selected_in_teampreview = True

        picked_species = tuple(to_id(team[i - 1].species) for i in order)
        battle._vgc_preview_plan = PreviewPlan(
            our_closer_species=None,
            opponent_closer_species=None,
            default_mega_species=self.plan.default_mega,
            opponent_engines=(),
            picked_species=picked_species,
            lead_species=picked_species[:2],
            lead_functions=(),
            speed_modes=(),
            balanced_structure={},
            lead_covers_engine=False,
            back_has_second_speed_mode=False,
            predicted_opponent_leads=None,
        )
        return choice_string(f"team {self.plan.order}")


def _make_forced(team: str, battle_format: str, plan: ForcedPlan, name: str) -> ForcedPreviewAgent:
    agent = ForcedPreviewAgent(
        make_direct_agent("vgc", team, battle_format=battle_format),
        plan,
    )
    agent.name = name
    return agent


def _cell_summary(games: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(games)
    wins = sum(g["winner_agent"] == "team-a" for g in games)
    losses = sum(g["winner_agent"] == "team-b" for g in games)
    low, high = wilson_interval(wins, n)
    return {
        "games": n,
        "a_wins": wins,
        "b_wins": losses,
        "draws": n - wins - losses,
        "a_win_rate": wins / n if n else 0.0,
        "a_wilson_95": [low, high],
        "mean_turns": sum(g["turns"] for g in games) / n if n else 0.0,
        "fallbacks": {
            "team-a": sum(g["a_fallbacks"] for g in games),
            "team-b": sum(g["b_fallbacks"] for g in games),
        },
    }


def _aggregate(cells: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for cell in cells:
        name = cell[key]["name"]
        buckets.setdefault(name, []).extend(cell["game_records"])
    rows = []
    for name, games in buckets.items():
        rows.append({"name": name, **_cell_summary(games)})
    rows.sort(key=lambda r: (-r["a_win_rate"], -r["games"], r["name"]))
    return rows


def run_matrix(
    team_a: str,
    team_b: str,
    *,
    preset: str,
    games_per_cell: int,
    seed: int,
    battle_format: str,
) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    a_plans = A_QUICK if preset == "quick" else A_FULL
    b_plans = B_QUICK if preset == "quick" else B_FULL
    a_species = _team_species(team_a)
    b_species = _team_species(team_b)

    rng = random.Random(seed)
    paired_seeds = [[rng.randrange(1, 2**31) for _ in range(4)] for _ in range(games_per_cell)]
    cells: list[dict[str, Any]] = []

    with SimWorker() as worker:
        cell_index = 0
        for a_plan in a_plans:
            for b_plan in b_plans:
                game_records: list[dict[str, Any]] = []
                for game_index, battle_seed in enumerate(paired_seeds):
                    a_side = "p1" if game_index % 2 == 0 else "p2"
                    b_side = "p2" if a_side == "p1" else "p1"
                    a_agent = _make_forced(team_a, battle_format, a_plan, "team-a")
                    b_agent = _make_forced(team_b, battle_format, b_plan, "team-b")
                    outcome = play_battle(
                        worker,
                        f"matrix-{cell_index}-{game_index}",
                        {a_side: a_agent, b_side: b_agent},
                        {a_side: team_a, b_side: team_b},
                        battle_format=battle_format,
                        seed=battle_seed,
                    )
                    winner_agent = (
                        "team-a"
                        if outcome.winner == a_side
                        else "team-b"
                        if outcome.winner == b_side
                        else None
                    )
                    game_records.append(
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

                a_leads, a_picked = _plan_species(a_plan, a_species)
                b_leads, b_picked = _plan_species(b_plan, b_species)
                cells.append(
                    {
                        "cell_index": cell_index,
                        "a_plan": {**asdict(a_plan), "leads": a_leads, "picked": a_picked},
                        "b_plan": {**asdict(b_plan), "leads": b_leads, "picked": b_picked},
                        "summary": _cell_summary(game_records),
                        "game_records": game_records,
                    }
                )
                cell_index += 1

    overall_games = [game for cell in cells for game in cell["game_records"]]
    return {
        "format": battle_format,
        "preset": preset,
        "games_per_cell": games_per_cell,
        "seed": seed,
        "paired_seed_stream": paired_seeds,
        "timestamp": datetime.now(UTC).isoformat(),
        "team_a_species": a_species,
        "team_b_species": b_species,
        "overall": _cell_summary(overall_games),
        "by_a_plan": _aggregate(cells, "a_plan"),
        "by_b_plan": _aggregate(cells, "b_plan"),
        "cells": cells,
    }


def print_report(result: dict[str, Any]) -> None:
    print(
        f"Preview matrix: {len(result['cells'])} cells x {result['games_per_cell']} games "
        f"= {result['overall']['games']} battles"
    )
    print("\nOur plans, averaged over tested opponent plans:")
    for row in result["by_a_plan"]:
        print(
            f"  {row['name']:<48} {row['a_wins']:>3}/{row['games']:<3} "
            f"{row['a_win_rate']:.1%}"
        )
    print("\nBest individual cells:")
    ranked = sorted(
        result["cells"],
        key=lambda c: (-c["summary"]["a_win_rate"], c["a_plan"]["name"], c["b_plan"]["name"]),
    )
    for cell in ranked[:10]:
        s = cell["summary"]
        print(
            f"  {cell['a_plan']['name']}  VS  {cell['b_plan']['name']}: "
            f"{s['a_wins']}/{s['games']} ({s['a_win_rate']:.1%})"
        )
    fallbacks = result["overall"]["fallbacks"]
    if any(fallbacks.values()):
        print(f"\nWARNING: policy fallbacks occurred: {fallbacks}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--preset", choices=("quick", "full"), default="quick")
    parser.add_argument("--games-per-cell", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.games_per_cell <= 0:
        raise SystemExit("--games-per-cell must be > 0")

    result = run_matrix(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        preset=args.preset,
        games_per_cell=args.games_per_cell,
        seed=args.seed,
        battle_format=args.format,
    )
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / (
        f"preview_matrix_{args.preset}_{args.games_per_cell}_seed{args.seed}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
