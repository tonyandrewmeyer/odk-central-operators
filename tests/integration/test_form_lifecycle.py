"""The end-to-end path that proves all three charms are cooperating.

Each step exercises a different charm boundary, and each fails in a way that
points at the boundary that broke:

* uploading an XLSForm goes through `pyxform-k8s`;
* the form gaining an Enketo id means Central authenticated to `enketo-k8s`
  with the shared secret;
* rendering the form means Enketo could fetch it back from Central;
* the OData read means the submission survived the round trip.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import requests

from .conftest import DATA, Deployment

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(scope="module")
def published_form(deployment: Deployment, project: dict[str, Any]) -> dict[str, Any]:
    """Upload and publish the minimal form, and return it."""
    response = deployment.api(
        "POST",
        f"/v1/projects/{project['id']}/forms?ignoreWarnings=true&publish=true",
        headers={"Content-Type": XLSX, "X-XlsForm-FormId-Fallback": "minimal_test_form"},
        data=(DATA / "minimal.xlsx").read_bytes(),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_xlsform_conversion(published_form: dict[str, Any]) -> None:
    """The spreadsheet became a form, which means pyxform converted it."""
    assert published_form["xmlFormId"] == "minimal_test_form"


def test_form_has_an_enketo_id(published_form: dict[str, Any]) -> None:
    """An Enketo id means Central authenticated to Enketo successfully.

    This is where a mismatched shared secret shows up: Enketo rejects the
    request and the form is created with no id at all.
    """
    assert published_form["enketoId"]


def test_form_renders_in_enketo(deployment: Deployment, published_form: dict[str, Any]) -> None:
    """Enketo serves the form, having fetched it back from Central."""
    enketo_id = published_form["enketoId"]

    response = requests.get(f"{deployment.base_url}/enketo-passthrough/{enketo_id}", timeout=120)

    assert response.status_code == 200
    assert "<title>Enketo</title>" in response.text


def test_public_link_can_be_created(
    deployment: Deployment, project: dict[str, Any], published_form: dict[str, Any]
) -> None:
    """A public link is what a respondent is actually given."""
    response = deployment.api(
        "POST",
        f"/v1/projects/{project['id']}/forms/{published_form['xmlFormId']}/public-links",
        json={"displayName": "integration link", "once": False},
    )

    assert response.status_code == 200, response.text
    assert response.json()["token"]


def test_submission_round_trips_through_odata(
    deployment: Deployment, project: dict[str, Any], published_form: dict[str, Any]
) -> None:
    """A submitted value comes back out of the OData feed unchanged."""
    form_id = published_form["xmlFormId"]
    instance_id = f"uuid:{uuid.uuid4()}"
    submission = f"""<?xml version="1.0"?>
<data id="{form_id}" version="1">
  <name_of_respondent>Ada Lovelace</name_of_respondent>
  <meta><instanceID>{instance_id}</instanceID></meta>
</data>"""

    posted = deployment.api(
        "POST",
        f"/v1/projects/{project['id']}/forms/{form_id}/submissions",
        headers={"Content-Type": "application/xml"},
        data=submission.encode(),
    )
    assert posted.status_code == 200, posted.text

    feed = deployment.api("GET", f"/v1/projects/{project['id']}/forms/{form_id}.svc/Submissions")
    assert feed.status_code == 200, feed.text

    rows = feed.json()["value"]
    assert [row["name_of_respondent"] for row in rows] == ["Ada Lovelace"]
    assert rows[0]["__id"] == instance_id
