"""Rotating the shared secrets has to reach both charms.

A rotation that updates Central but not Enketo leaves every web form returning
an authentication failure, and nothing in the deployment looks unhealthy while
it does: both applications stay active and both report themselves fine.
"""

from __future__ import annotations

from typing import Any

import jubilant
import requests

from .conftest import DATA, Deployment

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _publish_form(deployment: Deployment, project_id: int, form_id: str) -> dict[str, Any]:
    """Publish a form and return it."""
    response = deployment.api(
        "POST",
        f"/v1/projects/{project_id}/forms?ignoreWarnings=true&publish=true",
        headers={"Content-Type": XLSX, "X-XlsForm-FormId-Fallback": form_id},
        data=(DATA / "minimal.xlsx").read_bytes(),
    )
    response.raise_for_status()
    return dict(response.json())


def test_rotate_enketo_secrets(deployment: Deployment, project: dict[str, Any]) -> None:
    """After a rotation, web forms still render."""
    form = _publish_form(deployment, project["id"], "rotation_test_form")
    enketo_id = form["enketoId"]
    assert enketo_id, "the form had no Enketo id even before rotating"

    before = requests.get(f"{deployment.base_url}/enketo-passthrough/{enketo_id}", timeout=120)
    assert before.status_code == 200

    result = deployment.juju.run("odk-central-k8s/0", "rotate-enketo-secrets")
    assert "rotated" in result.results["result"]
    # The action tells operators what it costs them.
    assert "start again" in result.results["warning"]

    deployment.juju.wait(jubilant.all_active, timeout=15 * 60)

    after = requests.get(f"{deployment.base_url}/enketo-passthrough/{enketo_id}", timeout=120)
    assert after.status_code == 200, (
        "Enketo stopped serving after the rotation, which means the new secret "
        "reached Central but not Enketo."
    )


def test_a_new_form_still_gets_an_enketo_id_after_rotation(
    deployment: Deployment, project: dict[str, Any]
) -> None:
    """Central can still authenticate to Enketo with the rotated key."""
    form = _publish_form(deployment, project["id"], "post_rotation_form")

    assert form["enketoId"]


def test_secrets_are_not_in_config(deployment: Deployment) -> None:
    """No credential may be readable from `juju config`."""
    for app in ("odk-central-k8s", "enketo-k8s", "pyxform-k8s"):
        config = deployment.juju.config(app)
        for key, value in config.items():
            if not isinstance(value, str):
                continue
            # Secret-typed options hold a URI, not a value.
            assert not value.startswith("secret:") or key.endswith("-secret"), key
            assert "password" not in key or not value, key
