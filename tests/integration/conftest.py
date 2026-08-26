"""Fixtures for the ODK Central charm group's integration tests.

One deployment serves the whole suite. Standing the group up takes about
twenty-five minutes, so a per-module model would make the suite unusable, and
the interesting failures in this charm group are the ones that only happen
across charm boundaries anyway. The tests share a model and are written not to
depend on each other's ordering, with one exception noted in test_secrets.
"""

from __future__ import annotations

import io
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
from openpyxl import Workbook

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

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# How long to keep retrying a publish that fails because the conversion service
# is briefly unreachable, which happens for a few seconds after a refresh.
PUBLISH_RETRY_SECONDS = 120

# Central fills the Enketo id in from a background worker, so it is not always
# present on the response to the upload.
ENKETO_ID_TIMEOUT = 180

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


def make_xlsform(form_id: str, *, with_attachment: bool = False) -> bytes:
    """Return an XLSForm with the given form id.

    Built here rather than loaded from a fixture file because ODK Central takes
    the form id from the spreadsheet's own settings sheet -- the
    X-XlsForm-FormId-Fallback header only applies when the sheet does not
    provide one. Every test that publishes a form therefore needs its own
    spreadsheet, or the second upload into a project is a 409.
    """
    workbook = Workbook()
    survey = workbook.active
    assert survey is not None
    survey.title = "survey"
    survey.append(["type", "name", "label"])
    survey.append(["text", "name_of_respondent", "What is your name?"])
    if with_attachment:
        survey.append(["image", "photo", "Take a photo"])

    settings = workbook.create_sheet("settings")
    settings.append(["form_title", "form_id", "version"])
    settings.append([f"Integration form {form_id}", form_id, "1"])

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


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

    def publish_form(
        self, project_id: int, form_id: str, *, with_attachment: bool = False
    ) -> dict[str, Any]:
        """Upload and publish a form, and return it.

        Goes through pyxform-k8s, and the Enketo id in the result is what says
        Central could authenticate to enketo-k8s with the shared secret.

        Retries a 502 for a short while. Central reports one when it cannot
        reach the conversion service, which happens legitimately for a few
        seconds after a refresh: both applications report active before the
        Kubernetes Service behind pyxform has endpoints again.
        """
        payload = make_xlsform(form_id, with_attachment=with_attachment)
        deadline = time.monotonic() + PUBLISH_RETRY_SECONDS
        while True:
            response = self.api(
                "POST",
                f"/v1/projects/{project_id}/forms?ignoreWarnings=true&publish=true",
                headers={"Content-Type": XLSX_CONTENT_TYPE},
                data=payload,
            )
            if response.status_code != 502 or time.monotonic() > deadline:
                break
            logger.warning("conversion service not reachable yet, retrying: %s", response.text)
            time.sleep(5)

        assert response.status_code == 200, response.text
        form = dict(response.json())
        # Central takes the id from the spreadsheet's settings sheet. If that
        # ever stops being true, every test that publishes more than one form
        # would collide instead of failing here.
        assert form["xmlFormId"] == form_id, form
        return form

    def wait_for_enketo_id(self, project_id: int, form_id: str) -> str:
        """Return the form's Enketo id once Central has one, or fail.

        Central does not always have the id by the time it answers the upload:
        `pushFormToEnketo` is a background worker, and the request only carries
        an id when the call happened to complete inline. Asserting on the
        response is therefore a coin toss, and one that lands differently right
        after a restart.

        An id appearing at all is what proves Central authenticated to Enketo
        with the shared secret; an id that never appears is the failure worth
        reporting.
        """
        deadline = time.monotonic() + ENKETO_ID_TIMEOUT
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = self.api("GET", f"/v1/projects/{project_id}/forms/{form_id}")
            if response.status_code == 200:
                last = dict(response.json())
                if last.get("enketoId"):
                    return str(last["enketoId"])
            time.sleep(5)

        pytest.fail(
            f"{form_id} never got an Enketo id, which means Central could not "
            f"authenticate to Enketo with the shared secret. Last state: {last}"
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


@pytest.fixture(scope="session")
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


@pytest.fixture(scope="session")
def project(deployment: Deployment) -> dict[str, Any]:
    """Create a project for the suite to work in."""
    response = deployment.api("POST", "/v1/projects", json={"name": "integration"})
    response.raise_for_status()
    return dict(response.json())
