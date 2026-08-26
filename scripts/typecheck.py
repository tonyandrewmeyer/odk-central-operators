#!/usr/bin/env python3
"""Type-check the repository, one charm at a time.

All three charms have a ``src/charm.py``, so a single ``mypy`` invocation over
the repository sees three modules named ``charm`` and refuses to continue. Each
charm is therefore checked in its own run, with its own ``lib/`` on
``MYPYPATH`` so that the vendored charm libraries resolve.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHARMS = ("odk-central-k8s", "enketo-k8s", "pyxform-k8s")


def run(targets: list[str], mypy_path: list[Path]) -> int:
    """Run mypy over ``targets`` with ``mypy_path`` prepended to MYPYPATH."""
    env = dict(os.environ)
    if mypy_path:
        env["MYPYPATH"] = os.pathsep.join(str(p) for p in mypy_path)
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", *targets],
        cwd=REPO_ROOT,
        env=env,
    )
    return result.returncode


def main() -> int:
    """Type-check each charm and the repository's own tooling."""
    failures: list[str] = []

    for charm in CHARMS:
        charm_dir = REPO_ROOT / "charms" / charm
        if not (charm_dir / "src").is_dir():
            continue
        # The charm's own lib/ is on MYPYPATH so that the ODK libraries resolve;
        # vendored third-party libraries are excluded by the mypy config.
        if run([str(charm_dir / "src")], [charm_dir / "lib"]):
            failures.append(charm)

    # The canonical copies of the ODK charm libraries, and the repo tooling.
    if run([str(REPO_ROOT / "lib"), str(REPO_ROOT / "scripts")], []):
        failures.append("lib+scripts")

    if failures:
        print(f"\nType checking failed for: {', '.join(failures)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
