#!/usr/bin/env python3
"""Distribute the ODK-specific charm libraries into the charms that use them.

The canonical copy of each library lives in ``lib/charms/`` at the repository
root, so that the provider and the requirer are edited as one file and cannot
drift apart. Each charm needs its own copy under ``charms/<charm>/lib/``,
because ``charmcraft pack`` only sees files inside the charm's own directory —
a symlink pointing out of the project would not survive packing.

Run with no arguments to verify the copies are current (this is what CI and the
pre-commit hook do). Run with ``--write`` to refresh them.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Which charm gets which library. A charm only carries the libraries it uses:
# odk-central-k8s provides odk-enketo and requires xlsform, so it needs both.
DISTRIBUTION: dict[str, tuple[str, ...]] = {
    "odk-central-k8s": (
        "charms/odk_central_k8s/v0/odk_enketo.py",
        "charms/pyxform_k8s/v0/xlsform.py",
    ),
    "enketo-k8s": ("charms/odk_central_k8s/v0/odk_enketo.py",),
    "pyxform-k8s": ("charms/pyxform_k8s/v0/xlsform.py",),
}


def main() -> int:
    """Verify or refresh each charm's copy of the shared libraries."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="refresh the per-charm copies instead of only checking them",
    )
    args = parser.parse_args()

    stale: list[str] = []
    for charm, libraries in DISTRIBUTION.items():
        for library in libraries:
            source = REPO_ROOT / "lib" / library
            target = REPO_ROOT / "charms" / charm / "lib" / library
            if not source.exists():
                print(f"missing canonical library: {source}", file=sys.stderr)
                return 1

            current = target.exists() and filecmp.cmp(source, target, shallow=False)
            if current:
                continue

            if args.write:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                print(f"synced charms/{charm}/lib/{library}")
            else:
                stale.append(f"charms/{charm}/lib/{library}")

    if stale:
        print(
            "These charm library copies are out of date with lib/:\n  "
            + "\n  ".join(stale)
            + "\nRun: uv run python scripts/sync-libs.py --write",
            file=sys.stderr,
        )
        return 1

    if not args.write:
        print("Charm library copies are up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
