"""Run an exact two-team Champions matchup on the direct Showdown environment."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from vgc.config import FORMAT_ID, RUNS_DIR  # noqa: E402
from vgc.evaluation import wilson_interval  # noqa: E402
from vgc.rl.agents import make_direct_agent  # noqa: E402
from vgc.rl.env import SimWorker  # noqa: E402
from vgc.rl.match import play_battle  # noqa: E402


def _preview_from_history(history: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    if not history:
        return None
    for entry in history:
        if not isinstance(entry, dict):
            continue
        notes = entry.get("notes")
        if isinstance(notes, dict) and isinstance(notes.get("team_preview_choice"), dict):
            return notes["team_preview_choice"]
    return None


def _preview_summary(agent) -> dict[str, Any]:
    player = getattr(agent, "player", None)
    preview = _preview_from_history(getattr(player, "decision_trace_history", None))
    out: dict[str, Any] = {"fallback_count": int(getattr(player, "fallback_count", 0) or 0)}
    if preview is None:
        out["preview_trace_missing"] = True
        return out
    plan = preview.get("plan")
    if isinstance(plan, dict):
        out["leads"] = list(plan.get("lead_species") or [])
        out["picked"] = list(plan.get("picked_species") or [])
        out["default_mega"] = plan.get("default_mega")
        out["predicted_opponent_leads"] = list(plan.get("predicted_opponent_leads") or [])
    out["preview_score"] = preview.get("score")
    out["order"] = preview.get("order")
    return out


def _species_key(items: list[str] | None) -> str:
    return " + ".join(items) if items else "(unknown)"


def _grouped_rate(records: list[dict[str, Any]], field: str, winner_name: str) -> list[dict[str, Any]]:
    groups: dict[str, list[bool]] = defaultdict(list)
    for record in records:
        groups[record.get(field, "(unknown)")].append(record["winner_agent"] == winner_name)
    rows = []
    for key, values in groups.items():
        wins = sum(values)
        n = len(values)
        low, high = wilson_interval(wins, n)
        rows.append({"key": key, "games": n, "wins": wins, "win_rate": wins / n, "wilson_95": [low, high]})
    rows.sort(key=lambda row: (-row["games"], -row["win_rate"], row["key"]))
    return rows


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    games = len(records)
    a_wins = sum(record["winner_agent"] == "team-a" for record in records)
    b_wins = sum(record["winner_agent"] == "team-b" for record in records)
    low, high = wilson_interval(a_wins, games)
    return {
        "games": games,
        "a_wins": a_wins,
        "b_wins": b_wins,
        "draws": games - a_wins - b_wins,
        "a_win_rate": a_wins / games,
        "a_wilson_95": [low, high],
        "mean_turns": sum(record["turns"] for record in records) / games,
        "a_leads": _grouped_rate(records, "a_lead", "team-a"),
        "b_leads_from_a_perspective": _grouped_rate(records, "b_lead", "team-a"),
        "a_picks": _grouped_rate(records, "a_pick", "team-a"),
        "fallbacks": {
            "team-a": sum(record["a_preview"].get("fallback_count", 0) for record in records),
            "team-b": sum(record["b_preview"].get("fallback_count", 0) for record in records),
        },
    }


def run_matchup(team_a: str, team_b: str, games: int, seed: int, battle_format: str) -> dict[str, Any]:
    os.environ.setdefault("VGC_TRACE", "1")
    rng = random.Random(seed)
    records: list[dict[str, Any]] = []

    with SimWorker() as worker:
        for index in range(games):
            a_side = "p1" if index % 2 == 0 else "p2"
            b_side = "p2" if a_side == "p1" else "p1"
            a_agent = make_direct_agent("vgc", team_a, battle_format=battle_format)
            b_agent = make_direct_agent("vgc", team_b, battle_format=battle_format)
            a_agent.name = "team-a"
            b_agent.name = "team-b"
            agents = {a_side: a_agent, b_side: b_agent}
            teams = {a_side: team_a, b_side: team_b}
            battle_seed = [rng.randrange(1, 2**31) for _ in range(4)]
            outcome = play_battle(worker, f"matchup-{index}", agents, teams, battle_format=battle_format, seed=battle_seed)
            a_preview = _preview_summary(a_agent)
            b_preview = _preview_summary(b_agent)
            winner_agent = "team-a" if outcome.winner == a_side else "team-b" if outcome.winner == b_side else None
            records.append({
                "index": index,
                "seed": battle_seed,
                "a_side": a_side,
                "winner_side": outcome.winner,
                "winner_agent": winner_agent,
                "turns": outcome.turns,
                "decisions": outcome.decisions,
                "a_preview": a_preview,
                "b_preview": b_preview,
                "a_lead": _species_key(a_preview.get("leads")),
                "b_lead": _species_key(b_preview.get("leads")),
                "a_pick": _species_key(a_preview.get("picked")),
                "b_pick": _species_key(b_preview.get("picked")),
            })

    return {
        "format": battle_format,
        "seed": seed,
        "timestamp": datetime.now(UTC).isoformat(),
        "summary": summarize(records),
        "games": records,
    }


def print_report(result: dict[str, Any]) -> None:
    s = result["summary"]
    low, high = s["a_wilson_95"]
    print(f"Team A vs Team B: {s['a_wins']}-{s['b_wins']}-{s['draws']} of {s['games']} games")
    print(f"Team A win rate: {s['a_win_rate']:.1%} (95% CI {low:.1%}..{high:.1%})")
    print(f"Mean turns: {s['mean_turns']:.2f}")
    print("\nTeam A leads:")
    for row in s["a_leads"][:10]:
        print(f"  {row['key']:<34} {row['wins']:>4}/{row['games']:<4} {row['win_rate']:.1%}")
    print("\nOpponent leads (Team A win rate):")
    for row in s["b_leads_from_a_perspective"][:10]:
        print(f"  {row['key']:<34} {row['wins']:>4}/{row['games']:<4} {row['win_rate']:.1%}")
    if any(s["fallbacks"].values()):
        print(f"\nWARNING: policy fallbacks occurred: {s['fallbacks']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team-a", type=Path, required=True)
    parser.add_argument("--team-b", type=Path, required=True)
    parser.add_argument("--games", "-n", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260916)
    parser.add_argument("--format", default=FORMAT_ID)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    if args.games <= 0:
        raise SystemExit("--games must be > 0")
    result = run_matchup(args.team_a.read_text().strip(), args.team_b.read_text().strip(), args.games, args.seed, args.format)
    print_report(result)
    output = args.output or RUNS_DIR / "matchups" / f"{args.team_a.stem}_vs_{args.team_b.stem}_{args.games}_seed{args.seed}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True))
    print(f"\nwrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
