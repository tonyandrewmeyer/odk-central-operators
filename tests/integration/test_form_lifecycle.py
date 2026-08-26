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

import base64
import uuid
from typing import Any

import pytest
import requests

from .conftest import Deployment


@pytest.fixture(scope="session")
def published_form(deployment: Deployment, project: dict[str, Any]) -> dict[str, Any]:
    """Upload and publish a form of this module's own, and return it."""
    return deployment.publish_form(project["id"], "lifecycle_form")


def test_xlsform_conversion(published_form: dict[str, Any]) -> None:
    """The spreadsheet became a form, which means pyxform converted it."""
    assert published_form["xmlFormId"] == "lifecycle_form"


def test_form_has_an_enketo_id(
    deployment: Deployment, project: dict[str, Any], published_form: dict[str, Any]
) -> None:
    """An Enketo id means Central authenticated to Enketo successfully.

    This is where a mismatched shared secret shows up: Enketo rejects the
    request and the form never gets an id at all.
    """
    assert deployment.wait_for_enketo_id(project["id"], published_form["xmlFormId"])


def test_form_renders_in_enketo(
    deployment: Deployment, project: dict[str, Any], published_form: dict[str, Any]
) -> None:
    """Enketo serves the form, having fetched it back from Central."""
    enketo_id = deployment.wait_for_enketo_id(project["id"], published_form["xmlFormId"])

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


def test_submission_with_an_attachment(deployment: Deployment, project: dict[str, Any]) -> None:
    """An attachment survives the OpenRosa upload and comes back out again.

    Attachments are what the S3 blob store exists for. Without an `s3` relation
    they stay in PostgreSQL, which is the state this exercises; moving them is
    covered by the upload-pending-blobs action.
    """
    form = deployment.publish_form(project["id"], "attachment_form", with_attachment=True)
    instance_id = f"uuid:{uuid.uuid4()}"
    # A 1x1 PNG.
    photo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8A"
        "AAAASUVORK5CYII="
    )
    submission = f"""<?xml version="1.0"?>
<data id="{form["xmlFormId"]}" version="1">
  <name_of_respondent>Grace Hopper</name_of_respondent>
  <photo>photo.png</photo>
  <meta><instanceID>{instance_id}</instanceID></meta>
</data>"""

    posted = deployment.api(
        "POST",
        f"/v1/projects/{project['id']}/submission",
        headers={"X-OpenRosa-Version": "1.0"},
        files={
            "xml_submission_file": ("submission.xml", submission.encode(), "text/xml"),
            "photo.png": ("photo.png", photo, "image/png"),
        },
    )
    assert posted.status_code == 201, posted.text

    attachments = deployment.api(
        "GET",
        f"/v1/projects/{project['id']}/forms/{form['xmlFormId']}"
        f"/submissions/{instance_id}/attachments",
    )
    assert attachments.status_code == 200, attachments.text
    assert [a["name"] for a in attachments.json()] == ["photo.png"]
    assert attachments.json()[0]["exists"] is True

    fetched = deployment.api(
        "GET",
        f"/v1/projects/{project['id']}/forms/{form['xmlFormId']}"
        f"/submissions/{instance_id}/attachments/photo.png",
    )
    assert fetched.status_code == 200
    assert fetched.content == photo
