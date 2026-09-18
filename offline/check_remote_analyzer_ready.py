#!/usr/bin/env python
"""Check whether this environment can run the real-game human analyzer."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vgc.config import FORMAT_ID, SHOWDOWN_REPO, TEAMS_DIR  # noqa: E402

TEAM_PATH = TEAMS_DIR / "recksal_mc.packed.txt"
EXPECTED_SHOWDOWN_PREFIX = "efe494857"


def _git_head(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=path, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> int:
    checks: list[tuple[str, bool, str]] = []

    checks.append(
        (
            "Python environment",
            Path(sys.executable).exists(),
            sys.executable,
        )
    )
    checks.append(
        (
            "Node 22",
            bool(shutil.which("node")),
            shutil.which("node") or "node not found",
        )
    )
    checks.append(
        (
            "Packed M-C team",
            TEAM_PATH.is_file(),
            str(TEAM_PATH),
        )
    )
    checks.append(
        (
            "Pokemon Showdown checkout",
            SHOWDOWN_REPO.is_dir(),
            str(SHOWDOWN_REPO),
        )
    )
    head = _git_head(SHOWDOWN_REPO) if SHOWDOWN_REPO.is_dir() else None
    checks.append(
        (
            "Pinned Showdown revision",
            bool(head and head.startswith(EXPECTED_SHOWDOWN_PREFIX)),
            head or "unavailable",
        )
    )
    checks.append(
        (
            "Showdown username secret",
            bool(os.environ.get("VGC_SHOWDOWN_USERNAME")),
            "set" if os.environ.get("VGC_SHOWDOWN_USERNAME") else "missing",
        )
    )
    checks.append(
        (
            "Showdown password secret",
            bool(os.environ.get("VGC_SHOWDOWN_PASSWORD")),
            "set" if os.environ.get("VGC_SHOWDOWN_PASSWORD") else "missing",
        )
    )

    print(f"Remote analyzer readiness — {FORMAT_ID}")
    failed = False
    for label, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {detail}")
        failed = failed or not ok

    if failed:
        print("")
        print("Not ready yet. In Codespaces, add VGC_SHOWDOWN_USERNAME and")
        print("VGC_SHOWDOWN_PASSWORD as Codespaces secrets, then rebuild/reopen if needed.")
        return 1

    print("")
    print("Ready for a real game. Run:")
    print(
        "  .venv/bin/python offline/play_and_analyze.py "
        "--team teams/recksal_mc.packed.txt"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
