"""Unit tests for the pyxform-k8s charm."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import charm as charm_module
import pytest
import requests
from charm import PyxformCharm
from ops import testing

MINIMAL_XLSX = Path(__file__).parent.parent / "data" / "minimal.xlsx"

XFORM = (
    '<?xml version="1.0"?><h:html xmlns:h="http://www.w3.org/1999/xhtml">'
    "<h:head><h:title>Minimal test form</h:title></h:head><h:body/></h:html>"
)


class FakeResponse:
    """Stand-in for a ``requests`` response from the conversion service."""

    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        """Raise if the fake response carries an error status."""
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self) -> dict[str, Any]:
        """Return the decoded response body."""
        return self._payload


@pytest.fixture
def ctx() -> testing.Context[PyxformCharm]:
    """Return a Scenario context for the charm."""
    return testing.Context(PyxformCharm)


@pytest.fixture
def container() -> testing.Container:
    """Return a connectable pyxform workload container."""
    return testing.Container("pyxform", can_connect=True)


@pytest.fixture
def converts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch the conversion call to succeed, recording each request."""
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse({"error": None, "result": XFORM, "warnings": [], "status": 200})

    monkeypatch.setattr(charm_module.requests, "post", fake_post)
    return calls


@pytest.fixture
def fails_to_convert(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the conversion call to return a conversion error."""

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse(
            {"error": "Unknown question type 'textX'.", "result": None, "status": 400}
        )

    monkeypatch.setattr(charm_module.requests, "post", fake_post)


@pytest.fixture
def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the conversion call to fail at the transport level."""

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(charm_module.requests, "post", fake_post)


def _service_command(state: testing.State) -> str:
    """Return the command Pebble was told to run for the workload."""
    container = next(iter(state.containers))
    return container.layers["pyxform"].services["pyxform"].command


# Workload startup


def test_service_starts_on_pebble_ready(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """The workload is started and the unit goes active once it can convert."""
    state_in = testing.State(containers={container}, leader=True)

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)

    assert state_out.unit_status == testing.ActiveStatus()
    service = next(iter(state_out.containers)).service_statuses["pyxform"]
    assert service == testing.pebble.ServiceStatus.ACTIVE


def test_workload_port_is_opened_but_not_exposed(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """Port 80 is opened so the Kubernetes Service routes to it in-cluster."""
    state_in = testing.State(containers={container}, leader=True)

    state_out = ctx.run(ctx.on.pebble_ready(container), state_in)

    assert testing.TCPPort(80) in state_out.opened_ports


def test_config_flows_into_the_gunicorn_command(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """workers and conversion-timeout become gunicorn arguments."""
    state_in = testing.State(
        containers={container},
        leader=True,
        config={"workers": 7, "conversion-timeout": 300, "log-level": "DEBUG"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    command = _service_command(state_out)
    assert "--workers 7" in command
    assert "--timeout 300" in command
    assert "--log-level debug" in command
    # Upstream recycles a worker after every request; it is not a tuning knob.
    assert "--max-requests 1" in command


def test_warn_log_level_is_translated_for_gunicorn(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """The group spells it WARN; gunicorn only accepts "warning"."""
    state_in = testing.State(containers={container}, leader=True, config={"log-level": "WARN"})

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "--log-level warning" in _service_command(state_out)


def test_waits_for_the_container(ctx: testing.Context[PyxformCharm]) -> None:
    """Before Pebble is reachable the charm waits rather than erroring."""
    state_in = testing.State(containers={testing.Container("pyxform", can_connect=False)})

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"workers": 0}, "workers"),
        ({"conversion-timeout": 0}, "conversion-timeout"),
        ({"log-level": "VERBOSE"}, "log-level"),
    ],
)
def test_invalid_config_blocks(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    config: dict[str, Any],
    expected: str,
) -> None:
    """Invalid configuration blocks with a message naming the offending option."""
    state_in = testing.State(containers={container}, config=config)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert expected in state_out.unit_status.message


# The conversion probe


def test_probe_failure_blocks(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    fails_to_convert: None,
) -> None:
    """A service that answers but cannot convert is blocked, not active.

    This is the whole point of probing with a real form: a port check would
    pass for a gunicorn that fails on every conversion.
    """
    state_in = testing.State(containers={container}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "conversion probe failed" in state_out.unit_status.message


def test_unreachable_service_blocks(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    unreachable: None,
) -> None:
    """A service that cannot be reached blocks rather than raising."""
    state_in = testing.State(containers={container}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)


def test_probe_uses_a_real_xlsform(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """The probe posts an actual xlsx workbook to the conversion endpoint."""
    state_in = testing.State(containers={container}, leader=True)

    ctx.run(ctx.on.config_changed(), state_in)

    assert converts, "the charm did not probe the workload"
    request = converts[0]
    assert request["url"].endswith("/api/v1/convert")
    # PK is the zip magic number; pyxform-http sniffs it to pick .xlsx over .xls.
    assert request["data"][:2] == b"PK"


# The xlsform relation


def test_endpoint_is_published_on_the_relation(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """The leader advertises a stable in-cluster address and port."""
    relation = testing.Relation("xlsform")
    state_in = testing.State(containers={container}, relations={relation}, leader=True)

    state_out = ctx.run(ctx.on.relation_joined(relation), state_in)

    databag = state_out.get_relation(relation.id).local_app_data
    assert databag["port"] == "80"
    # The Kubernetes Service, not a unit IP, so scaling does not break Central.
    assert databag["host"].endswith(".svc.cluster.local")


def test_non_leader_does_not_write_to_the_databag(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """Only the leader writes application databag content."""
    relation = testing.Relation("xlsform")
    state_in = testing.State(containers={container}, relations={relation}, leader=False)

    state_out = ctx.run(ctx.on.relation_joined(relation), state_in)

    assert state_out.get_relation(relation.id).local_app_data == {}


def test_endpoint_is_withdrawn_when_the_probe_fails(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    fails_to_convert: None,
) -> None:
    """A broken converter stops advertising itself, so Central stops using it."""
    relation = testing.Relation(
        "xlsform", local_app_data={"host": "old.example.com", "port": "80"}
    )
    state_in = testing.State(containers={container}, relations={relation}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.get_relation(relation.id).local_app_data == {}


# The convert action


def test_convert_action_returns_the_xform(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """The diagnostic action returns the converted XForm."""
    encoded = base64.b64encode(MINIMAL_XLSX.read_bytes()).decode()
    state_in = testing.State(containers={container}, leader=True)

    ctx.run(ctx.on.action("convert", params={"xlsform": encoded}), state_in)

    assert ctx.action_results is not None
    assert "<h:html" in ctx.action_results["xform"]
    assert json.loads(ctx.action_results["warnings"]) == []


def test_convert_action_rejects_bad_base64(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    converts: list[dict[str, Any]],
) -> None:
    """A caller mistake fails the action with a clear message, not a traceback."""
    state_in = testing.State(containers={container}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("convert", params={"xlsform": "not base64!!"}), state_in)

    assert "base64" in excinfo.value.message


def test_convert_action_surfaces_a_conversion_error(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    fails_to_convert: None,
) -> None:
    """A form that does not convert fails the action with the converter's reason."""
    encoded = base64.b64encode(MINIMAL_XLSX.read_bytes()).decode()
    state_in = testing.State(containers={container}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("convert", params={"xlsform": encoded}), state_in)

    assert "textX" in excinfo.value.message


def test_convert_action_needs_the_container(
    ctx: testing.Context[PyxformCharm], converts: list[dict[str, Any]]
) -> None:
    """The action fails cleanly when Pebble is not reachable."""
    encoded = base64.b64encode(MINIMAL_XLSX.read_bytes()).decode()
    state_in = testing.State(containers={testing.Container("pyxform", can_connect=False)})

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("convert", params={"xlsform": encoded}), state_in)

    assert "not ready" in excinfo.value.message


def test_convert_action_reports_an_unreachable_service(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    unreachable: None,
) -> None:
    """A transport failure during the action is reported, not raised."""
    encoded = base64.b64encode(MINIMAL_XLSX.read_bytes()).decode()
    state_in = testing.State(containers={container}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("convert", params={"xlsform": encoded}), state_in)

    assert "Could not reach" in excinfo.value.message


def test_probe_rejects_a_response_that_is_not_an_xform(
    ctx: testing.Context[PyxformCharm],
    container: testing.Container,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response carrying something other than an XForm still blocks."""

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse({"error": None, "result": "not xml at all", "status": 200})

    monkeypatch.setattr(charm_module.requests, "post", fake_post)
    state_in = testing.State(containers={container}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "not an XForm" in state_out.unit_status.message
