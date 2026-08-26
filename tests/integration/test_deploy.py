"""The deployment answers, and answers as itself."""

from __future__ import annotations

import requests

from .conftest import CENTRAL_VERSION, Deployment


def test_version_endpoint(deployment: Deployment) -> None:
    """The deployed release is the one the charms pin.

    Served by nginx as a static file, not by the API: there is no
    /v1/version.json in central-backend, whatever the internet says.
    """
    response = requests.get(f"{deployment.base_url}/version.txt", timeout=60)

    assert response.status_code == 200
    assert CENTRAL_VERSION in response.text


def test_api_is_reachable(deployment: Deployment) -> None:
    """The one unauthenticated endpoint that also proves the database works."""
    response = requests.get(f"{deployment.base_url}/v1/config/public", timeout=60)

    assert response.status_code == 200
    assert isinstance(response.json(), dict)


def test_ui_reachable(deployment: Deployment) -> None:
    """The web interface loads through the ingress."""
    response = requests.get(deployment.base_url, timeout=60)

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_frontend_config_matches_domain(deployment: Deployment) -> None:
    """The frontend must be served from the origin it was configured for.

    Central's client configuration is generated at container start from the
    domain the charm supplies. If it disagrees with where the deployment is
    actually reached, the browser's API calls go to the wrong origin and
    nothing else in this suite would notice.
    """
    response = requests.get(f"{deployment.base_url}/client-config.json", timeout=60)

    assert response.status_code == 200
    config = response.json()
    assert config["oidcEnabled"] is False
    # Upstream's shipped compose defaults are the ODK project's own Sentry
    # credentials. A deployment must never inherit them.
    assert config["sentryDsn"] == ""


def test_upstream_sentry_defaults_are_not_inherited(deployment: Deployment) -> None:
    """None of ODK's own telemetry identifiers may appear anywhere served."""
    for path in ("/client-config.json", "/"):
        body = requests.get(f"{deployment.base_url}{path}", timeout=60).text
        assert "o130137" not in body
        assert "3cf75f54983e473da6bd07daddf0d2ee" not in body


def test_enketo_is_proxied(deployment: Deployment) -> None:
    """nginx reaches Enketo, which is a separate application in a separate pod.

    Upstream hardcodes `proxy_pass http://enketo:8005`, a name that does not
    resolve under Juju, so this is really a test that the charm replaced it.
    """
    response = requests.get(f"{deployment.base_url}/-/thanks", timeout=60)

    assert response.status_code == 200
