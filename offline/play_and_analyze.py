#!/usr/bin/env python
"""Play one real Champions M-C ladder game manually and analyze it afterward.

The human selects every team-preview and battle action. The program records Showdown's
private player request stream, verifies the resulting decision bundle can be rebuilt,
then writes both machine-readable and text analysis reports.

Credentials use the same contract as ladder/run_ladder.py:
VGC_SHOWDOWN_USERNAME / VGC_SHOWDOWN_PASSWORD
or the gitignored .showdown-credentials.json file.

Example:
    .venv/bin/python offline/play_and_analyze.py --team teams/recksal_mc.packed.txt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from poke_env.ps_client.account_configuration import AccountConfiguration  # noqa: E402
from poke_env.ps_client.server_configuration import ShowdownServerConfiguration  # noqa: E402

from ladder.run_ladder import DEFAULT_CREDENTIALS_FILE, load_credentials  # noqa: E402
from vgc.battle_state_replay import verify_decision_replay_bundle  # noqa: E402
from vgc.config import FORMAT_ID, RUNS_DIR, TEAMS_DIR  # noqa: E402
from vgc.game_analyzer import analyze_decision_bundle, render_text_report  # noqa: E402
from vgc.human_capture import HumanCapturePlayer  # noqa: E402
from vgc.models import PolicyConfig  # noqa: E402

DEFAULT_OUT_DIR = RUNS_DIR / "game-analyzer-real"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--team", type=Path, default=TEAMS_DIR / "recksal_mc.packed.txt")
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_CREDENTIALS_FILE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--top-k", type=int, default=3)
    return parser.parse_args()


async def play_and_analyze(
    *,
    team: str,
    username: str,
    password: str,
    out_dir: Path,
    top_k: int,
) -> dict[str, object]:
    out_dir.mkdir(parents=True, exist_ok=True)
    replay_dir = out_dir / "replays"
    replay_dir.mkdir(parents=True, exist_ok=True)

    config = PolicyConfig(
        format_id=FORMAT_ID,
        accept_open_team_sheet=False,
        log_decisions=False,
    )
    player = HumanCapturePlayer(
        config=config,
        team=team,
        battle_format=FORMAT_ID,
        accept_open_team_sheet=False,
        record_decision_replays=True,
        save_replays=str(replay_dir),
        account_configuration=AccountConfiguration(username, password),
        server_configuration=ShowdownServerConfiguration,
    )

    print(f"Joining one {FORMAT_ID} ladder game as {username}.")
    print("You will choose every action; the bot policy will not make battle decisions.")
    try:
        await player.ladder(1)
    finally:
        await player.ps_client.stop_listening()

    if not player.battles:
        raise RuntimeError("ladder session ended without a battle")
    battle = max(
        player.battles.values(),
        key=lambda item: int(getattr(item, "turn", 0) or 0),
    )
    bundle = player.decision_replay_bundle(battle)
    if bundle is None:
        raise RuntimeError("completed battle has no decision replay bundle")

    verification = await verify_decision_replay_bundle(bundle)
    if not verification.ready:
        mismatch_preview = "\n".join(verification.mismatches[:10])
        raise RuntimeError(
            "captured decision bundle failed replay verification; refusing to grade it."
            + (f"\n{mismatch_preview}" if mismatch_preview else "")
        )

    report = await analyze_decision_bundle(bundle, top_k=top_k)
    tag = str(bundle.get("battle_tag") or "battle").replace("/", "_")
    bundle_path = out_dir / f"{tag}.decision-replay.json"
    json_path = out_dir / f"{tag}.analysis.json"
    text_path = out_dir / f"{tag}.analysis.txt"
    bundle_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_report = render_text_report(report)
    text_path.write_text(text_report + "\n", encoding="utf-8")

    print("")
    print(text_report)
    print("")
    print(f"Verified decision bundle: {bundle_path}")
    print(f"JSON analysis:           {json_path}")
    print(f"Text analysis:           {text_path}")
    return report


def main() -> int:
    args = parse_args()
    if args.top_k < 1:
        raise ValueError("--top-k must be at least 1")
    if not args.team.is_file():
        raise FileNotFoundError(f"team file not found: {args.team}")
    team = args.team.read_text(encoding="utf-8").strip()
    if not team:
        raise ValueError(f"team file is empty: {args.team}")
    credentials = load_credentials(args.credentials_file)
    asyncio.run(
        play_and_analyze(
            team=team,
            username=credentials.username,
            password=credentials.password,
            out_dir=args.out_dir,
            top_k=args.top_k,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
