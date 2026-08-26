"""Unit tests for the odk-central-k8s charm."""

from __future__ import annotations

import json
from typing import Any

import pytest
from charm import ENKETO_URL_PLACEHOLDER, HEALTH_PATH, OdkCentralCharm, generate_secret
from charms.odk_central_k8s.v0.odk_enketo import (
    SECRET_LABEL_API_KEY,
    SECRET_LABEL_ENCRYPTION_KEY,
    SECRET_LABEL_LESS_SECURE_KEY,
)
from ops import testing

from conftest import (
    DB_ENDPOINTS,
    config_file,
    container_named,
    migrations_exec,
    rendered_config,
)

# Startup ordering


def test_waits_for_the_service_container(
    ctx: testing.Context[OdkCentralCharm], nginx: testing.Container
) -> None:
    """Before Pebble is reachable the charm waits rather than erroring."""
    state_in = testing.State(
        containers={testing.Container("service", can_connect=False), nginx}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)


def test_waits_for_the_postgresql_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
) -> None:
    """Central cannot start without a database and says so."""
    state_in = testing.State(containers={service, nginx}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
    assert "postgresql" in state_out.unit_status.message


def test_waits_for_an_incomplete_postgresql_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
) -> None:
    """A related but not yet provisioned database is a wait, not a block."""
    relation = testing.Relation("postgresql", remote_app_data={"endpoints": DB_ENDPOINTS})
    state_in = testing.State(containers={service, nginx}, relations={relation}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)


def test_follower_waits_for_the_leader_to_generate_secrets(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A follower cannot create application secrets and must wait for the leader."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=False)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
    assert "leader" in state_out.unit_status.message


def test_migrations_run_then_the_service_starts(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """With a database, migrations run and the API service is started."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    container = container_named(state_out, "service")
    assert container.service_statuses["service"] == testing.pebble.ServiceStatus.ACTIVE


def test_failed_migrations_block_and_do_not_start_the_service(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A migration failure blocks rather than starting an API against a bad schema."""
    service = testing.Container(
        "service",
        can_connect=True,
        execs={migrations_exec(return_code=1, stdout="relation already exists")},
    )
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "migration" in state_out.unit_status.message
    container = container_named(state_out, "service")
    assert "service" not in container.service_statuses


def test_the_api_port_is_opened(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Port 8383 is opened for the API."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert testing.TCPPort(8383) in state_out.opened_ports


# Shared secrets


def test_secrets_are_generated_at_the_exact_required_lengths(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Enketo asserts on 128, 64 and 32 bytes; anything else aborts its startup."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    by_label = {s.label: s for s in state_out.secrets}
    assert len(by_label[SECRET_LABEL_API_KEY].latest_content["value"].encode()) == 128
    assert len(by_label[SECRET_LABEL_ENCRYPTION_KEY].latest_content["value"].encode()) == 64
    assert len(by_label[SECRET_LABEL_LESS_SECURE_KEY].latest_content["value"].encode()) == 32


def test_secrets_are_reused_not_regenerated(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Re-running a hook must not rotate the secrets under Enketo's feet."""
    before = {s.label: s.latest_content["value"] for s in shared_secrets}
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    after = {s.label: s.latest_content["value"] for s in state_out.secrets}
    assert after == before


def test_generate_secret_produces_the_requested_byte_length() -> None:
    """The generator is ASCII-only, so characters and bytes agree."""
    for length in (32, 64, 128):
        value = generate_secret(length)
        assert len(value) == length
        assert len(value.encode()) == length


# The rendered configuration


def test_database_stanza_comes_from_the_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The relation's endpoint and credentials land in config.json."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"db-pool-size": 17},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    database = rendered_config(state_out, ctx)["database"]
    assert database["host"] == "postgresql-k8s-primary.odk.svc.cluster.local"
    assert database["port"] == 5432
    assert database["user"] == "relation-7"
    assert database["password"] == "hunter2"
    assert database["database"] == "odk"
    assert database["maximumPoolSize"] == 17


def test_database_stanza_has_no_keys_central_backend_rejects(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """central-backend throws on any unrecognised key under `database`."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    allowed = {"host", "port", "user", "password", "database", "maximumPoolSize"}
    assert set(rendered_config(state_out, ctx)["database"]) <= allowed


def test_sentry_is_blank_by_default(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Upstream's shipped defaults are ODK's own project and must never leak in."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    sentry = rendered_config(state_out, ctx)["external"]["sentry"]
    assert all(value == "" for value in sentry.values())
    serialised = json.dumps(sentry)
    assert "o130137" not in serialised
    assert "3cf75f54983e473da6bd07daddf0d2ee" not in serialised
    assert "1298632" not in serialised


def test_operator_sentry_dsn_is_used_when_set(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """An operator's own DSN is honoured."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"error-reporting-dsn": "https://abc@sentry.example.com/42"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["external"]["sentry"]["dsn"].endswith("/42")


def test_enketo_url_starts_as_a_placeholder_with_the_real_api_key(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    shared_secrets: set[testing.Secret],
) -> None:
    """Central starts before Enketo exists, but already knows the API key.

    Waiting for Enketo here would deadlock: Enketo cannot start until Central
    has published the secrets.
    """
    expected = next(
        s.latest_content["value"] for s in shared_secrets if s.label == SECRET_LABEL_API_KEY
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets=shared_secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    enketo = rendered_config(state_out, ctx)["enketo"]
    assert enketo["url"] == ENKETO_URL_PLACEHOLDER
    assert enketo["apiKey"] == expected


def test_xlsform_stanza_comes_from_the_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """pyxform's advertised address is written into config.json."""
    xlsform = testing.Relation(
        "xlsform",
        remote_app_name="pyxform-k8s",
        remote_app_data={"host": "pyxform-k8s.odk.svc.cluster.local", "port": "80"},
    )
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, xlsform}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["xlsform"] == {
        "host": "pyxform-k8s.odk.svc.cluster.local",
        "port": 80,
    }


def test_status_warns_when_there_is_no_xlsform_relation(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Central runs without pyxform, but publishing a form will fail."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    assert "xlsform" in state_out.unit_status.message


def test_base_url_uses_the_configured_hostname(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """external-hostname becomes the domain Central advertises."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "odk.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["env"]["domain"] == "https://odk.example.com"


def test_email_sender_defaults_to_the_hostname(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """With no email-from set, the sender is derived from the public hostname."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "odk.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["email"]["serviceAccount"] == "no-reply@odk.example.com"


def test_config_file_is_not_world_readable(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """config.json holds the database password and the Enketo API key."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert (config_file(state_out, ctx).stat().st_mode & 0o077) == 0


# Health check and configuration validation


def test_health_check_uses_an_endpoint_that_exists(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """There is no /v1/version.json in central-backend; the check must not use one."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    container = container_named(state_out, "service")
    check = container.layers["service"].checks["api-up"]
    assert check.http is not None
    assert check.http["url"].endswith(HEALTH_PATH)
    assert "version.json" not in check.http["url"]


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"log-level": "LOUD"}, "log-level"),
        ({"db-pool-size": 0}, "db-pool-size"),
        ({"session-lifetime": 0}, "session-lifetime"),
    ],
)
def test_invalid_config_blocks(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    config: dict[str, Any],
    expected: str,
) -> None:
    """Invalid configuration blocks with a message naming the offending option."""
    state_in = testing.State(containers={service, nginx}, config=config, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert expected in state_out.unit_status.message


def test_no_pg_environment_variables_are_set(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """A PG* variable in the environment silently overrides config.json."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    container = container_named(state_out, "service")
    environment = container.layers["service"].services["service"].environment
    assert not [key for key in environment if key.startswith("PG")]
