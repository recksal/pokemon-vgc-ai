"""CLI for the V1 Champions game analyzer."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from vgc.game_analyzer import analyze_decision_bundle, render_text_report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze a saved player-view Champions decision replay."
    )
    parser.add_argument("bundle", type=Path, help="vgc-decision-replay-v1 JSON bundle")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--top-k", type=int, default=3, help="alternatives to retain per turn")
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    if args.top_k < 1:
        raise SystemExit("--top-k must be at least 1")
    bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
    report = await analyze_decision_bundle(bundle, top_k=args.top_k)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
