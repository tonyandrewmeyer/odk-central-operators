"""Refreshing the charms must not break a working deployment."""

from __future__ import annotations

from typing import Any

import jubilant
import requests

from .conftest import CHARMS, Deployment, charm_path


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

    form = deployment.publish_form(project["id"], "post_refresh_form")
    # Still able to authenticate to Enketo, so the shared secrets survived.
    enketo_id = deployment.wait_for_enketo_id(project["id"], form["xmlFormId"])

    rendered = requests.get(f"{deployment.base_url}/enketo-passthrough/{enketo_id}", timeout=120)
    assert rendered.status_code == 200

    served = requests.get(f"{deployment.base_url}/v1/config/public", timeout=60)
    assert served.status_code == 200
