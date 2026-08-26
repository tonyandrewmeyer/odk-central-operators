"""Make the charm's own source and libraries importable from its unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

CHARM_ROOT = Path(__file__).resolve().parent.parent

for path in (CHARM_ROOT / "src", CHARM_ROOT / "lib"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
