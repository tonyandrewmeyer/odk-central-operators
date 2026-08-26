"""Unit tests for the odk-central-k8s day-2 actions."""

from __future__ import annotations

import json
import pathlib

import pytest
from charm import SECRET_LABEL_ADMIN_PASSWORD, OdkCentralCharm
from charms.odk_central_k8s.v0.odk_enketo import SECRET_LABEL_API_KEY, SECRET_LENGTHS
from ops import testing

from conftest import migrations_exec

ADMIN_EMAIL = "ops-team@example.com"
# Juju secret URIs are "secret:" plus a 20-character identifier.
ADMIN_EMAIL_SECRET_ID = "secret:cvh7kruupa1s46bqvuig"


def cli_exec(return_code: int = 0, stdout: str = "{}", stderr: str = "") -> testing.Exec:
    """Return a fake exec result for Central's admin CLI."""
    return testing.Exec(
        command_prefix=["node", "./lib/bin/cli.js"],
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
    )


def purge_exec(return_code: int = 0, stdout: str = "purged 3 forms") -> testing.Exec:
    """Return a fake exec result for the purge job."""
    return testing.Exec(
        command_prefix=["node", "./lib/bin/purge.js"],
        return_code=return_code,
        stdout=stdout,
    )


def s3_exec(return_code: int = 0, stdout: str = "0") -> testing.Exec:
    """Return a fake exec result for the blob store job."""
    return testing.Exec(
        command_prefix=["node", "./lib/bin/s3.js"], return_code=return_code, stdout=stdout
    )


@pytest.fixture
def admin_email_secret() -> testing.Secret:
    """Return the operator-provided secret holding the administrator's address."""
    return testing.Secret(id=ADMIN_EMAIL_SECRET_ID, tracked_content={"email": ADMIN_EMAIL})


@pytest.fixture
def cli_service() -> testing.Container:
    """Return a service container where every CLI invocation succeeds."""
    return testing.Container(
        "service",
        can_connect=True,
        execs={migrations_exec(), cli_exec(), purge_exec(), s3_exec()},
    )


# create-admin


def test_create_admin_returns_the_password_once(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    admin_email_secret: testing.Secret,
) -> None:
    """The generated password is returned in the results and nowhere else."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets={admin_email_secret},
        leader=True,
        config={"admin-email-secret": ADMIN_EMAIL_SECRET_ID},
    )

    ctx.run(ctx.on.action("create-admin"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["email"] == ADMIN_EMAIL
    assert len(ctx.action_results["password"]) >= 16


def test_create_admin_needs_the_email_secret(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Without a configured address the action explains how to provide one."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("create-admin"), state_in)

    assert "add-secret" in excinfo.value.message


def test_create_admin_refuses_under_oidc(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    admin_email_secret: testing.Secret,
) -> None:
    """A password is meaningless when OIDC replaces password authentication."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets={admin_email_secret},
        leader=True,
        config={"admin-email-secret": ADMIN_EMAIL_SECRET_ID, "oidc-enabled": True},
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("create-admin"), state_in)

    assert "OpenID Connect" in excinfo.value.message
    # A clear message, not a CLI stack trace.
    assert "Traceback" not in excinfo.value.message


def test_create_admin_surfaces_a_cli_failure(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
    admin_email_secret: testing.Secret,
) -> None:
    """A user that already exists fails the action, not the charm."""
    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            migrations_exec(),
            cli_exec(return_code=1, stderr="duplicate key value violates unique constraint"),
        },
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets={admin_email_secret},
        leader=True,
        config={"admin-email-secret": ADMIN_EMAIL_SECRET_ID},
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("create-admin"), state_in)

    assert "duplicate key" in excinfo.value.message


# promote-user and reset-password


def test_promote_user(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Promotion reports the address it acted on."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("promote-user", params={"email": "someone@example.com"}), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["email"] == "someone@example.com"


def test_reset_password_returns_a_new_password(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The new password is returned once."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("reset-password", params={"email": "someone@example.com"}), state_in)

    assert ctx.action_results is not None
    assert len(ctx.action_results["password"]) >= 16


def test_reset_password_refuses_under_oidc(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Upstream's CLI throws here; the charm explains instead."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        leader=True,
        config={"oidc-enabled": True},
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("reset-password", params={"email": "a@example.com"}), state_in)

    assert "identity provider" in excinfo.value.message


# rotate-enketo-secrets


def test_rotation_changes_all_three_secrets(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """A partial rotation leaves every web form returning 403."""
    before = {s.label: s.latest_content["value"] for s in shared_secrets}
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.action("rotate-enketo-secrets"), state_in)

    after = {s.label: s.latest_content["value"] for s in state_out.secrets}
    for label in SECRET_LENGTHS:
        assert after[label] != before[label], label
        assert len(after[label].encode()) == SECRET_LENGTHS[label], label


def test_rotation_warns_about_the_user_visible_effect(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Operators should know sessions are dropped before they run it again."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=True,
    )

    ctx.run(ctx.on.action("rotate-enketo-secrets"), state_in)

    assert ctx.action_results is not None
    assert "start again" in ctx.action_results["warning"]


def test_rotation_reaches_the_enketo_relation(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Central must republish, or Enketo keeps the old key and every form 403s."""
    enketo = testing.Relation("odk-enketo", remote_app_name="enketo-k8s")
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql, enketo},
        secrets=shared_secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.action("rotate-enketo-secrets"), state_in)

    databag = state_out.get_relation(enketo.id).local_app_data
    assert databag["api-key-secret-id"]
    assert databag["encryption-key-secret-id"]
    assert databag["less-secure-key-secret-id"]
    assert databag["base-url"]


def test_rotation_needs_the_leader(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Only the leader owns the application secrets."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=False,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("rotate-enketo-secrets"), state_in)

    assert "leader" in excinfo.value.message


# run-migrations


def test_run_migrations_reports_success(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The action reports what the migration run printed."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("run-migrations"), state_in)

    assert ctx.action_results is not None
    assert "completed" in ctx.action_results["result"]


def test_run_migrations_reports_failure(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A failed migration fails the action with its output, not a charm error."""
    service = testing.Container(
        "service",
        can_connect=True,
        execs={migrations_exec(return_code=3, stdout="relation already exists")},
    )
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("run-migrations"), state_in)

    assert "exited 3" in excinfo.value.message
    assert "relation already exists" in excinfo.value.message


def test_run_migrations_needs_a_database(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
) -> None:
    """There is nothing to migrate without a database."""
    state_in = testing.State(containers={cli_service, nginx}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("run-migrations"), state_in)

    assert "postgresql" in excinfo.value.message


# upload-pending-blobs and purge-deleted


def test_upload_pending_blobs_is_a_clear_no_op_without_s3(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Attachments stay in PostgreSQL until there is somewhere to put them."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("upload-pending-blobs"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["result"] == "no-op"
    assert "s3-integrator" in ctx.action_results["note"]


def test_upload_pending_blobs_reports_how_many_moved(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count is the difference before and after, which is what operators want."""
    counts = iter([7, 0])
    monkeypatch.setattr(
        OdkCentralCharm, "_pending_blob_count", lambda self, container: next(counts)
    )
    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            migrations_exec(),
            testing.Exec(
                command_prefix=["node", "./lib/bin/s3.js", "upload-pending"],
                return_code=0,
                stdout="uploaded 7 blobs",
            ),
        },
    )
    s3 = testing.Relation(
        "s3",
        remote_app_name="s3-integrator",
        remote_app_data={
            "access-key": "AKIA",
            "secret-key": "shhh",
            "bucket": "odk-central",
            "endpoint": "http://minio.default.svc:9000",
        },
    )
    state_in = testing.State(containers={service, nginx}, relations={postgresql, s3}, leader=True)

    ctx.run(ctx.on.action("upload-pending-blobs"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["uploaded"] == 7
    assert ctx.action_results["pending"] == 0


def test_s3_relation_reaches_the_rendered_config(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """New blobs go to S3 as soon as the relation exists."""
    from conftest import rendered_config

    s3 = testing.Relation(
        "s3",
        remote_app_name="s3-integrator",
        remote_app_data={
            "access-key": "AKIA",
            "secret-key": "shhh",
            "bucket": "odk-central",
            "endpoint": "http://minio.default.svc:9000",
        },
    )
    state_in = testing.State(
        containers={cli_service, nginx}, relations={postgresql, s3}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    blob_store = rendered_config(state_out, ctx)["external"]["s3blobStore"]
    assert blob_store["server"] == "http://minio.default.svc:9000"
    assert blob_store["bucketName"] == "odk-central"
    assert blob_store["accessKey"] == "AKIA"


def test_purge_passes_force_through(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """force skips the retention window, so it must actually be forwarded."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("purge-deleted", params={"force": True}), state_in)

    purge_calls = [
        call
        for call in ctx.exec_history["service"]
        if call.command[:2] == ["node", "./lib/bin/purge.js"]
    ]
    assert purge_calls
    assert "--force" in purge_calls[0].command


def test_purge_defaults_to_the_retention_window(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Deleting things that are still recoverable must be opt-in."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    ctx.run(ctx.on.action("purge-deleted"), state_in)

    purge_calls = [
        call
        for call in ctx.exec_history["service"]
        if call.command[:2] == ["node", "./lib/bin/purge.js"]
    ]
    assert "--force" not in purge_calls[0].command


def test_no_secret_value_appears_in_the_rendered_config_permissions(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    admin_email_secret: testing.Secret,
) -> None:
    """The admin password is a Juju secret, never a config option."""
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets={admin_email_secret},
        leader=True,
        config={"admin-email-secret": ADMIN_EMAIL_SECRET_ID},
    )

    state_out = ctx.run(ctx.on.action("create-admin"), state_in)

    stored = {s.label for s in state_out.secrets}
    assert SECRET_LABEL_ADMIN_PASSWORD in stored
    assert "password" not in json.dumps(dict(state_out.config))


def test_a_node_stack_trace_is_reduced_to_its_error_line(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Operators need the error, not the bottom of a module-loader trace."""
    trace = (
        "/usr/odk/lib/task/task.js:41\n"
        "      throw err;\n"
        "      ^\n"
        "\n"
        "Error: Could not find the resource you were looking for.\n"
        "    at Module.load (node:internal/modules/cjs/loader:1577:32)\n"
        "    at Module._load (node:internal/modules/cjs/loader:1379:12)\n"
        "    at wrapModuleLoad (node:internal/modules/cjs/loader:255:19)\n"
    )
    service = testing.Container(
        "service",
        can_connect=True,
        execs={migrations_exec(), cli_exec(return_code=1, stderr=trace)},
    )
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("promote-user", params={"email": "nobody@example.com"}), state_in)

    assert "Could not find the resource" in excinfo.value.message
    assert "node:internal/modules" not in excinfo.value.message


# backup and restore


def s3_relation() -> testing.Relation:
    """Return a settled s3 relation."""
    return testing.Relation(
        "s3",
        remote_app_name="s3-integrator",
        remote_app_data={
            "access-key": "AKIA",
            "secret-key": "shhh",
            "bucket": "odk-central",
            "endpoint": "http://minio.default.svc:9000",
        },
    )


def pg_dump_exec(return_code: int = 0, stdout: str = "") -> testing.Exec:
    """Return a fake exec result for pg_dump."""
    return testing.Exec(command_prefix=["pg_dump"], return_code=return_code, stdout=stdout)


def test_backup_needs_an_s3_relation(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """There is nowhere to put a backup without one."""
    state_in = testing.State(containers={cli_service, nginx}, relations={postgresql}, leader=True)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("backup", params={"destination": "nightly"}), state_in)

    assert "s3-integrator" in excinfo.value.message


def test_backup_explains_the_postgres_version_mismatch(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The image ships postgresql-client-14 and cannot dump a newer server.

    This is not hypothetical: it is what happens against
    `postgresql-k8s --channel 16/stable`, and it breaks ODK Central's own
    /v1/backup endpoint for the same reason.
    """
    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            migrations_exec(),
            pg_dump_exec(
                return_code=1,
                stdout=(
                    "pg_dump: error: server version: 16.14; pg_dump version: 14.24\n"
                    "pg_dump: error: aborting because of server version mismatch"
                ),
            ),
        },
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, s3_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("backup", params={"destination": "nightly"}), state_in)

    assert "14/stable" in excinfo.value.message
    assert "create-backup" in excinfo.value.message


def test_backup_uploads_and_says_what_is_not_included(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """Blobs already in S3 are outside the dump, and operators must know."""
    uploads: list[tuple[str, str]] = []

    class FakeS3:
        def upload_fileobj(self, fileobj: object, bucket: str, key: str) -> None:
            uploads.append((bucket, key))

    monkeypatch.setattr(OdkCentralCharm, "_s3_client", lambda self, config: FakeS3())
    # pg_dump writes the file; the fake exec cannot, so it is placed here.
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    (tmp_dir / "central-backup.dump").write_bytes(b"PGDMP fake dump")
    service = testing.Container(
        "service",
        can_connect=True,
        execs={migrations_exec(), pg_dump_exec()},
        mounts={"tmp": testing.Mount(location="/tmp", source=tmp_dir)},
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, s3_relation()},
        leader=True,
    )

    ctx.run(ctx.on.action("backup", params={"destination": "nightly/"}), state_in)

    assert ctx.action_results is not None
    assert uploads and uploads[0][0] == "odk-central"
    assert uploads[0][1].startswith("nightly/")
    assert uploads[0][1].endswith("/central.dump")
    assert "NOT in this dump" in ctx.action_results["note"]


def test_restore_refuses_a_populated_database_without_force(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Overwriting live data has to be deliberate."""
    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            migrations_exec(),
            testing.Exec(command_prefix=["psql"], return_code=0, stdout="42\n"),
        },
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, s3_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("restore", params={"source": "nightly/20260101T000000Z"}), state_in)

    assert "force=true" in excinfo.value.message


def test_restore_refuses_when_it_cannot_check_the_database(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A check that cannot run must not be read as "the database is empty"."""
    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            migrations_exec(),
            testing.Exec(command_prefix=["psql"], return_code=2, stderr="could not connect"),
        },
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, s3_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed):
        ctx.run(ctx.on.action("restore", params={"source": "nightly/x"}), state_in)


def test_backup_fails_cleanly_if_the_dump_file_is_missing(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A pg_dump that exits 0 without writing anything must not raise."""
    service = testing.Container(
        "service", can_connect=True, execs={migrations_exec(), pg_dump_exec()}
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, s3_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("backup", params={"destination": "nightly"}), state_in)

    assert "no dump file" in excinfo.value.message


def test_rotation_renders_with_the_values_it_just_generated(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconcile must be handed the new secrets, not left to re-read them.

    A Juju secret revision written during a hook does not become current until
    that hook ends, so re-reading inside the rotation returns the values being
    replaced: Central would keep presenting the old key to an Enketo that
    already has the new one, and every web form would fail until some unrelated
    later event reconciled it.

    Asserted through the call rather than through the rendered file, because
    Scenario does not reproduce Juju's revision visibility -- it makes a new
    revision readable immediately, so a test against the output would pass
    whether or not the charm got this right. The behaviour itself is covered by
    tests/integration/test_secrets.py.
    """
    captured: dict[str, object] = {}
    original = OdkCentralCharm._reconcile

    def spy(self: OdkCentralCharm, **kwargs: object) -> None:
        captured.update(kwargs)
        original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(OdkCentralCharm, "_reconcile", spy)
    state_in = testing.State(
        containers={cli_service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.action("rotate-enketo-secrets"), state_in)

    rotated = next(
        s.latest_content["value"] for s in state_out.secrets if s.label == SECRET_LABEL_API_KEY
    )
    passed = captured.get("shared_secrets")
    assert passed is not None, "the reconcile was left to re-read the secrets"
    assert passed.api_key == rotated  # type: ignore[union-attr]


def test_rotation_changes_the_relation_databag(
    ctx: testing.Context[OdkCentralCharm],
    cli_service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Rotating must be visible to Enketo as a relation change.

    A rotation does not change the secret IDs, so a databag holding only IDs is
    byte-identical before and after one and Enketo gets no relation-changed at
    all. That leaves secret-changed as the single delivery carrying the
    rotation, and if it is missed the two ends disagree permanently: Central
    presents the new key to an Enketo still holding the old one, every request
    is rejected, and no new form gets an Enketo id -- while both applications
    go on reporting themselves perfectly healthy.
    """
    enketo = testing.Relation("odk-enketo", remote_app_name="enketo-k8s")
    state = testing.State(
        containers={cli_service, nginx},
        relations={postgresql, enketo},
        secrets=shared_secrets,
        leader=True,
    )

    settled = ctx.run(ctx.on.config_changed(), state)
    before = dict(settled.get_relation(enketo.id).local_app_data)

    rotated = ctx.run(ctx.on.action("rotate-enketo-secrets"), settled)
    after = dict(rotated.get_relation(enketo.id).local_app_data)

    assert after != before, "Enketo has no way to notice this rotation"
