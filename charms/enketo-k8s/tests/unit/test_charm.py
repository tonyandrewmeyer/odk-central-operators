"""Unit tests for the enketo-k8s charm."""

from __future__ import annotations

import pytest
from charm import EnketoCharm
from ops import testing


@pytest.fixture
def ctx() -> testing.Context[EnketoCharm]:
    """Return a Scenario context for the charm."""
    return testing.Context(EnketoCharm)


@pytest.fixture
def containers() -> set[testing.Container]:
    """Return the three workload containers, none yet connected."""
    return {
        testing.Container("enketo", can_connect=False),
        testing.Container("redis-main", can_connect=False),
        testing.Container("redis-cache", can_connect=False),
    }


@pytest.mark.parametrize("event_name", ["install", "config_changed", "upgrade_charm"])
def test_lifecycle_events_reach_a_known_status(
    ctx: testing.Context[EnketoCharm],
    containers: set[testing.Container],
    event_name: str,
) -> None:
    """Every lifecycle hook leaves the unit in a status the operator can act on."""
    state_in = testing.State(containers=containers)
    event = getattr(ctx.on, event_name)()

    state_out = ctx.run(event, state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
