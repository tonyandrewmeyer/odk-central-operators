"""Unit tests for the edges: partial relation data, and things going wrong."""

from __future__ import annotations

import charm as charm_module
import ops
import pytest
from charm import ENKETO_URL_PLACEHOLDER, SECRET_LABEL_ADMIN_PASSWORD, OdkCentralCharm
from charms.odk_central_k8s.v0.odk_enketo import FIELD_ENKETO_URL
from ops import testing

from conftest import OIDC_SECRET_ID, nginx_environment, rendered_config


def test_status_reports_blobs_waiting_to_move(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partially migrated blob store can stay that way indefinitely."""
    monkeypatch.setattr(OdkCentralCharm, "_pending_blob_count", lambda self, container: 12)
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

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "12 blobs pending upload to s3" in state_out.unit_status.message


def test_an_incomplete_s3_relation_is_ignored(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Half a set of credentials is not a blob store."""
    s3 = testing.Relation(
        "s3", remote_app_name="s3-integrator", remote_app_data={"bucket": "odk-central"}
    )
    state_in = testing.State(containers={service, nginx}, relations={postgresql, s3}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["external"]["s3blobStore"]["server"] == ""


def test_enketo_url_and_nginx_upstream_follow_the_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Once Enketo answers, both Central and nginx point at the real address."""
    enketo = testing.Relation(
        "odk-enketo",
        remote_app_name="enketo-k8s",
        remote_app_data={FIELD_ENKETO_URL: "http://enketo-k8s.odk.svc.cluster.local:8005/-"},
    )
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, enketo}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = rendered_config(state_out, ctx)
    assert config["enketo"]["url"] == "http://enketo-k8s.odk.svc.cluster.local:8005/-"
    assert config["enketo"]["url"] != ENKETO_URL_PLACEHOLDER
    upstream = nginx_environment(state_out)["ENKETO_UPSTREAM"]
    assert upstream == "enketo-k8s.odk.svc.cluster.local:8005"


def test_nginx_upstream_stays_local_while_enketo_is_absent(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """nginx has to load a valid config whether or not Enketo is related.

    A hostname it cannot resolve would stop nginx starting at all, taking the
    whole web interface down because web forms are unavailable.
    """
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["ENKETO_UPSTREAM"] == "127.0.0.1:8005"


def test_waiting_when_the_api_never_becomes_healthy(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    api_is_unhealthy: None,
) -> None:
    """The charm waits and lets the next event retry, rather than erroring."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
    assert "healthy" in state_out.unit_status.message


def test_create_admin_needs_the_leader(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The password is an application secret, which only the leader owns."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=False)

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("create-admin"), state_in)

    assert "leader" in excinfo.value.message


def test_an_unreadable_admin_email_secret_is_reported(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A secret that was configured but never granted must not raise."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"admin-email-secret": "secret:h4k2m9p1qr7st3uv6wxy"},
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("create-admin"), state_in)

    assert "add-secret" in excinfo.value.message


def test_an_existing_admin_password_is_reused(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Re-running create-admin must not silently change a stored password."""
    from test_actions import ADMIN_EMAIL_SECRET_ID, cli_exec

    service = testing.Container(
        "service",
        can_connect=True,
        execs={
            testing.Exec(command_prefix=["node", "./lib/bin/run-migrations"], return_code=0),
            cli_exec(),
        },
    )
    existing = testing.Secret(
        tracked_content={"value": "already-chosen-password"},
        label=SECRET_LABEL_ADMIN_PASSWORD,
        owner="app",
    )
    email = testing.Secret(id=ADMIN_EMAIL_SECRET_ID, tracked_content={"email": "ops@example.com"})
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets={existing, email},
        leader=True,
        config={"admin-email-secret": ADMIN_EMAIL_SECRET_ID},
    )

    ctx.run(ctx.on.action("create-admin"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["password"] == "already-chosen-password"


def test_an_unreadable_oidc_secret_blocks_rather_than_raising(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A client secret that was configured but never granted is a config error."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={
            "oidc-enabled": True,
            "oidc-issuer-url": "https://idp.example.com",
            "oidc-client-id": "odk-central",
            "oidc-client-secret": OIDC_SECRET_ID,
        },
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)


def test_unusable_smtp_relation_data_does_not_raise(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The library validates strictly; a partial databag must not kill the charm."""
    smtp = testing.Relation(
        "smtp", remote_app_name="smtp-integrator", remote_app_data={"host": ""}
    )
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, smtp}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    # Falls back to the local relay that is not there, as with no relation.
    assert rendered_config(state_out, ctx)["email"]["transportOpts"]["host"] == "localhost"


def test_generate_secret_refuses_to_return_the_wrong_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The belt-and-braces check exists because the length is load-bearing."""
    monkeypatch.setattr(charm_module.secrets, "choice", lambda alphabet: "é")

    with pytest.raises(RuntimeError, match="wanted 32"):
        charm_module.generate_secret(32)


def test_stop_if_running_tolerates_a_service_that_never_started(
    ctx: testing.Context[OdkCentralCharm],
) -> None:
    """Stopping something that is not there is not an error."""
    container = testing.Container("service", can_connect=True)
    state = testing.State(containers={container})

    with ctx(ctx.on.update_status(), state) as manager:
        charm_module.stop_if_running(manager.charm.unit.get_container("service"), "service")


def test_stop_if_running_ignores_an_unreachable_container(
    ctx: testing.Context[OdkCentralCharm],
) -> None:
    """A container that cannot be reached has nothing running to stop."""
    container = testing.Container("service", can_connect=False)
    state = testing.State(containers={container})

    with ctx(ctx.on.update_status(), state) as manager:
        charm_module.stop_if_running(manager.charm.unit.get_container("service"), "service")


def test_reason_reports_the_exit_code_when_there_is_no_output() -> None:
    """A failed exec with nothing to say still tells you what it exited with."""
    error = ops.pebble.ExecError(["true"], 7, "", "")

    assert "7" in charm_module._reason(error)


def test_reason_survives_a_trace_that_is_only_stack_frames() -> None:
    """Dropping every line must not leave an empty message."""
    frames = "    at Module.load (node:internal)\n    at Module._load (node:internal)\n"
    error = ops.pebble.ExecError(["node"], 3, "", frames)

    assert charm_module._reason(error).strip()
