#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""A Prometheus exporter for the pyxform conversion service.

pyxform-http exposes no metrics: it is a small Flask application behind
gunicorn, with no instrumentation and no counters to read. What matters
operationally is not request volume but whether conversion still works, because
a converter that is up but failing blocks all form publishing in ODK Central
while every other part of the deployment looks healthy.

So this exporter does what the charm's readiness probe does, on a timer: it
converts a real two-question XLSForm and reports whether the result was an
XForm, and how long it took.

Standard library only. The image is a Python image, but its virtualenv contains
the service's dependencies, not ours, and adding to it is not the charm's
business.
"""

from __future__ import annotations

import http.server
import os
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("EXPORTER_PORT", "9103"))
TARGET = os.environ.get("PYXFORM_URL", "http://localhost:80")
PROBE_FORM = os.environ.get("PROBE_FORM", "/usr/share/odk/probe-form.xlsx")
# The conversion is real work; do not run it more often than this however
# often Prometheus scrapes.
INTERVAL = max(60, int(os.environ.get("EXPORTER_INTERVAL", "60")))
TIMEOUT = int(os.environ.get("EXPORTER_TIMEOUT", "60"))

_state: dict[str, float] = {
    "pyxform_up": 0.0,
    "pyxform_conversion_success": 0.0,
    "pyxform_conversion_duration_seconds": 0.0,
    "pyxform_scrape_success": 0.0,
}
_lock = threading.Lock()

HELP = {
    "pyxform_up": "Whether the conversion service answered at all.",
    "pyxform_conversion_success": (
        "Whether converting a known-good XLSForm produced an XForm. "
        "A converter that is up but failing blocks all form publishing."
    ),
    "pyxform_conversion_duration_seconds": "How long the probe conversion took.",
    "pyxform_scrape_success": "Whether the last collection ran without error.",
}


def probe() -> None:
    """Convert the probe form once and record what happened."""
    values = dict.fromkeys(_state, 0.0)
    started = time.monotonic()
    try:
        with open(PROBE_FORM, "rb") as handle:
            payload = handle.read()

        request = urllib.request.Request(  # noqa: S310 - fixed localhost URL
            f"{TARGET}/api/v1/convert",
            data=payload,
            headers={"X-XlsForm-FormId-Fallback": "charm-exporter"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            body = response.read().decode("utf-8", "replace")
            values["pyxform_up"] = 1.0 if response.status == 200 else 0.0

        values["pyxform_conversion_success"] = 1.0 if "<h:html" in body else 0.0
        values["pyxform_scrape_success"] = 1.0
    except (OSError, urllib.error.URLError) as exc:
        print(f"[pyxform-exporter] probe failed: {exc}", flush=True)
    finally:
        values["pyxform_conversion_duration_seconds"] = time.monotonic() - started
        with _lock:
            _state.update(values)


def render() -> bytes:
    """Return the current metrics in Prometheus text format."""
    with _lock:
        values = dict(_state)
    lines: list[str] = []
    for name, value in values.items():
        lines.append(f"# HELP {name} {HELP[name]}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return ("\n".join(lines) + "\n").encode()


class Handler(http.server.BaseHTTPRequestHandler):
    """Serve the metrics, and nothing else."""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Handle a GET request."""
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress per-request logging; a scrape every 15s is not news."""


def main() -> None:
    """Probe on a timer and serve the results."""

    def loop() -> None:
        while True:
            probe()
            time.sleep(INTERVAL)

    threading.Thread(target=loop, daemon=True).start()
    print(f"[pyxform-exporter] listening on {PORT}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()  # noqa: S104


if __name__ == "__main__":
    main()
