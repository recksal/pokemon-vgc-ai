"""Capture turn-by-turn diagnostics for selected forced-preview matchup cells.

Unlike the aggregate preview matrix, this runner saves the actual choices submitted by
both policies, each side's fogged Showdown protocol, and the VGC decision trace after
every decision. It is intentionally diagnostic: use small n to understand WHY a cell is
strong or weak, then use the matrix runner for larger confirmation samples.
"""

from __future__ import annotations

import argparse
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

from offline.analyze_preview_matrix import A_QUICK, B_QUICK, ForcedPlan, _make_forced  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.decision_trace import get_last_trace  # noqa: E402
from vgc.rl.env import DirectBattle, SIDES, SimWorker  # noqa: E402
from vgc.rl.match import MAX_DECISIONS  # noqa: E402


SCENARIOS: dict[str, tuple[tuple[int, int], ...]] = {
    # The key contrast from the first 320-game matrix: same Recksal plan, radically
    # different outcomes depending on Yamazaki's lead/bring plan.
    "contrast": ((0, 0), (0, 1)),
    # Isolate the effect of committing the same four to Mega Kang vs Mega Maw.
    "mega": ((0, 1), (1, 1)),
    # Hold our strongest tested structure fixed against all four tested opponent plans.
    "best-plan": ((0, 0), (0, 1), (0, 2), (0, 3)),
    # Inspect both tested sun structures against all four opponent structures.
    "sun": tuple((a, b) for a in (2, 3) for b in range(4)),
}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return _jsonable(asdict(value))
    return str(value)


def _trace_snapshot() -> dict[str, Any] | None:
    trace = get_last_trace()
    if trace is None:
        return None
    return {
        "turn": trace.turn,
        "chosen_order": trace.chosen_order,
        "fallback_used": trace.fallback_used,
        "fallback_reason": trace.fallback_reason,
        "notes": _jsonable(trace.notes),
    }


def _battle_snapshot(battle, side: str) -> dict[str, Any]:
    view = battle.battles[side]

    def mons(items) -> list[dict[str, Any] | None]:
        out: list[dict[str, Any] | None] = []
        for mon in list(items or ()):
            if mon is None:
                out.append(None)
                continue
            out.append(
                {
                    "species": str(getattr(mon, "species", "")),
                    "current_hp": getattr(mon, "current_hp", None),
                    "max_hp": getattr(mon, "max_hp", None),
                    "fainted": bool(getattr(mon, "fainted", False)),
                    "status": str(getattr(mon, "status", None) or ""),
                    "boosts": _jsonable(getattr(mon, "boosts", {}) or {}),
                }
            )
        return out

    return {
        "turn": int(getattr(view, "turn", 0) or 0),
        "own_active": mons(getattr(view, "active_pokemon", ())),
        "opponent_active": mons(getattr(view, "opponent_active_pokemon", ())),
        "weather": [str(x) for x in getattr(view, "weather", {})],
        "fields": [str(x) for x in getattr(view, "fields", {})],
        "side_conditions": [str(x) for x in getattr(view, "side_conditions", {})],
        "opponent_side_conditions": [
            str(x) for x in getattr(view, "opponent_side_conditions", {})
        ],
    }


def _events(lines: list[str]) -> list[str]:
    """Keep state-carrying battle events while dropping request/chat noise."""
    interesting = {
        "move",
        "switch",
        "drag",
        "detailschange",
        "-mega",
        "-formechange",
        "-damage",
        "-heal",
        "-status",
        "-curestatus",
        "-boost",
        "-unboost",
        "-weather",
        "-fieldstart",
        "-fieldend",
        "-sidestart",
        "-sideend",
        "-ability",
        "-item",
        "-enditem",
        "faint",
        "turn",
        "win",
        "tie",
    }
    kept = []
    for line in lines:
        parts = line.split("|")
        if len(parts) > 1 and parts[1] in interesting:
            kept.append(line)
    return kept


def play_diagnostic(
    worker: SimWorker,
    battle_id: str,
    team_a: str,
    team_b: str,
    a_plan: ForcedPlan,
    b_plan: ForcedPlan,
    *,
    a_side: str,
    battle_format: str,
    seed: list[int],
) -> dict[str, Any]:
    b_side = "p2" if a_side == "p1" else "p1"
    a_agent = _make_forced(team_a, battle_format, a_plan, "team-a")
    b_agent = _make_forced(team_b, battle_format, b_plan, "team-b")
    agents = {a_side: a_agent, b_side: b_agent}
    teams = {a_side: team_a, b_side: team_b}

    battle = DirectBattle.start(
        worker,
        battle_id,
        teams["p1"],
        teams["p2"],
        battle_format=battle_format,
        seed=seed,
    )
    decision_count = 0
    steps: list[dict[str, Any]] = []
    try:
        for side in SIDES:
            agents[side].observe(battle_id, battle.last_lines[side])

        # Save the opening team-preview request/protocol separately.
        opening = {
            "phase": "opening",
            "p1_protocol": list(battle.last_lines["p1"]),
            "p2_protocol": list(battle.last_lines["p2"]),
        }

        while not battle.ended:
            to_move = battle.sides_to_move()
            for side in to_move:
                battle.battles[side]._vgc_direct_root = battle
                battle.battles[side]._vgc_direct_side = side

            choices: dict[str, str] = {}
            traces: dict[str, Any] = {}
            before = {side: _battle_snapshot(battle, side) for side in SIDES}
            for side in to_move:
                choices[side] = agents[side].choose(battle.battles[side])
                # Forced team preview bypasses VgcPlayer's trace wrapper; mark it explicitly.
                traces[side] = None if battle.battles[side].teampreview else _trace_snapshot()

            decision_count += len(choices)
            result = battle.step(choices)
            for side in SIDES:
                agents[side].observe(battle_id, result.lines[side])

            steps.append(
                {
                    "decision_index": len(steps),
                    "turn_before": before["p1"]["turn"],
                    "sides_to_move": list(to_move),
                    "choices": dict(choices),
                    "traces": traces,
                    "before": before,
                    # p1/p2 protocol is intentionally preserved separately: each is a
                    # fogged legal player view, not omniscient simulator state.
                    "p1_protocol": list(result.lines["p1"]),
                    "p2_protocol": list(result.lines["p2"]),
                    "events": _events(result.lines[a_side]),
                    "ended": result.ended,
                    "winner_side": result.winner,
                }
            )
            if decision_count > MAX_DECISIONS:
                raise RuntimeError(f"battle {battle_id} exceeded {MAX_DECISIONS} decisions")

        for side in SIDES:
            agents[side].finish(battle.battles[side])
        winner_agent = (
            "team-a" if battle.winner == a_side else "team-b" if battle.winner == b_side else None
        )
        return {
            "battle_id": battle_id,
            "seed": seed,
            "a_side": a_side,
            "winner_side": battle.winner,
            "winner_agent": winner_agent,
            "turns": int(getattr(battle.battles["p1"], "turn", 0) or 0),
            "decisions": decision_count,
            "opening": opening,
            "steps": steps,
            "fallbacks": {
                "team-a": int(getattr(a_agent.player, "fallback_count", 0) or 0),
                "team-b": int(getattr(b_agent.player, "fallback_count", 0) or 0),
            },
        }
    finally:
        battle.close()


def _summary(games: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(games)
    wins = sum(g["winner_agent"] == "team-a" for g in games)
    return {
        "games": n,
        "a_wins": wins,
        "b_wins": sum(g["winner_agent"] == "team-b" for g in games),
        "draws": sum(g["winner_agent"] is None for g in games),
        "a_win_rate": wins / n if n else 0.0,
        "mean_turns": sum(g["turns"] for g in games) / n if n else 0.0,
        "fallbacks": {
            "team-a": sum(g["fallbacks"]["team-a"] for g in games),
            "team-b": sum(g["fallbacks"]["team-b"] for g in games),
        },
    }


def run_diagnostics(
    team_a: str,
    team_b: str,
    *,
    scenario: str,
    games_per_cell: int,
    seed: int,
    battle_format: str,
) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    selected = SCENARIOS[scenario]
    rng = random.Random(seed)
    paired_seeds = [[rng.randrange(1, 2**31) for _ in range(4)] for _ in range(games_per_cell)]
    cells: list[dict[str, Any]] = []

    with SimWorker() as worker:
        for cell_index, (a_idx, b_idx) in enumerate(selected):
            a_plan, b_plan = A_QUICK[a_idx], B_QUICK[b_idx]
            games = []
            for game_index, battle_seed in enumerate(paired_seeds):
                a_side = "p1" if game_index % 2 == 0 else "p2"
                games.append(
                    play_diagnostic(
                        worker,
                        f"diag-{scenario}-{cell_index}-{game_index}",
                        team_a,
                        team_b,
                        a_plan,
                        b_plan,
                        a_side=a_side,
                        battle_format=battle_format,
                        seed=battle_seed,
                    )
                )
            cells.append(
                {
                    "cell_index": cell_index,
                    "a_plan": asdict(a_plan),
                    "b_plan": asdict(b_plan),
                    "summary": _summary(games),
                    "games": games,
                }
            )

    return {
        "format": battle_format,
        "scenario": scenario,
        "games_per_cell": games_per_cell,
        "seed": seed,
        "paired_seed_stream": paired_seeds,
        "timestamp": datetime.now(UTC).isoformat(),
        "cells": cells,
    }


def print_report(result: dict[str, Any]) -> None:
    print(
        f"Diagnostic scenario {result['scenario']}: {len(result['cells'])} cells x "
        f"{result['games_per_cell']} games"
    )
    for cell in result["cells"]:
        s = cell["summary"]
        print(
            f"  {cell['a_plan']['name']}  VS  {cell['b_plan']['name']}: "
            f"{s['a_wins']}/{s['games']} ({s['a_win_rate']:.1%}), "
            f"mean {s['mean_turns']:.2f} turns"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--scenario", choices=tuple(SCENARIOS), default="contrast")
    parser.add_argument("--games-per-cell", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.games_per_cell <= 0:
        raise SystemExit("--games-per-cell must be > 0")

    result = run_diagnostics(
        args.team_a.read_text().strip(),
        args.team_b.read_text().strip(),
        scenario=args.scenario,
        games_per_cell=args.games_per_cell,
        seed=args.seed,
        battle_format=args.format,
    )
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / (
        f"diagnostic_{args.scenario}_{args.games_per_cell}_seed{args.seed}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
