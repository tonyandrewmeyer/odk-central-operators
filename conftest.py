"""Root pytest configuration for the odk-central-operators workspace."""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Treat an empty test session as success.

    The CI matrix runs the per-charm unit suites before every charm has tests,
    and ``pytest`` exits 5 ("no tests collected") in that case. An empty suite
    is not a failure for this repository's scaffold phases.
    """
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = 0
