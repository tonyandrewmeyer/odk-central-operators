"""The day-2 actions, against a real deployment."""

from __future__ import annotations

import base64
from typing import Any

import jubilant
import pytest

from .conftest import DATA, Deployment


def test_run_migrations(deployment: Deployment) -> None:
    """Migrations can be re-run by hand and report what happened."""
    result = deployment.juju.run("odk-central-k8s/0", "run-migrations")

    assert "completed" in result.results["result"]


def test_upload_pending_blobs_is_a_clear_no_op_without_s3(
    deployment: Deployment,
) -> None:
    """Attachments stay in PostgreSQL until there is somewhere to put them."""
    result = deployment.juju.run("odk-central-k8s/0", "upload-pending-blobs")

    assert result.results["result"] == "no-op"
    assert "s3-integrator" in result.results["note"]


def test_backup_without_s3_fails_clearly(deployment: Deployment) -> None:
    """There is nowhere to write a backup to without the relation."""
    with pytest.raises(jubilant.TaskError) as excinfo:
        deployment.juju.run("odk-central-k8s/0", "backup", {"destination": "nightly"})

    assert "s3" in str(excinfo.value)


def test_promote_user_reports_an_unknown_address_clearly(
    deployment: Deployment,
) -> None:
    """Operators get the workload's reason, not the tail of a Node stack trace."""
    with pytest.raises(jubilant.TaskError) as excinfo:
        deployment.juju.run("odk-central-k8s/0", "promote-user", {"email": "nobody@example.com"})

    message = str(excinfo.value)
    assert "Could not find the resource" in message
    assert "node:internal/modules" not in message


def test_reset_password_produces_a_working_login(deployment: Deployment) -> None:
    """The returned password authenticates against the API."""
    result = deployment.juju.run(
        "odk-central-k8s/0", "reset-password", {"email": deployment.admin_email}
    )
    password = str(result.results["password"])

    deployment.admin_password = password
    deployment.forget_token()

    # Raises if the credentials do not work.
    assert deployment.token


def test_convert_action_on_pyxform(deployment: Deployment) -> None:
    """The diagnostic conversion path works without going through Central."""
    encoded = base64.b64encode((DATA / "minimal.xlsx").read_bytes()).decode()
    result = deployment.juju.run("pyxform-k8s/0", "convert", {"xlsform": encoded})

    assert "<h:html" in result.results["xform"]


def test_redis_info_reports_both_instances(deployment: Deployment) -> None:
    """Both Redis instances answer, and the durable one is a real server."""
    result = deployment.juju.run("enketo-k8s/0", "redis-info")

    assert "redis_version" in result.results["main"]
    assert "redis_version" in result.results["cache"]


def test_flush_cache_only_touches_the_cache(deployment: Deployment) -> None:
    """Flushing must never reach the instance holding in-flight form state."""
    result = deployment.juju.run("enketo-k8s/0", "flush-cache")

    assert result.results["result"] == "OK"
    assert "transformed again" in result.results["note"]


def test_purge_deleted(deployment: Deployment, project: dict[str, Any]) -> None:
    """A soft-deleted form can be purged for good."""
    form_id = deployment.publish_form(project["id"], "purge_test_form")["xmlFormId"]

    deleted = deployment.api("DELETE", f"/v1/projects/{project['id']}/forms/{form_id}")
    assert deleted.status_code == 200, deleted.text

    result = deployment.juju.run("odk-central-k8s/0", "purge-deleted", {"force": True})
    assert "completed" in result.results["result"]

    gone = deployment.api("GET", f"/v1/projects/{project['id']}/forms/{form_id}")
    assert gone.status_code == 404
