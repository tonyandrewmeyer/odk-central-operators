"""Unit tests for the nginx frontend proxy and ingress wiring."""

from __future__ import annotations

import json
import pathlib

import ops
import pytest
from charm import OdkCentralCharm
from ops import testing

from conftest import (
    CLIENT_CONFIG,
    NGINX_CONF,
    NGINX_TEMPLATE_DIR,
    OIDC_SECRET_ID,
    container_named,
    nginx_environment,
    pushed_to_nginx,
    rendered_config,
)


def nginx_container(state: testing.State) -> testing.Container:
    """Return the nginx container from a resulting state."""
    return container_named(state, "nginx")


# Startup ordering


def test_nginx_does_not_start_while_the_api_is_unhealthy(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    api_is_unhealthy: None,
) -> None:
    """nginx proxies to the API and fails its own start if the API is absent.

    Pebble dependencies do not cross container boundaries, so this ordering has
    to be enforced by the charm.
    """
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.WaitingStatus)
    assert "nginx" not in nginx_container(state_out).service_statuses


def test_nginx_starts_once_the_api_is_healthy(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """With a healthy API, the frontend proxy is started."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    statuses = nginx_container(state_out).service_statuses
    assert statuses["nginx"] == testing.pebble.ServiceStatus.ACTIVE


def test_nginx_uses_the_image_entrypoint(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """setup-odk.sh templates the config and client-config.json before exec'ing nginx."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    layer = nginx_container(state_out).layers["nginx"]
    assert layer.services["nginx"].command == "/scripts/setup-odk.sh"


# Templates the image does not contain


def test_both_templates_are_pushed(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The published image ships neither template; upstream bind-mounts them in."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "server_name" in pushed_to_nginx(state_out, ctx, NGINX_CONF)
    assert "oidcEnabled" in pushed_to_nginx(state_out, ctx, CLIENT_CONFIG)


def test_proxy_targets_are_charm_controlled(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Upstream's hardcoded hostnames would never resolve under Juju."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    conf = pushed_to_nginx(state_out, ctx, NGINX_CONF)
    # `service` is in the same pod, so it is reachable on localhost.
    assert "proxy_pass http://127.0.0.1:8383;" in conf
    assert "proxy_pass http://service:8383;" not in conf
    # `enketo` is a separate Juju application, supplied over the relation.
    assert "proxy_pass http://${ENKETO_UPSTREAM};" in conf
    assert "proxy_pass http://enketo:8005;" not in conf


def test_csp_report_is_neutralised_when_no_dsn_is_configured(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Blank Sentry values would render an unloadable proxy_pass.

    `https://${SENTRY_ORG_SUBDOMAIN}.ingest.sentry.io/api/${SENTRY_PROJECT}/`
    becomes `https://.ingest.sentry.io/api//` and nginx refuses the config.
    """
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    conf = pushed_to_nginx(state_out, ctx, NGINX_CONF)
    # The Sentry report endpoint is the only https proxy_pass in the file.
    assert "proxy_pass https://" not in conf
    assert "return 204;" in conf


def test_csp_report_is_kept_when_a_dsn_is_configured(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """An operator with their own DSN keeps upstream's reporting behaviour."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"error-reporting-dsn": "https://abc@sentry.example.com/42"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "proxy_pass https://" in pushed_to_nginx(state_out, ctx, NGINX_CONF)


# Environment


def test_tls_terminates_at_the_ingress(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """SSL_TYPE=upstream makes nginx serve HTTP and trust X-Forwarded-Proto."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["SSL_TYPE"] == "upstream"


def test_upstream_sentry_defaults_never_reach_nginx(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Upstream's compose defaults are the ODK project's own Sentry credentials."""
    state_in = testing.State(containers={service, nginx}, relations={postgresql}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    environment = nginx_environment(state_out)
    assert environment["SENTRY_ORG_SUBDOMAIN"] == ""
    assert environment["SENTRY_KEY"] == ""
    assert environment["SENTRY_PROJECT"] == ""
    assert environment["SENTRY_DSN_FRONTEND"] == ""
    serialised = json.dumps(environment)
    assert "o130137" not in serialised
    assert "3cf75f54983e473da6bd07daddf0d2ee" not in serialised


def test_oidc_flag_is_not_advertised_until_it_works(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Setting oidc-enabled alone must not offer a login that cannot work.

    The browser reads oidcEnabled from client-config.json at page load, so
    advertising OIDC before a provider is configured would replace the password
    form with a button that goes nowhere.
    """
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"oidc-enabled": True},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "oidc-enabled is set but no usable provider" in state_out.unit_status.message
    assert nginx_environment(state_out)["OIDC_ENABLED"] == "false"


def test_oidc_flag_reaches_the_frontend_when_configured(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    oidc_client_secret: testing.Secret,
) -> None:
    """With a complete configuration the frontend switches to OIDC."""
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

    assert nginx_environment(state_out)["OIDC_ENABLED"] == "true"
    oidc = rendered_config(state_out, ctx)["oidc"]
    assert oidc["enabled"] is True
    assert oidc["issuerUrl"] == "https://idp.example.com"
    assert oidc["clientId"] == "odk-central"
    assert oidc["clientSecret"] == "sssh"


# The public hostname


def test_ingress_url_becomes_the_domain(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    ingress: testing.Relation,
) -> None:
    """The ingress URL propagates to both nginx and Central's own config."""
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, ingress}, leader=True
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["DOMAIN"] == "odk.ingress.example"
    # No trailing slash: Central concatenates onto env.domain without
    # normalising, and would otherwise redirect to "https://host//login".
    assert rendered_config(state_out, ctx)["env"]["domain"] == "https://odk.ingress.example"


def test_configured_hostname_overrides_the_ingress(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    ingress: testing.Relation,
) -> None:
    """An operator can point Central at the name their users actually type."""
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql, ingress},
        leader=True,
        config={"external-hostname": "odk.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["DOMAIN"] == "odk.example.com"
    assert rendered_config(state_out, ctx)["env"]["domain"] == "https://odk.example.com"


def test_ingress_requests_the_nginx_port(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
    ingress: testing.Relation,
) -> None:
    """Ingress must front nginx, not the API: they have to be same-origin."""
    state_in = testing.State(
        containers={service, nginx}, relations={postgresql, ingress}, leader=True
    )

    # The ingress requirer publishes on relation-changed, not relation-joined.
    state_out = ctx.run(ctx.on.relation_changed(ingress), state_in)

    databag = state_out.get_relation(ingress.id).local_app_data
    assert databag["port"] == "80"


def _running_nginx(environment: dict[str, str]) -> testing.Container:
    """Return an nginx container already running with ``environment``."""
    return testing.Container(
        "nginx",
        can_connect=True,
        layers={
            "nginx": testing.pebble.Layer(
                {
                    "services": {
                        "nginx": {
                            "override": "replace",
                            "command": "/scripts/setup-odk.sh",
                            "startup": "disabled",
                            "environment": environment,
                        }
                    }
                }
            )
        },
        service_statuses={"nginx": testing.pebble.ServiceStatus.ACTIVE},
    )


def test_changing_the_hostname_restarts_nginx(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    postgresql: testing.Relation,
    restarts: list[str],
) -> None:
    """client-config.json is regenerated by the entrypoint, not by a reload.

    A bare `nginx -s reload` would leave the browser reading the old origin, so
    the service is restarted to re-run the templating step.
    """
    nginx = _running_nginx({"DOMAIN": "old.example.com"})
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "new.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["DOMAIN"] == "new.example.com"
    assert "nginx" in restarts


def test_unchanged_configuration_does_not_bounce_nginx(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    postgresql: testing.Relation,
    restarts: list[str],
    tmp_path: pathlib.Path,
) -> None:
    """An unrelated hook must not drop the frontend for no reason.

    The template directory is a real mount so that the files the first run
    pushes are still there for the second; Scenario gives each run a fresh
    mock filesystem otherwise.
    """
    template_dir = tmp_path / "nginx-templates"
    template_dir.mkdir()
    mounts = {"templates": testing.Mount(location=NGINX_TEMPLATE_DIR, source=template_dir)}

    state = testing.State(
        containers={service, testing.Container("nginx", can_connect=True, mounts=mounts)},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "odk.example.com"},
    )
    state = ctx.run(ctx.on.config_changed(), state)

    settled = testing.Container(
        "nginx",
        can_connect=True,
        mounts=mounts,
        layers=dict(container_named(state, "nginx").layers),
        service_statuses={"nginx": testing.pebble.ServiceStatus.ACTIVE},
    )
    restarts.clear()

    ctx.run(
        ctx.on.update_status(),
        testing.State(
            containers={service, settled},
            relations={postgresql},
            leader=True,
            config={"external-hostname": "odk.example.com"},
        ),
    )

    # Scoped to nginx: the service container has no mounts in this test, so its
    # config.json looks new on every run.
    assert "nginx" not in restarts


def test_nginx_health_check_does_not_depend_on_the_host_header(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """Upstream's catch-all server block answers 421 to an unrecognised Host.

    Pebble cannot override the Host header of an http check -- in Go it comes
    from the URL, not from the headers -- so an http check against localhost
    would start failing the moment a real hostname was configured. This is the
    same port check upstream's own compose healthcheck uses.
    """
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "odk.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    check = container_named(state_out, "nginx").layers["nginx"].checks["nginx-up"]
    assert check.http is None
    assert check.exec is not None
    assert check.exec["command"] == "nc -z localhost 80"


def test_long_hostnames_do_not_break_nginx(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """nginx sizes its server-name hash from the longest name it serves.

    The default of 64 bytes is not enough for the hostnames Juju produces --
    "<model>-<application>.<ingress-host>" -- and nginx refuses to start with
    "could not build server_names_hash" rather than truncating. Upstream never
    hits this because a compose deployment uses a short, human-chosen domain.
    """
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "some-long-model-name-odk-central-k8s.10-43-45-0.nip.io"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "server_names_hash_bucket_size 128;" in pushed_to_nginx(state_out, ctx, NGINX_CONF)


def test_a_workload_that_will_not_start_blocks_rather_than_erroring(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    postgresql: testing.Relation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid generated config is an operator problem, not a traceback."""

    def refuse(self: object, *service_names: str) -> None:
        raise testing.pebble.ChangeError("nginx: [emerg] bad config", change=None)

    monkeypatch.setattr(ops.Container, "start", refuse)
    state_in = testing.State(
        containers={service, testing.Container("nginx", can_connect=True)},
        relations={postgresql},
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "nginx did not start" in state_out.unit_status.message
