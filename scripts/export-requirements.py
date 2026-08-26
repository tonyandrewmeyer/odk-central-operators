#!/usr/bin/env python3
"""Generate each charm's ``requirements.txt`` from the workspace lock file.

The three charms are ``uv`` workspace members, so there is a single
``uv.lock`` at the repository root and the members do not have their own. But
``charmcraft pack`` runs inside one charm directory and cannot see the
workspace root, so each charm needs a self-contained, fully pinned and hashed
requirements file to build a reproducible venv from.

``uv export --package <charm>`` produces exactly that from the shared lock, so
the pins in the charm artefacts and the pins developers test against cannot
diverge.

Run with no arguments to verify (this is what CI and the pre-commit hook do),
or with ``--write`` to regenerate.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHARMS = ("odk-central-k8s", "enketo-k8s", "pyxform-k8s")


def export(charm: str) -> str:
    """Return the pinned, hashed requirements for one charm."""
    result = subprocess.run(
        [
            "uv",
            "export",
            "--package",
            charm,
            "--no-dev",
            "--no-emit-workspace",
            "--format",
            "requirements-txt",
            "--quiet",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def main() -> int:
    """Verify or regenerate the per-charm requirements files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the files instead of only checking them",
    )
    args = parser.parse_args()

    stale: list[str] = []
    for charm in CHARMS:
        target = REPO_ROOT / "charms" / charm / "requirements.txt"
        wanted = export(charm)
        if target.exists() and target.read_text() == wanted:
            continue
        if args.write:
            target.write_text(wanted)
            print(f"wrote charms/{charm}/requirements.txt")
        else:
            stale.append(f"charms/{charm}/requirements.txt")

    if stale:
        print(
            "These requirements files are out of date with uv.lock:\n  "
            + "\n  ".join(stale)
            + "\nRun: uv run python scripts/export-requirements.py --write",
            file=sys.stderr,
        )
        return 1

    if not args.write:
        print("Charm requirements files are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
