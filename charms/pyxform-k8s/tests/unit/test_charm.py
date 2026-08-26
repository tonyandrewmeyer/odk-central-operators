"""Unit tests for the pyxform-k8s charm."""

from __future__ import annotations

import pytest
from charm import PyxformCharm
from ops import testing


@pytest.fixture
def ctx() -> testing.Context[PyxformCharm]:
    """Return a Scenario context for the charm."""
    return testing.Context(PyxformCharm)


@pytest.fixture
def container() -> testing.Container:
    """Return the pyxform workload container, not yet connected."""
    return testing.Container("pyxform", can_connect=False)


@pytest.mark.parametrize("event_name", ["install", "config_changed", "upgrade_charm"])
def test_lifecycle_events_reach_a_known_status(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    event_name: str,
) -> None:
    """Every lifecycle hook leaves the unit in a status the operator can act on."""
    state_in = testing.State(containers={container})
    event = getattr(ctx.on, event_name)()

    state_out = ctx.run(event, state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
