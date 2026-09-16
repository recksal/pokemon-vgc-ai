"""Screen all Team A bring-4/lead plans against one fixed opponent opening.

Stage 1 gives every generated Team A preview plan a small paired-seed screen. Stage 2
retests only the strongest screen plans on a fresh simulator-seed stream. This is a
matchup-lab diagnostic: preview is forced, while all in-battle choices use the normal
VGC policy and exact Showdown simulator.

The screen and confirmation streams are deliberately disjoint so the same games are not
used both to select and to confirm a plan.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from offline.analyze_preview_matrix import ForcedPlan, _make_forced, _team_species  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.rl.env import SimWorker  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402


# Yamazaki replica slots:
# 1 Dragonite, 2 Floette-Eternal, 3 Basculegion, 4 Sneasler, 5 Kingambit, 6 Garchomp.
OPPONENT_OPENINGS: dict[str, ForcedPlan] = {
    "sneas-chomp": ForcedPlan(
        "Sneas+Chomp | Dragonite+Basc | Mega Dragonite",
        "4613",
        "dragonite",
        "Sneasler + Garchomp lead; Dragonite + Basculegion back.",
    ),
    "chomp-dnite": ForcedPlan(
        "Chomp+Dragonite | Basc+Sneas | Mega Dragonite",
        "6134",
        "dragonite",
        "Garchomp + Dragonite lead; Basculegion + Sneasler back.",
    ),
}


def _default_mega_variants(picked_slots: tuple[int, int, int, int]) -> tuple[str | None, ...]:
    """Return sensible Team A preview Mega preferences for this bring-4.

    Team A slots are 3 Kangaskhan and 6 Mawile. If both are brought, preserve both
    strategic possibilities as separate preview plans. If only one is brought, use it
    as the default Mega preference. If neither is brought, there is no Mega preference.
    """
    has_kang = 3 in picked_slots
    has_maw = 6 in picked_slots
    if has_kang and has_maw:
        return ("kangaskhan", "mawile")
    if has_kang:
        return ("kangaskhan",)
    if has_maw:
        return ("mawile",)
    return (None,)


def generate_a_plans(team: str) -> list[ForcedPlan]:
    species = _team_species(team)
    plans: list[ForcedPlan] = []
    for picked in itertools.combinations(range(1, 7), 4):
        for lead in itertools.permutations(picked, 2):
            back = tuple(slot for slot in picked if slot not in lead)
            # Back order is canonicalized. The strategic variables being screened are
            # bring-4, ordered lead slots, and Mega preference; arbitrary bench display
            # ordering should not multiply the search space.
            order_slots = lead + tuple(sorted(back))
            order = "".join(str(x) for x in order_slots)
            lead_names = "+".join(species[x - 1] for x in lead)
            back_names = "+".join(species[x - 1] for x in sorted(back))
            for mega in _default_mega_variants(tuple(picked)):
                mega_label = mega or "none"
                plans.append(
                    ForcedPlan(
                        f"{lead_names} | {back_names} | Mega {mega_label}",
                        order,
                        mega,
                        "Generated exhaustive bring-4/ordered-lead screen plan.",
                    )
                )
    return plans


def _run_plan(
    worker: SimWorker,
    team_a: str,
    team_b: str,
    a_plan: ForcedPlan,
    b_plan: ForcedPlan,
    seeds: list[list[int]],
    *,
    battle_format: str,
    label: str,
) -> dict[str, Any]:
    games: list[dict[str, Any]] = []
    for game_index, battle_seed in enumerate(seeds):
        a_side = "p1" if game_index % 2 == 0 else "p2"
        b_side = "p2" if a_side == "p1" else "p1"
        a_agent = _make_forced(team_a, battle_format, a_plan, "team-a")
        b_agent = _make_forced(team_b, battle_format, b_plan, "team-b")
        outcome = play_battle(
            worker,
            f"preview-screen-{label}-{game_index}",
            {a_side: a_agent, b_side: b_agent},
            {a_side: team_a, b_side: team_b},
            battle_format=battle_format,
            seed=battle_seed,
        )
        winner_agent = "team-a" if outcome.winner == a_side else "team-b" if outcome.winner == b_side else None
        games.append(
            {
                "index": game_index,
                "seed": battle_seed,
                "a_side": a_side,
                "winner_agent": winner_agent,
                "turns": outcome.turns,
                "a_fallbacks": int(getattr(a_agent.player, "fallback_count", 0) or 0),
                "b_fallbacks": int(getattr(b_agent.player, "fallback_count", 0) or 0),
            }
        )
    n = len(games)
    wins = sum(g["winner_agent"] == "team-a" for g in games)
    losses = sum(g["winner_agent"] == "team-b" for g in games)
    lo, hi = wilson_interval(wins, n)
    return {
        "plan": asdict(a_plan),
        "summary": {
            "games": n,
            "a_wins": wins,
            "b_wins": losses,
            "draws": n - wins - losses,
            "a_win_rate": wins / n if n else 0.0,
            "a_wilson_95": [lo, hi],
            "mean_turns": sum(g["turns"] for g in games) / n if n else 0.0,
            "fallbacks": {
                "team-a": sum(g["a_fallbacks"] for g in games),
                "team-b": sum(g["b_fallbacks"] for g in games),
            },
        },
        "games": games,
    }


def _rank_key(row: dict[str, Any]) -> tuple[float, float, str]:
    s = row["summary"]
    return (-float(s["a_win_rate"]), -float(s["a_wilson_95"][0]), row["plan"]["name"])


def run_screen(
    team_a: str,
    team_b: str,
    *,
    opponent_opening: str,
    screen_games: int,
    confirm_top: int,
    confirm_games: int,
    seed: int,
    battle_format: str,
) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    a_plans = generate_a_plans(team_a)
    b_plan = OPPONENT_OPENINGS[opponent_opening]
    rng = random.Random(seed)
    screen_seeds = [[rng.randrange(1, 2**31) for _ in range(4)] for _ in range(screen_games)]
    # Fresh held-out stream. Do not reuse screen seeds for confirmation.
    confirm_seeds = [[rng.randrange(1, 2**31) for _ in range(4)] for _ in range(confirm_games)]

    screen_rows: list[dict[str, Any]] = []
    confirm_rows: list[dict[str, Any]] = []
    with SimWorker() as worker:
        for i, plan in enumerate(a_plans):
            screen_rows.append(
                _run_plan(
                    worker, team_a, team_b, plan, b_plan, screen_seeds,
                    battle_format=battle_format, label=f"screen-{i}",
                )
            )
        screen_rows.sort(key=_rank_key)
        finalists = screen_rows[: min(confirm_top, len(screen_rows))]
        for i, row in enumerate(finalists):
            plan = ForcedPlan(**row["plan"])
            confirm_rows.append(
                _run_plan(
                    worker, team_a, team_b, plan, b_plan, confirm_seeds,
                    battle_format=battle_format, label=f"confirm-{i}",
                )
            )
        confirm_rows.sort(key=_rank_key)

    return {
        "format": battle_format,
        "timestamp": datetime.now(UTC).isoformat(),
        "seed": seed,
        "opponent_opening_key": opponent_opening,
        "opponent_plan": asdict(b_plan),
        "generated_plan_count": len(a_plans),
        "screen_games_per_plan": screen_games,
        "confirm_top": min(confirm_top, len(a_plans)),
        "confirm_games_per_plan": confirm_games,
        "screen_seed_stream": screen_seeds,
        "confirm_seed_stream": confirm_seeds,
        "screen": screen_rows,
        "confirmation": confirm_rows,
    }


def print_report(result: dict[str, Any]) -> None:
    print(
        f"Preview lead screen: {result['generated_plan_count']} plans vs "
        f"{result['opponent_plan']['name']}"
    )
    print(
        f"Screen: {result['screen_games_per_plan']} games/plan; held-out confirmation: "
        f"top {result['confirm_top']} x {result['confirm_games_per_plan']} games"
    )
    for row in result["confirmation"][:12]:
        s = row["summary"]
        print(f"  {s['a_wins']:>3}/{s['games']:<3} {s['a_win_rate']:.1%}  {row['plan']['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--opponent-opening", choices=tuple(OPPONENT_OPENINGS), required=True)
    parser.add_argument("--screen-games", type=int, default=2)
    parser.add_argument("--confirm-top", type=int, default=12)
    parser.add_argument("--confirm-games", type=int, default=20)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.screen_games <= 0 or args.confirm_top <= 0 or args.confirm_games <= 0:
        raise SystemExit("screen/confirmation counts must all be > 0")

    result = run_screen(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        opponent_opening=args.opponent_opening,
        screen_games=args.screen_games,
        confirm_top=args.confirm_top,
        confirm_games=args.confirm_games,
        seed=args.seed,
        battle_format=args.format,
    )
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / f"preview_lead_screen_{args.opponent_opening}_seed{args.seed}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
