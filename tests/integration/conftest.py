"""Fixtures for the ODK Central charm group's integration tests.

One deployment serves the whole suite. Standing the group up takes long enough
that a per-test model would make the suite unusable, and the interesting
failures in this charm group are the ones that only happen across charm
boundaries, so the tests are written to share a model without depending on each
other's ordering.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jubilant
import pytest
import requests

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA = Path(__file__).parent.parent / "data"

CHARMS = ("odk-central-k8s", "enketo-k8s", "pyxform-k8s")

# Pinned in the charms' own charmcraft.yaml; repeated here so that a mismatch
# between what is packed and what is deployed shows up as a test failure.
CENTRAL_VERSION = "v2026.2.4"
IMAGES = {
    "odk-central-k8s": {
        "service-image": f"ghcr.io/getodk/central-service:{CENTRAL_VERSION}",
        "nginx-image": f"ghcr.io/getodk/central-nginx:{CENTRAL_VERSION}",
    },
    "enketo-k8s": {
        "enketo-image": "ghcr.io/enketo/enketo:7.6.1",
        "redis-image": "redis:8.6.4",
    },
    "pyxform-k8s": {"pyxform-image": "ghcr.io/getodk/pyxform-http:v4.5.0"},
}

# ODK Central targets PostgreSQL 14: its image ships postgresql-client-14, and
# pg_dump refuses to dump a newer server. See docs/day2-ops.md.
POSTGRESQL_CHANNEL = "14/stable"

DEPLOY_TIMEOUT = 45 * 60
SETTLE_TIMEOUT = 20 * 60


def charm_path(name: str) -> Path:
    """Return the packed charm for ``name``.

    Honours CHARM_ARTIFACT_DIR so that CI can pack once in an earlier job and
    hand the artefacts to this one.
    """
    artifacts = os.environ.get("CHARM_ARTIFACT_DIR")
    candidates = []
    if artifacts:
        candidates.extend(Path(artifacts).rglob(f"{name}_*.charm"))
    candidates.extend((REPO_ROOT / "charms" / name).glob(f"{name}_*.charm"))
    if not candidates:
        pytest.fail(
            f"No packed charm for {name}. Run `charmcraft pack` in "
            f"charms/{name}, or set CHARM_ARTIFACT_DIR."
        )
    return candidates[0]


@dataclass
class Deployment:
    """A deployed ODK Central group, and how to talk to it."""

    juju: jubilant.Juju
    base_url: str
    admin_email: str
    admin_password: str
    _token: str | None = None

    @property
    def token(self) -> str:
        """Return a session token, creating one on first use."""
        if self._token is None:
            response = requests.post(
                f"{self.base_url}/v1/sessions",
                json={"email": self.admin_email, "password": self.admin_password},
                timeout=60,
            )
            response.raise_for_status()
            self._token = str(response.json()["token"])
        return self._token

    def forget_token(self) -> None:
        """Drop the cached session, so the next call authenticates again."""
        self._token = None

    @property
    def headers(self) -> dict[str, str]:
        """Return authorisation headers for the Central API."""
        return {"Authorization": f"Bearer {self.token}"}

    def api(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Call the Central API with the admin session."""
        headers = {**self.headers, **kwargs.pop("headers", {})}
        return requests.request(
            method, f"{self.base_url}{path}", headers=headers, timeout=120, **kwargs
        )


def _traefik_hostname(juju: jubilant.Juju) -> str:
    """Return the hostname traefik will serve the deployment at.

    Read from the traefik charm rather than from the Kubernetes Service, so
    that this works the same on Canonical Kubernetes, microk8s and anywhere
    else without needing a kubectl on PATH.
    """
    endpoints = json.loads(
        juju.run("traefik-k8s/0", "show-proxied-endpoints").results["proxied-endpoints"]
    )
    address = urlparse(endpoints["traefik-k8s"]["url"]).hostname
    if not address:
        pytest.fail(f"traefik did not report an address: {endpoints}")
    # nip.io resolves any name under a dashed address to that address, which
    # avoids needing real DNS for a test deployment. ODK Central will not work
    # with a bare IP: the frontend bakes the origin into its client config, and
    # Enketo's linked-server URL has to match it.
    return f"{address.replace('.', '-')}.nip.io"


def _wait_for(predicate: Any, timeout: int, description: str) -> None:
    """Poll ``predicate`` until it is true, or fail the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(5)
    pytest.fail(f"timed out waiting for {description}")


@pytest.fixture(scope="module")
def deployment() -> Generator[Deployment, None, None]:
    """Deploy the whole charm group and yield a handle with admin credentials."""
    with jubilant.temp_model() as juju:
        juju.wait_timeout = DEPLOY_TIMEOUT

        for name in CHARMS:
            juju.deploy(
                charm_path(name),
                name,
                resources=IMAGES[name],
                trust=name == "odk-central-k8s",
            )

        juju.deploy("postgresql-k8s", channel=POSTGRESQL_CHANNEL, trust=True)
        juju.deploy("traefik-k8s", channel="latest/stable", trust=True)

        # traefik has to be up before it can say what address it got.
        juju.wait(lambda status: jubilant.all_active(status, "traefik-k8s"))
        hostname = _traefik_hostname(juju)
        juju.config("traefik-k8s", {"external_hostname": hostname, "routing_mode": "subdomain"})

        juju.integrate("odk-central-k8s", "postgresql-k8s")
        juju.integrate("odk-central-k8s", "pyxform-k8s")
        juju.integrate("odk-central-k8s", "enketo-k8s")
        juju.integrate("odk-central-k8s", "traefik-k8s")

        juju.wait(jubilant.all_active, timeout=DEPLOY_TIMEOUT)

        base_url = f"http://{juju.model}-odk-central-k8s.{hostname}"

        admin_email = "integration@example.com"
        secret_uri = juju.add_secret("odk-admin-email", {"email": admin_email})
        juju.cli("grant-secret", str(secret_uri), "odk-central-k8s")
        juju.config("odk-central-k8s", {"admin-email-secret": str(secret_uri)})
        juju.wait(jubilant.all_active, timeout=SETTLE_TIMEOUT)

        result = juju.run("odk-central-k8s/0", "create-admin")
        password = str(result.results["password"])

        deployed = Deployment(
            juju=juju,
            base_url=base_url,
            admin_email=admin_email,
            admin_password=password,
        )
        _wait_for(
            lambda: requests.get(f"{base_url}/v1/config/public", timeout=30).status_code == 200,
            timeout=SETTLE_TIMEOUT,
            description="the API to answer through the ingress",
        )
        yield deployed


@pytest.fixture(scope="module")
def project(deployment: Deployment) -> dict[str, Any]:
    """Create a project for the suite to work in."""
    response = deployment.api("POST", "/v1/projects", json={"name": "integration"})
    response.raise_for_status()
    return dict(response.json())
