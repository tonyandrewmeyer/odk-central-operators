"""Unit tests for the nginx frontend proxy and ingress wiring."""

from __future__ import annotations

import json

from charm import OdkCentralCharm
from ops import testing

from conftest import (
    CLIENT_CONFIG,
    NGINX_CONF,
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


def test_oidc_flag_reaches_the_frontend_config(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    nginx: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """The browser reads oidcEnabled from client-config.json at page load.

    Without this the frontend offers a password form that cannot work.
    """
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"oidc-enabled": True},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert nginx_environment(state_out)["OIDC_ENABLED"] == "true"


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
    assert rendered_config(state_out, ctx)["env"]["domain"] == "https://odk.ingress.example/"


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


def test_changing_the_hostname_restarts_nginx(
    ctx: testing.Context[OdkCentralCharm],
    service: testing.Container,
    postgresql: testing.Relation,
) -> None:
    """client-config.json is regenerated by the entrypoint, not by a reload.

    A bare `nginx -s reload` would leave the browser reading the old origin, so
    the service is restarted to re-run the templating step.
    """
    nginx = testing.Container(
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
                            "environment": {"DOMAIN": "old.example.com"},
                        }
                    }
                }
            )
        },
        service_statuses={"nginx": testing.pebble.ServiceStatus.ACTIVE},
    )
    state_in = testing.State(
        containers={service, nginx},
        relations={postgresql},
        leader=True,
        config={"external-hostname": "new.example.com"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    environment = nginx_environment(state_out)
    assert environment["DOMAIN"] == "new.example.com"
    assert nginx_container(state_out).service_statuses["nginx"] == (
        testing.pebble.ServiceStatus.ACTIVE
    )
