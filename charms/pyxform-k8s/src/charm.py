#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for pyxform, the XLSForm to XForm conversion service for ODK Central."""

from __future__ import annotations

import base64
import binascii
import json
import logging
from pathlib import Path
from typing import Any

import ops
import requests
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.pyxform_k8s.v0.xlsform import DEFAULT_PORT, XlsformProvider

logger = logging.getLogger(__name__)

WORKLOAD_CONTAINER = "pyxform"
PEBBLE_SERVICE = "pyxform"

# From the published image's own config: gunicorn lives in the project venv and
# the Flask app is importable from /app.
GUNICORN = "/app/.venv/bin/gunicorn"
WORKING_DIR = "/app"

CONVERT_PATH = "/api/v1/convert"
PROBE_FORM = Path(__file__).parent / "probe-form.xlsx"

# pyxform-http has no metrics of its own, so the charm ships a small exporter
# that converts a known-good form on a timer. What matters operationally is not
# request volume but whether conversion still works: a converter that is up but
# failing blocks all form publishing in Central while everything else looks fine.
EXPORTER_SERVICE = "exporter"
EXPORTER_PORT = 9103
EXPORTER_SOURCE = Path(__file__).parent / "exporter" / "pyxform-exporter.py"
EXPORTER_PATH = "/usr/share/odk/pyxform-exporter.py"
PROBE_FORM_PATH = "/usr/share/odk/probe-form.xlsx"
PYTHON = "/app/.venv/bin/python"

# Upstream runs one request per worker before recycling it. pyxform holds
# process-global state between conversions, so this is not a tuning knob.
MAX_REQUESTS = 1
MAX_REQUESTS_JITTER = 3

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


class ConversionProbeError(Exception):
    """The workload did not convert the probe form correctly."""


class PyxformCharm(ops.CharmBase):
    """Run the pyxform HTTP conversion service and advertise it to ODK Central."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)
        self.xlsform = XlsformProvider(self)
        self.logging = LogForwarder(self, relation_name="logging")
        self.metrics = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[{"static_configs": [{"targets": [f"*:{EXPORTER_PORT}"]}]}],
            refresh_event=[self.on.config_changed],
        )
        self.dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")

        framework.observe(self.on.install, self._on_lifecycle_event)
        framework.observe(self.on.config_changed, self._on_lifecycle_event)
        framework.observe(self.on.upgrade_charm, self._on_lifecycle_event)
        framework.observe(self.on.update_status, self._on_lifecycle_event)
        framework.observe(self.on[WORKLOAD_CONTAINER].pebble_ready, self._on_lifecycle_event)
        framework.observe(self.on["xlsform"].relation_joined, self._on_lifecycle_event)
        framework.observe(self.on.leader_elected, self._on_lifecycle_event)
        framework.observe(self.on.convert_action, self._on_convert_action)

    # Event handling

    def _on_lifecycle_event(self, event: ops.EventBase) -> None:
        """Reconcile on any event that could have changed the desired state."""
        self._reconcile()

    def _on_convert_action(self, event: ops.ActionEvent) -> None:
        """Convert a caller-supplied XLSForm and return the resulting XForm."""
        try:
            xlsform = base64.b64decode(event.params["xlsform"], validate=True)
        except (binascii.Error, ValueError) as exc:
            event.fail(f"xlsform is not valid base64: {exc}")
            return

        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if not container.can_connect():
            event.fail("The pyxform container is not ready.")
            return

        try:
            payload = self._convert(xlsform)
        except (requests.RequestException, ValueError) as exc:
            event.fail(f"Could not reach the conversion service: {exc}")
            return

        if payload.get("error"):
            event.fail(f"Conversion failed: {payload['error']}")
            return

        event.set_results(
            {
                "xform": payload.get("result", ""),
                "warnings": json.dumps(payload.get("warnings") or []),
            }
        )

    # Reconciliation

    def _reconcile(self) -> None:
        """Bring the workload into line with the charm's configuration.

        Every hook routes through here rather than each handler doing its own
        partial update, so that the charm's behaviour does not depend on which
        event happened to arrive.
        """
        invalid = self._invalid_config()
        if invalid:
            self.unit.status = ops.BlockedStatus(invalid)
            return

        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if not container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for the pyxform container")
            return

        container.push(EXPORTER_PATH, EXPORTER_SOURCE.read_text(), make_dirs=True)
        container.push(PROBE_FORM_PATH, PROBE_FORM.read_bytes(), make_dirs=True)

        container.add_layer(PEBBLE_SERVICE, self._pebble_layer(), combine=True)
        container.replan()
        self.unit.set_ports(DEFAULT_PORT, EXPORTER_PORT)

        # Reaching active means the converter converts. A port check would pass
        # for a gunicorn that imports pyxform and then fails on every form.
        try:
            self._probe()
        except (requests.RequestException, ConversionProbeError) as exc:
            logger.warning("conversion probe failed: %s", exc)
            self.xlsform.withdraw()
            self.unit.status = ops.BlockedStatus(f"conversion probe failed: {exc}")
            return

        self.xlsform.publish(host=self._service_host(), port=DEFAULT_PORT)
        self.unit.status = ops.ActiveStatus()

    def _invalid_config(self) -> str | None:
        """Return a message describing the first invalid config option, if any."""
        workers = int(self.config["workers"])
        if workers < 1:
            return "workers must be at least 1"

        timeout = int(self.config["conversion-timeout"])
        if timeout < 1:
            return "conversion-timeout must be at least 1 second"

        level = str(self.config["log-level"]).upper()
        if level not in VALID_LOG_LEVELS:
            return f"log-level must be one of {', '.join(VALID_LOG_LEVELS)}"

        return None

    def _pebble_layer(self) -> ops.pebble.Layer:
        """Build the Pebble layer for the gunicorn workload."""
        command = " ".join(
            [
                GUNICORN,
                "--bind",
                f"0.0.0.0:{DEFAULT_PORT}",
                "--workers",
                str(int(self.config["workers"])),
                "--timeout",
                str(int(self.config["conversion-timeout"])),
                "--max-requests",
                str(MAX_REQUESTS),
                "--max-requests-jitter",
                str(MAX_REQUESTS_JITTER),
                # Log to stdout so Pebble captures it and Loki can collect it.
                "--access-logfile",
                "-",
                "--error-logfile",
                "-",
                "--log-level",
                self._gunicorn_log_level(),
                "main:app()",
            ]
        )
        return ops.pebble.Layer(
            {
                "summary": "pyxform XLSForm conversion service",
                "description": "Converts XLSForm spreadsheets to ODK XForms.",
                "services": {
                    PEBBLE_SERVICE: {
                        "override": "replace",
                        "summary": "pyxform-http",
                        "command": command,
                        "startup": "enabled",
                        "working-dir": WORKING_DIR,
                        "on-failure": "restart",
                    },
                    EXPORTER_SERVICE: {
                        "override": "replace",
                        "summary": "prometheus exporter",
                        "command": f"{PYTHON} {EXPORTER_PATH}",
                        "startup": "enabled",
                        "environment": {
                            "EXPORTER_PORT": str(EXPORTER_PORT),
                            "PYXFORM_URL": f"http://localhost:{DEFAULT_PORT}",
                            "PROBE_FORM": PROBE_FORM_PATH,
                            # The probe is a real conversion; do not run it more
                            # often than this however often Prometheus scrapes.
                            "EXPORTER_INTERVAL": "60",
                            "EXPORTER_TIMEOUT": str(int(self.config["conversion-timeout"])),
                        },
                        "on-failure": "restart",
                    },
                },
                "checks": {
                    "online": {
                        "override": "replace",
                        "level": "ready",
                        "period": "15s",
                        "threshold": 3,
                        "http": {"url": f"http://localhost:{DEFAULT_PORT}/"},
                    },
                },
            }
        )

    def _gunicorn_log_level(self) -> str:
        """Map the charm's log-level onto the name gunicorn understands."""
        level = str(self.config["log-level"]).upper()
        # gunicorn spells it "warning"; the charm follows the group's convention.
        return {"WARN": "warning"}.get(level, level.lower())

    # Workload interaction

    def _service_host(self) -> str:
        """Return the in-cluster address Central should use for this service.

        The Kubernetes Service that Juju creates for the application, rather
        than a unit address, so that the endpoint keeps working when the
        application is scaled or a unit is rescheduled.
        """
        return f"{self.app.name}.{self.model.name}.svc.cluster.local"

    def _convert(self, xlsform: bytes) -> dict[str, Any]:
        """POST an XLSForm to the workload and return the decoded JSON response."""
        # The charm container shares a network namespace with the workload, so
        # the service is reachable on localhost.
        response = requests.post(
            f"http://localhost:{DEFAULT_PORT}{CONVERT_PATH}",
            data=xlsform,
            headers={"X-XlsForm-FormId-Fallback": "charm-probe"},
            timeout=int(self.config["conversion-timeout"]) + 5,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload

    def _probe(self) -> None:
        """Convert the bundled probe form and check the result is a real XForm.

        :raises ConversionProbeError: if the service answers but cannot convert.
        :raises requests.RequestException: if the service cannot be reached.
        """
        payload = self._convert(PROBE_FORM.read_bytes())

        if payload.get("error"):
            raise ConversionProbeError(str(payload["error"]))

        result = payload.get("result") or ""
        if "<h:html" not in result:
            raise ConversionProbeError("the converter returned something that is not an XForm")


if __name__ == "__main__":  # pragma: no cover
    ops.main(PyxformCharm)
