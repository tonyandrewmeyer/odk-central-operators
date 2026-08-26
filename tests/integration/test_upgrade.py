"""Refreshing the charms must not break a working deployment."""

from __future__ import annotations

from typing import Any

import jubilant
import requests

from .conftest import CHARMS, DATA, Deployment, charm_path

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_refresh_charm(deployment: Deployment, project: dict[str, Any]) -> None:
    """All three charms refresh to the packed revision and keep working.

    Refresh restarts the pods, which is exactly when a charm that only
    reconciles its workload's configuration on startup looks correct while
    being wrong, so the form lifecycle is exercised again afterwards rather
    than only checking that everything reports active.
    """
    for name in CHARMS:
        deployment.juju.refresh(name, path=charm_path(name))

    deployment.juju.wait(jubilant.all_active, timeout=25 * 60)

    response = deployment.api(
        "POST",
        f"/v1/projects/{project['id']}/forms?ignoreWarnings=true&publish=true",
        headers={"Content-Type": XLSX, "X-XlsForm-FormId-Fallback": "post_refresh_form"},
        data=(DATA / "minimal.xlsx").read_bytes(),
    )
    assert response.status_code == 200, response.text

    form = response.json()
    # Still able to authenticate to Enketo, so the shared secrets survived.
    assert form["enketoId"]

    rendered = requests.get(
        f"{deployment.base_url}/enketo-passthrough/{form['enketoId']}", timeout=120
    )
    assert rendered.status_code == 200

    served = requests.get(f"{deployment.base_url}/v1/config/public", timeout=60)
    assert served.status_code == 200
