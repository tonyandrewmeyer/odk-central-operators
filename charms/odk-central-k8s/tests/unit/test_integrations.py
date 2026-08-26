"""Unit tests for the SMTP and OpenID Connect integrations."""

from __future__ import annotations

import dataclasses
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest
from charm import HEALTH_PATH, OdkCentralCharm
from ops import testing

from conftest import OIDC_SECRET_ID, container_named, nginx_environment, rendered_config


def smtp_relation(**overrides: str) -> testing.Relation:
    """Return a settled smtp relation from smtp-integrator."""
    data = {
        "host": "smtp.example.com",
        "port": "587",
        "user": "odk",
        "password": "hunter2",
        "auth_type": "plain",
        "transport_security": "starttls",
        "domain": "example.com",
    }
    data.update(overrides)
    return testing.Relation("smtp", remote_app_name="smtp-integrator", remote_app_data=data)


# SMTP


def test_without_smtp_central_still_reaches_active(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Mail failure is a per-operation error upstream, not a startup one."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    assert "account email will not be sent" in state_out.unit_status.message


def test_smtp_relation_reaches_the_transport(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The relay's host, port and credentials land in config.json."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, smtp_relation()},
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    transport = rendered_config(state_out, ctx)["email"]["transportOpts"]
    assert transport["host"] == "smtp.example.com"
    assert transport["port"] == 587
    assert transport["auth"] == {"user": "odk", "pass": "hunter2"}


@pytest.mark.parametrize(
    ("security", "secure", "ignore_tls"),
    [
        # Implicit TLS on connect.
        ("tls", True, False),
        # Negotiated after connecting in the clear: not the same thing.
        ("starttls", False, False),
        ("none", False, True),
    ],
)
def test_transport_security_maps_onto_nodemailer(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    security: str,
    secure: bool,
    ignore_tls: bool,
) -> None:
    """nodemailer's "secure" means implicit TLS, not STARTTLS."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, smtp_relation(transport_security=security)},
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    transport = rendered_config(state_out, ctx)["email"]["transportOpts"]
    assert transport["secure"] is secure
    assert transport["ignoreTLS"] is ignore_tls


def test_email_sender_uses_the_public_domain(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    ingress: testing.Relation,
) -> None:
    """A From address at the ingress hostname is more likely to be deliverable."""
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, ingress}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    account = rendered_config(state_out, ctx)["email"]["serviceAccount"]
    assert account == "no-reply@odk.ingress.example"


def test_env_domain_has_no_trailing_slash(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    ingress: testing.Relation,
) -> None:
    """Traefik advertises a trailing slash; Central concatenates onto it.

    Left in place it produces redirects to "https://host//login", and an OIDC
    redirect URI that will not match what was registered with the provider.
    """
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, ingress}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert not rendered_config(state_out, ctx)["env"]["domain"].endswith("/")


# OpenID Connect


def _with_oauth_provider(
    monkeypatch: pytest.MonkeyPatch, issuer: str, client_id: str, secret: str
) -> None:
    """Make the oauth relation look settled, without modelling hydra's databag."""
    provider = SimpleNamespace(issuer_url=issuer, client_id=client_id, client_secret=secret)
    monkeypatch.setattr(OdkCentralCharm, "_oauth_provider", lambda self: provider)


def test_oidc_disabled_by_default(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Password authentication is the default."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert rendered_config(state_out, ctx)["oidc"]["enabled"] is False
    assert nginx_environment(state_out)["OIDC_ENABLED"] == "false"


def test_relation_data_wins_over_config(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    oidc_client_secret: testing.Secret,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With both present the relation is authoritative, and the status says so."""
    _with_oauth_provider(
        monkeypatch, "https://hydra.example.com", "from-relation", "relation-secret"
    )
    oauth = testing.Relation("oauth", remote_app_name="hydra")
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, oauth},
        secrets={oidc_client_secret},
        leader=True,
        config={
            "oidc-enabled": True,
            "oidc-issuer-url": "https://from-config.example.com",
            "oidc-client-id": "from-config",
            "oidc-client-secret": OIDC_SECRET_ID,
        },
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    oidc = rendered_config(state_out, ctx)["oidc"]
    assert oidc["issuerUrl"] == "https://hydra.example.com"
    assert oidc["clientId"] == "from-relation"
    assert "oidc via oauth relation" in state_out.unit_status.message


def test_status_says_when_oidc_came_from_config(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    oidc_client_secret: testing.Secret,
) -> None:
    """Operators should not have to guess which source is in effect."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets={oidc_client_secret},
        leader=True,
        config={
            "oidc-enabled": True,
            "oidc-issuer-url": "https://idp.example.com",
            "oidc-client-id": "odk-central",
            "oidc-client-secret": OIDC_SECRET_ID,
        },
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "oidc via config" in state_out.unit_status.message
    assert "password login disabled" in state_out.unit_status.message


def test_health_check_is_unaffected_by_oidc(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    oidc_client_secret: testing.Secret,
) -> None:
    """The check must stay on an unauthenticated endpoint.

    A check against anything that creates a session would fail permanently the
    moment OIDC is enabled, because OIDC disables session creation.
    """
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        secrets={oidc_client_secret},
        leader=True,
        config={
            "oidc-enabled": True,
            "oidc-issuer-url": "https://idp.example.com",
            "oidc-client-id": "odk-central",
            "oidc-client-secret": OIDC_SECRET_ID,
        },
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    check = container_named(state_out, "service").layers["service"].checks["api-up"]
    assert check.http is not None
    assert check.http["url"].endswith(HEALTH_PATH)
    assert "session" not in check.http["url"]


@pytest.mark.parametrize(
    ("config", "missing"),
    [
        ({"oidc-issuer-url": "https://idp.example.com"}, "client id and secret"),
        ({"oidc-client-id": "odk-central"}, "issuer and secret"),
    ],
)
def test_partial_oidc_config_blocks(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    config: dict[str, Any],
    missing: str,
) -> None:
    """Half a configuration is not a working login."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"oidc-enabled": True, **config},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)


# Configuration changes have to reach the running workload


def _service_mounts(tmp_path: pathlib.Path) -> dict[str, testing.Mount]:
    """Return a mount making config.json survive between runs."""
    root = tmp_path / "odk-config"
    root.mkdir()
    return {"config": testing.Mount(location="/usr/odk/config", source=root)}


def _running_service(mounts: dict[str, testing.Mount]) -> testing.Container:
    """Return a service container that is already running."""
    return testing.Container(
        "service",
        can_connect=True,
        mounts=mounts,
        execs={testing.Exec(command_prefix=["node", "./lib/bin/run-migrations"], return_code=0)},
        layers={
            "service": testing.pebble.Layer(
                {
                    "services": {
                        "service": {
                            "override": "replace",
                            "command": "npx --no pm2-runtime ./pm2.config.js",
                            "startup": "enabled",
                        }
                    }
                }
            )
        },
        service_statuses={"service": testing.pebble.ServiceStatus.ACTIVE},
    )


def test_a_new_smtp_relay_restarts_the_api(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
    restarts: list[str],
    tmp_path: pathlib.Path,
) -> None:
    """Central reads config.json once, at startup.

    Pebble's replan only restarts a service whose layer changed, so without an
    explicit restart a new relay, blob store, issuer or enketo.url would sit on
    disk while the running workload kept using what it started with.
    """
    mounts = _service_mounts(tmp_path)
    state = testing.State(
        containers={_running_service(mounts), nginx},
        relations={postgresql},
        leader=True,
    )
    state = ctx.run(ctx.on.config_changed(), state)
    restarts.clear()

    # Carry the resulting state forward so the generated secrets are stable and
    # the only difference is the new relation.
    ctx.run(
        ctx.on.config_changed(),
        dataclasses.replace(state, relations={postgresql, smtp_relation()}),
    )

    assert "service" in restarts


def test_an_unchanged_configuration_does_not_restart_the_api(
    ctx: testing.Context[OdkCentralCharm],
    nginx: testing.Container,
    postgresql: testing.Relation,
    restarts: list[str],
    tmp_path: pathlib.Path,
) -> None:
    """An unrelated hook must not drop the API for no reason."""
    mounts = _service_mounts(tmp_path)
    state = testing.State(
        containers={_running_service(mounts), nginx},
        relations={postgresql},
        leader=True,
    )
    state = ctx.run(ctx.on.config_changed(), state)
    restarts.clear()

    ctx.run(ctx.on.update_status(), state)

    assert "service" not in restarts
