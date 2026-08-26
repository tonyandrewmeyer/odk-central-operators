#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for ODK Central: the API service and the nginx frontend proxy."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import re
import secrets
import string
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import ops
import requests
from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseEndpointsChangedEvent,
    DatabaseRequires,
)
from charms.data_platform_libs.v0.s3 import S3Requirer
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.hydra.v0.oauth import ClientConfig, OAuthRequirer
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.odk_central_k8s.v0.odk_enketo import (
    ENKETO_PORT,
    SECRET_LABEL_API_KEY,
    SECRET_LABEL_ENCRYPTION_KEY,
    SECRET_LABEL_LESS_SECURE_KEY,
    SECRET_LENGTHS,
    EnketoSecrets,
    OdkEnketoProvider,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.pyxform_k8s.v0.xlsform import XlsformRequirer
from charms.smtp_integrator.v0.smtp import SmtpRequires
from charms.tempo_coordinator_k8s.v0.tracing import TracingEndpointRequirer
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

logger = logging.getLogger(__name__)

SERVICE_CONTAINER = "service"
NGINX_CONTAINER = "nginx"

API_PORT = 8383
NGINX_PORT = 80

# The published central-nginx image does not contain these templates: upstream's
# compose bind-mounts them in, so the charm ships and pushes them.
TEMPLATE_DIR = Path(__file__).parent / "templates"
NGINX_TEMPLATE_DIR = "/usr/share/odk/nginx"
NGINX_ENTRYPOINT = "/scripts/setup-odk.sh"

# nginx serves this static file, written into the image at build time. It is
# what reports the deployed version; there is no API route that does. Fetching
# it requires a Host header matching the configured domain.
VERSION_PATH = "/version.txt"

# How long a hook will wait for the API to answer before giving up and letting
# the next event retry. nginx proxies to the API and fails its own start if the
# API is not there, so the order matters.
API_READY_TIMEOUT = 60
API_POLL_INTERVAL = 2

# Paths inside the published central-service image. `service.dockerfile` sets
# WORKDIR /usr/odk and node-config reads config/local.json from there.
WORKING_DIR = "/usr/odk"
CONFIG_PATH = "/usr/odk/config/local.json"

# Upstream's start-odk.sh renders the config, waits for PostgreSQL, runs
# migrations, starts cron and only then execs pm2. The charm drives those steps
# separately so that migrations are a checkable operation rather than a side
# effect of starting the server.
MIGRATIONS_COMMAND = ["node", "./lib/bin/run-migrations"]
SERVER_COMMAND = "npx --no pm2-runtime ./pm2.config.js"

# Upstream's odk-cmd is a two-line wrapper around this. Calling the script
# directly keeps the exec's argv explicit.
CLI_COMMAND = ["node", "./lib/bin/cli.js"]
PURGE_COMMAND = ["node", "./lib/bin/purge.js"]
S3_COMMAND = ["node", "./lib/bin/s3.js"]

SECRET_LABEL_ADMIN_PASSWORD = "odk-admin-password"

# Long enough to be safe, short enough to be typed once if it has to be.
GENERATED_PASSWORD_LENGTH = 24

ACTION_TIMEOUT = 600

# Central builds this from env.domain itself; the same value has to be
# registered with the provider or the callback is rejected.
OIDC_CALLBACK_PATH = "/v1/oidc/callback"
# Central sends openid and email, and requires the email and email_verified
# claims. profile is requested as well so that users get a display name.
OIDC_SCOPES = "openid email profile"

# ODK Central exposes no metrics: GET /v1/metrics is a 404 and central-backend
# has no instrumentation dependencies. The charm ships an exporter that reads
# the numbers an operator watches straight out of the database.
EXPORTER_SERVICE = "exporter"
EXPORTER_PORT = 9102
EXPORTER_SOURCE = TEMPLATE_DIR.parent / "exporter" / "odk-exporter.js"
EXPORTER_PATH = "/usr/odk/odk-exporter.js"
BACKUP_TIMEOUT = 3600

# Where a dump lives inside the service container while it is being moved.
DUMP_PATH = "/tmp/central-backup.dump"  # noqa: S108 - inside the workload container

# The published central-service image ships postgresql-client-14, matching the
# PostgreSQL that upstream's compose deployment runs. pg_dump refuses to dump a
# server newer than itself, so a 16-series database cannot be backed up with
# the tooling in the image -- and neither can Central's own /v1/backup endpoint,
# which shells out to the same binary.
# pg_restore --clean emits a DROP for every object in the archive, extensions
# included, and the relation user does not own the extensions that the database
# charm installs as superuser -- pgaudit, for one. Restoring the whole archive
# therefore fails on an object the application never created and does not need
# to recreate. The archive's table of contents is filtered to drop every
# EXTENSION entry, which removes both its DROP and its CREATE, and the
# extensions already present in the database are left exactly as they are.
RESTORE_SCRIPT = """set -eu
pg_restore --list "$DUMP" > /tmp/central-restore.toc
grep -v ' EXTENSION ' /tmp/central-restore.toc > /tmp/central-restore.filtered
# pg_restore needs an explicit --dbname even when PGDATABASE is set.
pg_restore --clean --if-exists --no-owner --no-privileges \\
    --dbname "$PGDATABASE" --use-list /tmp/central-restore.filtered "$DUMP"
rm -f /tmp/central-restore.toc /tmp/central-restore.filtered
"""

VERSION_MISMATCH_HINT = (
    "The pg_dump in the ODK Central image cannot dump this server: it is older "
    "than the database. ODK Central targets PostgreSQL 14, so deploy "
    "`postgresql-k8s --channel 14/stable` if you want this action to work. On a "
    "16-series database, take backups with the database charm instead: "
    "`juju run postgresql-k8s/leader create-backup`. Note that ODK Central's own "
    "/v1/backup endpoint is unavailable for the same reason."
)

# /v1/config/public is the only unauthenticated endpoint that also touches the
# database, so it proves the API and its database are both working. There is no
# /v1/version.json: the deployed version is a static file served by nginx.
HEALTH_PATH = "/v1/config/public"

MIGRATION_TIMEOUT = 3600

# Events after which the database schema may need migrating. Everything else
# reconciles without paying for a migration run.
MIGRATION_EVENTS = (
    ops.InstallEvent,
    ops.UpgradeCharmEvent,
    DatabaseCreatedEvent,
    DatabaseEndpointsChangedEvent,
)

DATABASE_NAME = "odk"

# Placeholder used until Enketo answers over the relation. Central must not
# block on Enketo, because Enketo cannot start until Central has published the
# shared secrets: waiting for each other would deadlock the group.
ENKETO_URL_PLACEHOLDER = "http://enketo.invalid:8005/-"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# ASCII only, so that a character count is also a byte count. Upstream asserts
# on the byte length of the secret files.
SECRET_ALPHABET = string.ascii_letters + string.digits


_STACK_FRAME = re.compile(r"^\s+at\s")


def stop_if_running(container: ops.Container, service_name: str) -> None:
    """Stop a Pebble service, tolerating one that was never started."""
    if not container.can_connect():
        return
    service = container.get_services().get(service_name)
    if service is not None and service.is_running():
        container.stop(service_name)


def _tail(output: str, lines: int = 50) -> str:
    """Return the last few lines of command output, for an action result."""
    return "\n".join(output.splitlines()[-lines:])


def _reason(exc: ops.pebble.ExecError[str]) -> str:
    """Return the most useful message from a failed exec.

    Central's CLI is a Node program, so a failure arrives as a stack trace.
    The line an operator needs is the error itself, near the top; the tail is
    module-loader frames that say nothing. Stack frames are dropped and the
    error line is preferred.
    """
    text = str(exc.stderr or exc.stdout or exc)
    lines = [
        line.strip()
        for line in text.splitlines()
        # Stack frames, the caret pointer, and the re-thrown source line.
        if line.strip() and not _STACK_FRAME.match(line) and line.strip() not in {"^"}
    ]
    if not lines:
        return f"exited {exc.exit_code}"

    for index, line in enumerate(lines):
        if "Error" in line or "error" in line:
            return " ".join(lines[index : index + 2])[:500]
    return " ".join(lines[:2])[:500]


class MigrationError(Exception):
    """The database migrations did not complete successfully."""

    def __init__(self, exit_code: int, output: str) -> None:
        super().__init__(f"migrations exited {exit_code}")
        self.exit_code = exit_code
        self.output = output


def generate_secret(length: int) -> str:
    """Return a random ASCII secret of exactly ``length`` characters."""
    value = "".join(secrets.choice(SECRET_ALPHABET) for _ in range(length))
    # Belt and braces: the length is load-bearing for Enketo's startup check.
    if len(value.encode()) != length:
        raise RuntimeError(f"generated a {len(value.encode())}-byte secret, wanted {length}")
    return value


class OdkCentralCharm(ops.CharmBase):
    """Run the ODK Central API and frontend, and anchor the three-charm group."""

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.database = DatabaseRequires(
            self,
            relation_name="postgresql",
            database_name=DATABASE_NAME,
        )
        self.xlsform = XlsformRequirer(self)
        self.enketo = OdkEnketoProvider(self)
        self.s3 = S3Requirer(
            self,
            relation_name="s3",
            bucket_name=str(self.config["s3-bucket-name"]) or None,
        )
        self.ingress = IngressPerAppRequirer(
            self,
            relation_name="ingress",
            # nginx, not the API: Central's frontend and API must be
            # same-origin, and nginx is what serves the frontend.
            port=NGINX_PORT,
            strip_prefix=False,
        )
        self.smtp = SmtpRequires(self, relation_name="smtp")
        # Constructed after the ingress requirer: the redirect URI it registers
        # with the provider is derived from the public URL, which the ingress
        # relation supplies.
        self.oauth = OAuthRequirer(
            self,
            client_config=ClientConfig(
                redirect_uri=f"{self._external_url().rstrip('/')}{OIDC_CALLBACK_PATH}",
                scope=OIDC_SCOPES,
                grant_types=["authorization_code"],
            ),
            relation_name="oauth",
        )
        self.logging = LogForwarder(self, relation_name="logging")
        self.metrics = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[{"static_configs": [{"targets": [f"*:{EXPORTER_PORT}"]}]}],
            refresh_event=[self.on.config_changed],
        )
        self.dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")
        # The charm's own traces. central-backend has no OpenTelemetry
        # dependencies and emits no spans of its own -- see
        # docs/observability.md, which says so rather than pretending otherwise.
        self.tracing = TracingEndpointRequirer(
            self, relation_name="tracing", protocols=["otlp_http"]
        )

        for event in (
            self.on.install,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.secret_changed,
            self.on[SERVICE_CONTAINER].pebble_ready,
            self.database.on.database_created,
            self.database.on.endpoints_changed,
            self.on["postgresql"].relation_broken,
            self.xlsform.on.xlsform_ready,
            self.xlsform.on.xlsform_gone,
            self.ingress.on.ready,
            self.ingress.on.revoked,
            self.enketo.on.enketo_url_changed,
            self.s3.on.credentials_changed,
            self.s3.on.credentials_gone,
            self.smtp.on.smtp_data_available,
            self.on["smtp"].relation_broken,
            self.oauth.on.oauth_info_changed,
            self.oauth.on.oauth_info_removed,
            self.on[NGINX_CONTAINER].pebble_ready,
        ):
            framework.observe(event, self._on_lifecycle_event)

        for action, handler in (
            ("create_admin", self._on_create_admin_action),
            ("promote_user", self._on_promote_user_action),
            ("reset_password", self._on_reset_password_action),
            ("rotate_enketo_secrets", self._on_rotate_enketo_secrets_action),
            ("run_migrations", self._on_run_migrations_action),
            ("upload_pending_blobs", self._on_upload_pending_blobs_action),
            ("purge_deleted", self._on_purge_deleted_action),
            ("backup", self._on_backup_action),
            ("restore", self._on_restore_action),
        ):
            framework.observe(getattr(self.on, f"{action}_action"), handler)

    # Event handling

    def _on_lifecycle_event(self, event: ops.EventBase) -> None:
        """Reconcile on any event that could have changed the desired state."""
        self._reconcile(migrate=isinstance(event, MIGRATION_EVENTS))

    # Reconciliation

    def _reconcile(
        self, *, migrate: bool = False, shared_secrets: EnketoSecrets | None = None
    ) -> None:
        """Bring the workloads into line with configuration and relation data.

        Every hook routes through here rather than each handler doing its own
        partial update. That matters more in this charm than in most: its
        configuration depends on several relations that settle in any order, so
        per-relation handlers would each act on a different partial view.

        ``shared_secrets`` overrides what would be read from the model. The
        rotation action needs this: a new secret revision only becomes current
        when the hook ends, so re-reading inside the hook that wrote it returns
        the values being replaced.
        """
        invalid = self._invalid_config()
        if invalid:
            self.unit.status = ops.BlockedStatus(invalid)
            return

        container = self.unit.get_container(SERVICE_CONTAINER)
        if not container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for the service container")
            return

        if shared_secrets is None:
            shared_secrets = self._shared_secrets()
        if shared_secrets is None:
            self.unit.status = ops.WaitingStatus("waiting for the leader to generate secrets")
            return

        # Publish to Enketo before the database check. Enketo cannot start until
        # it has these, and blocking that on Central's own database would mean
        # a database problem takes the web forms down as well.
        self.enketo.publish(
            base_url=self._external_url(),
            support_email=str(self.config["sysadmin-email"]),
        )

        database = self._database_config()
        if database is None:
            self.unit.status = ops.WaitingStatus("waiting for the postgresql relation")
            return

        config_changed = self._push_if_changed(
            container,
            CONFIG_PATH,
            json.dumps(self._render_service_config(database, shared_secrets), indent=2),
            permissions=0o600,
        )

        # Migrations are cheap when there is nothing to do, but not free, and
        # running them from update-status every few minutes is pure waste. Run
        # them when something could have changed the schema, and whenever the
        # API has not been started yet -- which covers the case where the
        # database relation settled before the container was reachable.
        started = SERVICE_CONTAINER in container.get_plan().services
        if migrate or not started:
            self.unit.status = ops.MaintenanceStatus("running database migrations")
            try:
                self._run_migrations(container)
            except MigrationError as exc:
                logger.error("database migrations failed (%s):\n%s", exc.exit_code, exc.output)
                self.unit.status = ops.BlockedStatus(
                    "database migration failed; see juju debug-log"
                )
                return

        self._push_if_changed(container, EXPORTER_PATH, EXPORTER_SOURCE.read_text())

        container.add_layer(SERVICE_CONTAINER, self._service_layer(), combine=True)
        container.replan()
        self.unit.set_ports(API_PORT, NGINX_PORT, EXPORTER_PORT)

        # Central reads config.json once, at startup, and replan only restarts a
        # service whose *layer* changed. Without this, a new database endpoint,
        # SMTP relay, blob store or enketo.url would sit on disk while the
        # running workload kept using the values it started with.
        service = container.get_services().get(SERVICE_CONTAINER)
        if config_changed and service is not None and service.is_running():
            logger.info("service configuration changed; restarting the api")
            container.restart(SERVICE_CONTAINER)

        if not self._wait_for_api():
            self.unit.status = ops.WaitingStatus("waiting for the api to become healthy")
            return

        if not self._reconcile_nginx():
            self.unit.status = ops.BlockedStatus(
                "nginx did not start; see juju debug-log for its output"
            )
            return

        self.unit.status = self._status(container)

    def _status(self, container: ops.Container | None = None) -> ops.StatusBase:
        """Return a status that names what is and is not yet wired up.

        Blocked beats degraded: a deployment that is running but cannot publish
        forms, render them, or send mail should say which, rather than showing
        a bare "active" that hides it.
        """
        if self.config["oidc-enabled"] and not self._oidc_ready():
            return ops.BlockedStatus(
                "oidc-enabled is set but no usable provider: relate an oauth "
                "provider, or set oidc-issuer-url, oidc-client-id and "
                "oidc-client-secret"
            )

        notes: list[str] = []
        if self.xlsform.endpoint is None:
            notes.append("no xlsform relation, form publishing will fail")
        if self.enketo.enketo_url is None:
            notes.append("no enketo relation, web forms will not render")
        if not self.model.relations.get("smtp"):
            notes.append("no smtp relation, account email will not be sent")

        if self._oidc_ready():
            source = "oauth relation" if self._oauth_provider() else "config"
            notes.append(f"oidc via {source}, password login disabled")

        pending = self._pending_blobs_note(container)
        if pending:
            notes.append(pending)

        return ops.ActiveStatus("; ".join(notes))

    def _pending_blobs_note(self, container: ops.Container | None) -> str:
        """Return a note about attachments still waiting to move to S3.

        New blobs go to S3 as soon as the relation exists, but existing ones
        stay in PostgreSQL until upload-pending-blobs moves them. That
        partially-migrated state can last indefinitely, so it is worth saying.
        """
        if container is None or not self._s3_config():
            return ""
        count = self._pending_blob_count(container)
        if not count:
            return ""
        return f"{count} blobs pending upload to s3"

    def _invalid_config(self) -> str | None:
        """Return a message describing the first invalid config option, if any."""
        if str(self.config["log-level"]).upper() not in VALID_LOG_LEVELS:
            return f"log-level must be one of {', '.join(VALID_LOG_LEVELS)}"
        if int(self.config["db-pool-size"]) < 1:
            return "db-pool-size must be at least 1"
        if int(self.config["session-lifetime"]) < 1:
            return "session-lifetime must be at least 1 second"
        return None

    # Shared secrets

    def _shared_secrets(self) -> EnketoSecrets | None:
        """Return the three shared secrets, generating them on the leader.

        These are needed even before ``enketo-k8s`` exists, because the API key
        is part of Central's own configuration. Returns ``None`` on a follower
        that is running before the leader has created them.
        """
        try:
            return self._read_shared_secrets()
        except ops.SecretNotFoundError:
            pass

        if not self.unit.is_leader():
            return None

        for label, length in SECRET_LENGTHS.items():
            try:
                self.model.get_secret(label=label)
            except ops.SecretNotFoundError:
                self.app.add_secret({"value": generate_secret(length)}, label=label)
                logger.info("generated shared secret %s", label)

        return self._read_shared_secrets()

    def _read_shared_secrets(self) -> EnketoSecrets:
        """Read the three shared secrets from the model.

        :raises ops.SecretNotFoundError: if any of them does not exist yet.
        """
        values = {
            label: self.model.get_secret(label=label).get_content(refresh=True)["value"]
            for label in SECRET_LENGTHS
        }
        shared_secrets = EnketoSecrets(
            api_key=values[SECRET_LABEL_API_KEY],
            encryption_key=values[SECRET_LABEL_ENCRYPTION_KEY],
            less_secure_key=values[SECRET_LABEL_LESS_SECURE_KEY],
        )
        shared_secrets.validate()
        return shared_secrets

    # Database

    def _database_config(self) -> dict[str, Any] | None:
        """Return the ``database`` stanza, or ``None`` if the relation is incomplete.

        central-backend translates these into libpq environment variables, and
        rejects any key it does not recognise, so this must contain exactly
        host, port, user, password, database and maximumPoolSize. TLS options
        belong in PGSSLMODE and PGSSLROOTCERT, not here.
        """
        relations = self.model.relations.get("postgresql")
        if not relations:
            return None

        for data in self.database.fetch_relation_data().values():
            endpoints = data.get("endpoints")
            username = data.get("username")
            password = data.get("password")
            if not endpoints or not username or not password:
                continue

            # "host:port,host:port" — the first is the primary.
            host, _, port = endpoints.split(",")[0].partition(":")
            return {
                "host": host,
                "port": int(port) if port else 5432,
                "user": username,
                "password": password,
                "database": data.get("database") or DATABASE_NAME,
                "maximumPoolSize": int(self.config["db-pool-size"]),
            }
        return None

    # Rendered workload configuration

    def _email_config(self) -> dict[str, Any]:
        """Return the ``email`` stanza.

        Without an smtp relation this points at a local relay that is not
        there, which is deliberate: Central only sends mail on account
        creation, password reset and project invitation, so those operations
        fail while everything else keeps working. An empty host would risk
        failing at transport construction, which happens at startup.
        """
        sender = str(self.config["email-from"]).strip() or f"no-reply@{self._domain()}"
        transport: dict[str, Any] = {
            "host": "localhost",
            "port": 25,
            "secure": False,
            "ignoreTLS": True,
            "auth": {"user": "", "pass": ""},
        }

        relay = self._smtp_data()
        if relay is not None:
            transport = {
                "host": relay.host,
                "port": relay.port,
                # "secure" means implicit TLS on connect, which is what the
                # relation calls TLS. STARTTLS is negotiated afterwards and is
                # not the same thing.
                "secure": relay.transport_security.value == "tls",
                "ignoreTLS": relay.transport_security.value == "none",
                "auth": {"user": relay.user or "", "pass": relay.password or ""},
            }

        return {
            "serviceAccount": sender,
            "transport": "smtp",
            "transportOpts": transport,
        }

    def _smtp_data(self) -> Any:
        """Return the SMTP relay's settings, or ``None`` when unrelated."""
        if not self.model.relations.get("smtp"):
            return None
        try:
            return self.smtp.get_relation_data()
        except Exception:  # noqa: BLE001 - the library validates and raises broadly
            logger.warning("the smtp relation data is not usable yet")
            return None

    # --- OpenID Connect ---------------------------------------------------

    def _oidc_config(self) -> dict[str, Any]:
        """Return the ``oidc`` stanza.

        Relation data wins over configuration when both are present, and the
        status message says so rather than silently preferring one.
        """
        disabled = {"enabled": False, "issuerUrl": "", "clientId": "", "clientSecret": ""}
        if not self.config["oidc-enabled"]:
            return disabled

        provider = self._oauth_provider()
        if provider is not None:
            return {
                "enabled": True,
                "issuerUrl": provider.issuer_url,
                "clientId": provider.client_id or "",
                "clientSecret": provider.client_secret or "",
            }

        issuer = str(self.config["oidc-issuer-url"]).strip()
        client_id = str(self.config["oidc-client-id"]).strip()
        client_secret = self._oidc_client_secret()
        if not (issuer and client_id and client_secret):
            return disabled

        return {
            "enabled": True,
            "issuerUrl": issuer,
            "clientId": client_id,
            "clientSecret": client_secret,
        }

    def _oauth_provider(self) -> Any:
        """Return the provider's details from the oauth relation, if usable."""
        if not self.model.relations.get("oauth"):
            return None
        try:
            provider = self.oauth.get_provider_info()
        except Exception:  # noqa: BLE001 - the library validates and raises broadly
            logger.warning("the oauth relation data is not usable yet")
            return None
        if provider is None or not provider.issuer_url or not provider.client_id:
            return None
        return provider

    def _oidc_client_secret(self) -> str:
        """Return the operator-provided OIDC client secret, if there is one."""
        secret_id = str(self.config.get("oidc-client-secret") or "").strip()
        if not secret_id:
            return ""
        try:
            content = self.model.get_secret(id=secret_id).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            return ""
        return content.get("client-secret", "")

    def _oidc_ready(self) -> bool:
        """Return whether OIDC is both requested and fully configured."""
        return bool(self._oidc_config()["enabled"])

    def _sentry_config(self) -> dict[str, Any]:
        """Return the ``sentry`` stanza, blank unless an operator opted in.

        Upstream's shipped compose defaults are the ODK project's own Sentry
        organisation, key and project id. Inheriting them would report this
        deployment's errors to ODK's telemetry, so they are never used.
        """
        dsn = str(self.config["error-reporting-dsn"]).strip()
        if not dsn:
            return {"orgSubdomain": "", "key": "", "project": "", "traceRate": ""}
        return {"dsn": dsn, "traceRate": "0.1"}

    def _s3_config(self) -> dict[str, str]:
        """Return the blob store settings, or an empty mapping when unrelated.

        New blobs go to S3 as soon as this relation exists. Blobs already in
        PostgreSQL stay there until the upload-pending-blobs action moves them,
        so a deployment can sit in a partially migrated state indefinitely.
        """
        if not self.model.relations.get("s3"):
            return {}

        info = self.s3.get_s3_connection_info()
        access_key = info.get("access-key")
        secret_key = info.get("secret-key")
        bucket = str(self.config["s3-bucket-name"]).strip() or info.get("bucket")
        endpoint = info.get("endpoint")
        if not (access_key and secret_key and bucket and endpoint):
            return {}

        return {
            "server": endpoint,
            "accessKey": access_key,
            "secretKey": secret_key,
            "bucketName": bucket,
        }

    def _render_service_config(
        self, database: dict[str, Any], shared_secrets: EnketoSecrets
    ) -> dict[str, Any]:
        """Build the full ``config.json`` document for the API service.

        The top-level ``default`` key is not an environment name: central-backend
        reads its settings as ``config.get('default.<thing>')``.
        """
        endpoint = self.xlsform.endpoint
        return {
            "default": {
                "database": database,
                "email": self._email_config(),
                "sessionLifetime": int(self.config["session-lifetime"]),
                "xlsform": {
                    "host": endpoint.host if endpoint else "",
                    "port": endpoint.port if endpoint else 80,
                },
                "enketo": {
                    "url": self._enketo_url(),
                    "apiKey": shared_secrets.api_key,
                },
                "env": {
                    "domain": self._external_url(),
                    "sysadminAccount": str(self.config["sysadmin-email"]),
                },
                "oidc": self._oidc_config(),
                "external": {
                    "sentry": self._sentry_config(),
                    "s3blobStore": {
                        "server": "",
                        "accessKey": "",
                        "secretKey": "",
                        "bucketName": "",
                        **self._s3_config(),
                        "requestTimeout": 60000,
                    },
                },
            }
        }

    # Workload

    def _service_environment(self) -> dict[str, str]:
        """Return the environment for the API service and its one-shot jobs.

        Deliberately free of PG* variables: central-backend derives those from
        config.json, and a PG* variable set here would silently take precedence
        over the rendered configuration.
        """
        return {
            "NODE_ENV": "production",
            # pm2.config.js reads this to decide how many workers to fork.
            "WORKER_COUNT": "1" if int(self.config["db-pool-size"]) <= 2 else "4",
            "LOG_LEVEL": str(self.config["log-level"]).upper(),
            # Never inherit upstream's shipped Sentry defaults.
            "SENTRY_ORG_SUBDOMAIN": "",
            "SENTRY_KEY": "",
            "SENTRY_PROJECT": "",
            "SENTRY_TRACE_RATE": "",
        }

    def _service_layer(self) -> ops.pebble.Layer:
        """Build the Pebble layer for the API service."""
        return ops.pebble.Layer(
            {
                "summary": "odk central service",
                "description": "The ODK Central API.",
                "services": {
                    SERVICE_CONTAINER: {
                        "override": "replace",
                        "summary": "ODK Central API",
                        "command": SERVER_COMMAND,
                        "startup": "enabled",
                        "working-dir": WORKING_DIR,
                        "environment": self._service_environment(),
                        "on-failure": "restart",
                    },
                    EXPORTER_SERVICE: {
                        "override": "replace",
                        "summary": "prometheus exporter",
                        "command": f"node {EXPORTER_PATH}",
                        "startup": "enabled",
                        "working-dir": WORKING_DIR,
                        "environment": {
                            "EXPORTER_PORT": str(EXPORTER_PORT),
                            # Whole-table counts; they do not need to be fresh
                            # to the second and should not become database load.
                            "EXPORTER_INTERVAL": "60",
                        },
                        "on-failure": "restart",
                    },
                },
                "checks": {
                    "api-up": {
                        "override": "replace",
                        "level": "ready",
                        "period": "15s",
                        "threshold": 3,
                        "http": {"url": f"http://localhost:{API_PORT}{HEALTH_PATH}"},
                    },
                },
            }
        )

    # nginx and the public frontend

    def _external_url(self) -> str:
        """Return the URL the deployment is actually reached at.

        The ``external-hostname`` option wins over the ingress relation when
        both are set, so that an operator can point Central at the name their
        users type even when it differs from what the ingress advertises.
        """
        hostname = str(self.config["external-hostname"]).strip()
        if hostname:
            return f"https://{hostname}"
        if self.ingress.url:
            # Traefik advertises a trailing slash. Central concatenates paths
            # onto env.domain without normalising, so leaving it produces
            # redirects to "https://host//login".
            return str(self.ingress.url).rstrip("/")
        return f"http://localhost:{API_PORT}"

    def _domain(self) -> str:
        """Return the bare hostname nginx should serve, without scheme or port."""
        parsed = urlparse(self._external_url())
        return parsed.hostname or "localhost"

    def _nginx_environment(self) -> dict[str, str]:
        """Return the environment setup-odk.sh templates the nginx config from."""
        dsn = str(self.config["error-reporting-dsn"]).strip()
        return {
            "DOMAIN": self._domain(),
            # TLS terminates at the ingress. This makes the image serve plain
            # HTTP on 80, strip its ssl_ directives and trust X-Forwarded-Proto.
            # Never enable certbot inside the charm.
            "SSL_TYPE": "upstream",
            "HTTPS_PORT": "443",
            # The browser reads this from client-config.json at page load.
            # Advertising OIDC before it is usable would offer a login that
            # cannot work; so would offering a password form once it is.
            "OIDC_ENABLED": "true" if self._oidc_ready() else "false",
            "ENKETO_UPSTREAM": self._enketo_upstream(),
            # Upstream's shipped defaults are the ODK project's own Sentry
            # organisation and key. They are never inherited.
            "SENTRY_ORG_SUBDOMAIN": "",
            "SENTRY_KEY": "",
            "SENTRY_PROJECT": "",
            "SENTRY_DSN_FRONTEND": dsn,
        }

    def _enketo_url(self) -> str:
        """Return the URL for Central's ``enketo.url``, or the placeholder.

        The placeholder keeps Central startable before Enketo exists. It is
        never a working address, and web forms fail until Enketo answers, but
        that is strictly better than a deadlock: Enketo cannot answer until
        Central has published the shared secrets.
        """
        return self.enketo.enketo_url or ENKETO_URL_PLACEHOLDER

    def _enketo_upstream(self) -> str:
        """Return the host:port nginx should proxy the /- paths to.

        nginx has to load a valid configuration whether or not Enketo is
        related, so this falls back to a local address that simply refuses
        connections rather than to something nginx cannot resolve at all.
        """
        parsed = urlparse(self._enketo_url())
        if not parsed.hostname or parsed.hostname == urlparse(ENKETO_URL_PLACEHOLDER).hostname:
            return f"127.0.0.1:{ENKETO_PORT}"
        return f"{parsed.hostname}:{parsed.port or ENKETO_PORT}"

    def _nginx_config_template(self) -> str:
        """Return the nginx site template, adjusted for the Sentry setting.

        With no DSN the Sentry variables are blank, and upstream's /csp-report
        location would render as ``https://.ingest.sentry.io/api//security/``,
        which nginx refuses to load. Swallow the reports locally instead of
        proxying them anywhere.
        """
        template = (TEMPLATE_DIR / "odk.conf.template").read_text()
        if str(self.config["error-reporting-dsn"]).strip():
            return template

        return template.replace(
            "    proxy_pass https://${SENTRY_ORG_SUBDOMAIN}.ingest.sentry.io"
            "/api/${SENTRY_PROJECT}/security/?sentry_key=${SENTRY_KEY};\n"
            "    proxy_ssl_server_name on;\n",
            "    # No error-reporting DSN is configured, so CSP reports are\n"
            "    # accepted and discarded rather than forwarded anywhere.\n"
            "    return 204;\n",
        )

    def _reconcile_nginx(self) -> bool:
        """Push the nginx templates and (re)start the frontend proxy.

        :returns: whether nginx is running.

        A change to DOMAIN, OIDC_ENABLED or the Sentry settings is a re-render,
        not just a restart: the browser reads client-config.json at page load
        and the entrypoint is what regenerates it. Reloading nginx alone would
        leave the old client config in place, so the service is restarted, and
        only when something it depends on has actually changed.
        """
        container = self.unit.get_container(NGINX_CONTAINER)
        if not container.can_connect():
            return False

        template = self._nginx_config_template()
        changed = self._push_if_changed(
            container, f"{NGINX_TEMPLATE_DIR}/odk.conf.template", template
        )
        changed |= self._push_if_changed(
            container,
            f"{NGINX_TEMPLATE_DIR}/client-config.json.template",
            (TEMPLATE_DIR / "client-config.json.template").read_text(),
        )

        layer = self._nginx_layer()
        # Capture the running plan *before* merging the new layer into it:
        # add_layer rewrites the plan, so comparing afterwards would never see
        # a difference and the frontend would keep serving a stale origin.
        environment_changed = self._environment_changed(container, layer)
        container.add_layer(NGINX_CONTAINER, layer, combine=True)

        # A workload that refuses to start is a deployment problem to report,
        # not a charm error to raise: nginx will not start if its generated
        # configuration is invalid, and an operator needs to see why rather than
        # a traceback.
        service = container.get_services().get(NGINX_CONTAINER)
        try:
            if service is None or not service.is_running():
                container.start(NGINX_CONTAINER)
            elif changed or environment_changed:
                container.restart(NGINX_CONTAINER)
        except ops.pebble.ChangeError as exc:
            # exc.err, not exc: ChangeError's own string representation walks
            # the change's tasks, which is not always populated.
            logger.error("nginx did not start: %s", exc.err)
            return False
        return True

    @staticmethod
    def _environment_changed(container: ops.Container, layer: ops.pebble.Layer) -> bool:
        """Return whether the wanted nginx environment differs from the running one.

        Must be called before ``add_layer``.
        """
        current = container.get_plan().services.get(NGINX_CONTAINER)
        wanted = layer.services[NGINX_CONTAINER]
        return current is None or dict(current.environment) != dict(wanted.environment)

    @staticmethod
    def _push_if_changed(
        container: ops.Container,
        path: str,
        content: str,
        *,
        permissions: int | None = None,
    ) -> bool:
        """Push ``content`` to ``path``, returning whether it differed.

        Pebble's replan only restarts a service whose *layer* changed, so a
        workload configured from a file needs the charm to notice the file
        changing and restart it explicitly.
        """
        try:
            with container.pull(path) as existing:
                if existing.read() == content:
                    return False
        except (ops.pebble.PathError, ops.pebble.APIError):
            pass
        container.push(path, content, make_dirs=True, permissions=permissions)
        return True

    def _nginx_layer(self) -> ops.pebble.Layer:
        """Build the Pebble layer for the nginx frontend proxy."""
        return ops.pebble.Layer(
            {
                "summary": "odk central nginx",
                "description": "Serves the built frontend and proxies the API and Enketo.",
                "services": {
                    NGINX_CONTAINER: {
                        "override": "replace",
                        "summary": "nginx",
                        # The image's own entrypoint: it templates the site
                        # config and client-config.json, then execs nginx.
                        "command": NGINX_ENTRYPOINT,
                        # Started by charm code once the API is healthy. Pebble
                        # dependencies do not cross container boundaries, so
                        # the ordering cannot be expressed in the layer.
                        "startup": "disabled",
                        "environment": self._nginx_environment(),
                        "on-failure": "restart",
                    },
                },
                "checks": {
                    "nginx-up": {
                        "override": "replace",
                        "level": "ready",
                        "period": "15s",
                        "threshold": 3,
                        # The same check upstream's compose file uses. An HTTP
                        # check is not an option: upstream's config has a
                        # catch-all server block that answers 421 to any Host it
                        # does not serve, and Pebble cannot override the Host
                        # header of an http check -- setting it as a header is a
                        # no-op in Go, which takes Host from the URL. Whether
                        # nginx is serving the right content is covered by the
                        # charm's own reconcile and by the integration suite.
                        "exec": {"command": f"nc -z localhost {NGINX_PORT}"},
                    },
                },
            }
        )

    def _api_healthy(self) -> bool:
        """Return whether the API answers on its unauthenticated health route."""
        try:
            response = requests.get(f"http://localhost:{API_PORT}{HEALTH_PATH}", timeout=5)
        except requests.RequestException:
            return False
        return response.status_code == 200

    def _wait_for_api(self) -> bool:
        """Poll the API until it is healthy, or the hook's patience runs out."""
        deadline = time.monotonic() + API_READY_TIMEOUT
        while True:
            if self._api_healthy():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(API_POLL_INTERVAL)

    # Day-2 actions

    def _service_container(self, event: ops.ActionEvent) -> ops.Container | None:
        """Return the service container, failing the action if it is not ready."""
        container = self.unit.get_container(SERVICE_CONTAINER)
        if not container.can_connect():
            event.fail("The service container is not ready.")
            return None
        return container

    def _refuse_under_oidc(self, event: ops.ActionEvent, what: str) -> bool:
        """Fail the action when OIDC makes it meaningless, and say why."""
        if not self.config["oidc-enabled"]:
            return False
        event.fail(
            f"{what} is not possible while oidc-enabled is true: OpenID Connect "
            "replaces password authentication entirely, so a password would have "
            "no effect. Manage this user at your identity provider instead."
        )
        return True

    def _run_cli(
        self,
        container: ops.Container,
        args: list[str],
        stdin: str | None = None,
    ) -> str:
        """Run Central's admin CLI and return its output.

        :raises ops.pebble.ExecError: if the command exits non-zero.
        """
        process = container.exec(
            [*CLI_COMMAND, *args],
            working_dir=WORKING_DIR,
            environment=self._service_environment(),
            timeout=ACTION_TIMEOUT,
            # The CLI prompts for passwords rather than taking them as
            # arguments, which also keeps them out of the process table.
            stdin=stdin,
        )
        output, _ = process.wait_output()
        return output

    def _admin_email(self) -> str | None:
        """Return the initial administrator's address from its Juju secret."""
        secret_id = str(self.config.get("admin-email-secret") or "").strip()
        if not secret_id:
            return None
        try:
            content = self.model.get_secret(id=secret_id).get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            return None
        return content.get("email")

    def _admin_password(self) -> str:
        """Return the administrator password, generating it once if needed."""
        try:
            secret = self.model.get_secret(label=SECRET_LABEL_ADMIN_PASSWORD)
        except ops.SecretNotFoundError:
            password = generate_secret(GENERATED_PASSWORD_LENGTH)
            self.app.add_secret({"value": password}, label=SECRET_LABEL_ADMIN_PASSWORD)
            return password
        content: str = secret.get_content(refresh=True)["value"]
        return content

    def _on_create_admin_action(self, event: ops.ActionEvent) -> None:
        """Create the initial web user and promote it to administrator."""
        if self._refuse_under_oidc(event, "Creating a password user"):
            return
        if not self.unit.is_leader():
            event.fail("Run this on the leader unit; it owns the password secret.")
            return

        container = self._service_container(event)
        if container is None:
            return

        email = self._admin_email()
        if email is None:
            event.fail(
                "No administrator address is configured. Create a Juju secret "
                "holding it, for example `juju add-secret odk-admin-email "
                "email=ops@example.com`, grant it to this application, and set "
                "admin-email-secret to the resulting secret ID."
            )
            return

        password = self._admin_password()
        try:
            self._run_cli(container, ["-u", email, "user-create"], stdin=f"{password}\n")
            self._run_cli(container, ["-u", email, "user-promote"])
        except ops.pebble.ExecError as exc:
            event.fail(f"Could not create the administrator: {_reason(exc)}")
            return

        # Returned once. The password is not retrievable afterwards.
        event.set_results(
            {
                "email": email,
                "password": password,
                "note": (
                    "This password is shown once. Store it now; it cannot be "
                    "retrieved again. Use reset-password if it is lost."
                ),
            }
        )

    def _on_promote_user_action(self, event: ops.ActionEvent) -> None:
        """Grant an existing user the administrator role."""
        container = self._service_container(event)
        if container is None:
            return

        email = str(event.params["email"])
        try:
            self._run_cli(container, ["-u", email, "user-promote"])
        except ops.pebble.ExecError as exc:
            event.fail(f"Could not promote {email}: {_reason(exc)}")
            return

        event.set_results({"email": email, "result": "promoted to administrator"})

    def _on_reset_password_action(self, event: ops.ActionEvent) -> None:
        """Reset a user's password and return the new value once."""
        if self._refuse_under_oidc(event, "Resetting a password"):
            return

        container = self._service_container(event)
        if container is None:
            return

        email = str(event.params["email"])
        password = generate_secret(GENERATED_PASSWORD_LENGTH)
        try:
            self._run_cli(container, ["-u", email, "user-set-password"], stdin=f"{password}\n")
        except ops.pebble.ExecError as exc:
            event.fail(f"Could not reset the password for {email}: {_reason(exc)}")
            return

        event.set_results(
            {
                "email": email,
                "password": password,
                "note": "This password is shown once and is not retrievable afterwards.",
            }
        )

    def _on_rotate_enketo_secrets_action(self, event: ops.ActionEvent) -> None:
        """Regenerate the shared secrets and get both workloads onto the new ones."""
        if not self.unit.is_leader():
            event.fail("Run this on the leader unit; it owns the shared secrets.")
            return

        self.unit.status = ops.MaintenanceStatus("rotating enketo secrets")

        values = {label: generate_secret(length) for label, length in SECRET_LENGTHS.items()}
        for label, value in values.items():
            self.model.get_secret(label=label).set_content({"value": value})

        # Republish so a relation that has not seen these IDs yet gets them.
        # Enketo learns about the new values through secret-changed, which is
        # what actually carries a rotation: the IDs do not change.
        self.enketo.publish(
            base_url=self._external_url(),
            support_email=str(self.config["sysadmin-email"]),
        )

        # Central holds the API key in its own config too, so it has to be
        # re-rendered and restarted or the two ends disagree.
        #
        # The new values are passed in rather than read back: a secret revision
        # written in this hook does not become current until the hook ends, so
        # re-reading here would render the configuration with the key that is
        # being replaced. Central would then keep using the old key -- and keep
        # failing to authenticate to Enketo, which already has the new one --
        # until some later event happened to reconcile it.
        rotated = EnketoSecrets(
            api_key=values[SECRET_LABEL_API_KEY],
            encryption_key=values[SECRET_LABEL_ENCRYPTION_KEY],
            less_secure_key=values[SECRET_LABEL_LESS_SECURE_KEY],
        )
        rotated.validate()
        self._reconcile(shared_secrets=rotated)

        event.set_results(
            {
                "result": "all three shared secrets rotated",
                "warning": (
                    "Every in-progress web form session is invalidated. Anyone "
                    "part-way through filling in a form will have to start again."
                ),
            }
        )

    def _on_run_migrations_action(self, event: ops.ActionEvent) -> None:
        """Run the database migrations by hand and report what happened."""
        container = self._service_container(event)
        if container is None:
            return

        if self._database_config() is None:
            event.fail("There is no complete postgresql relation to migrate.")
            return

        try:
            output = self._run_migrations(container)
        except MigrationError as exc:
            event.fail(f"Migrations exited {exc.exit_code}. Last output:\n{_tail(exc.output)}")
            return

        event.set_results({"result": "migrations completed", "output": _tail(output)})

    def _on_upload_pending_blobs_action(self, event: ops.ActionEvent) -> None:
        """Move submission attachments from PostgreSQL to the S3 blob store."""
        container = self._service_container(event)
        if container is None:
            return

        if not self._s3_config():
            event.set_results(
                {
                    "result": "no-op",
                    "note": (
                        "There is no s3 relation, so submission attachments stay "
                        "in PostgreSQL. Relate s3-integrator to enable the blob "
                        "store."
                    ),
                }
            )
            return

        before = self._pending_blob_count(container)
        try:
            process = container.exec(
                [*S3_COMMAND, "upload-pending"],
                working_dir=WORKING_DIR,
                environment=self._service_environment(),
                timeout=ACTION_TIMEOUT,
                combine_stderr=True,
            )
            output, _ = process.wait_output()
        except ops.pebble.ExecError as exc:
            event.fail(f"Uploading pending blobs failed: {_reason(exc)}")
            return

        after = self._pending_blob_count(container)
        event.set_results(
            {
                "uploaded": max(0, (before or 0) - (after or 0)),
                "pending": after if after is not None else "unknown",
                "output": _tail(output),
            }
        )

    def _on_purge_deleted_action(self, event: ops.ActionEvent) -> None:
        """Permanently remove soft-deleted forms and submissions."""
        container = self._service_container(event)
        if container is None:
            return

        args = list(PURGE_COMMAND)
        if event.params.get("force"):
            args.append("--force")

        try:
            process = container.exec(
                args,
                working_dir=WORKING_DIR,
                environment=self._service_environment(),
                timeout=ACTION_TIMEOUT,
                combine_stderr=True,
            )
            output, _ = process.wait_output()
        except ops.pebble.ExecError as exc:
            event.fail(f"Purge failed: {_reason(exc)}")
            return

        event.set_results({"result": "purge completed", "output": _tail(output)})

    def _backup_environment(self, database: dict[str, Any]) -> dict[str, str]:
        """Return the libpq environment for pg_dump and pg_restore."""
        return {
            "PGHOST": str(database["host"]),
            "PGPORT": str(database["port"]),
            "PGUSER": str(database["user"]),
            "PGPASSWORD": str(database["password"]),
            "PGDATABASE": str(database["database"]),
        }

    def _s3_client(self, config: dict[str, str]) -> Any:
        """Return a boto3 S3 client for the related blob store."""
        return boto3.client(
            "s3",
            endpoint_url=config["server"],
            aws_access_key_id=config["accessKey"],
            aws_secret_access_key=config["secretKey"],
        )

    def _on_backup_action(self, event: ops.ActionEvent) -> None:
        """Dump the Central database to the S3 blob store."""
        container = self._service_container(event)
        if container is None:
            return

        s3 = self._s3_config()
        if not s3:
            event.fail(
                "There is no complete s3 relation to write the backup to. Relate "
                "s3-integrator and try again."
            )
            return

        database = self._database_config()
        if database is None:
            event.fail("There is no complete postgresql relation to back up.")
            return

        self.unit.status = ops.MaintenanceStatus("taking a database backup")
        try:
            try:
                process = container.exec(
                    ["pg_dump", "--format=custom", "--file", DUMP_PATH],
                    environment=self._backup_environment(database),
                    timeout=BACKUP_TIMEOUT,
                    combine_stderr=True,
                )
                process.wait_output()
            except ops.pebble.ExecError as exc:
                output = str(exc.stdout or "")
                if "server version mismatch" in output:
                    event.fail(VERSION_MISMATCH_HINT)
                else:
                    event.fail(f"pg_dump failed: {_reason(exc)}")
                return

            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            key = f"{str(event.params['destination']).strip('/')}/{timestamp}/central.dump"

            try:
                with container.pull(DUMP_PATH, encoding=None) as dump:
                    payload = dump.read()
            except ops.pebble.PathError:
                event.fail(
                    "pg_dump reported success but produced no dump file. Check the "
                    "service container's logs."
                )
                return
            self._s3_client(s3).upload_fileobj(io.BytesIO(payload), s3["bucketName"], key)

            event.set_results(
                {
                    "path": f"s3://{s3['bucketName']}/{key}",
                    "bytes": len(payload),
                    "note": (
                        "This is the database only. Submission attachments already "
                        "moved to the blob store are NOT in this dump, so the "
                        "bucket needs its own versioning or lifecycle policy to be "
                        "recoverable."
                    ),
                }
            )
        finally:
            with contextlib.suppress(ops.pebble.PathError):
                container.remove_path(DUMP_PATH)
            self._reconcile()

    def _on_restore_action(self, event: ops.ActionEvent) -> None:
        """Restore the Central database from a backup snapshot."""
        container = self._service_container(event)
        if container is None:
            return

        s3 = self._s3_config()
        if not s3:
            event.fail("There is no complete s3 relation to read the backup from.")
            return

        database = self._database_config()
        if database is None:
            event.fail("There is no complete postgresql relation to restore into.")
            return

        environment = self._backup_environment(database)
        if not event.params.get("force") and self._database_has_tables(container, environment):
            event.fail(
                "The target database is not empty. Restoring would overwrite it, so "
                "pass force=true if that is what you intend."
            )
            return

        nginx = self.unit.get_container(NGINX_CONTAINER)
        self.unit.status = ops.MaintenanceStatus("restoring the database")
        try:
            # Central must not be serving while its schema is replaced.
            stop_if_running(nginx, NGINX_CONTAINER)
            stop_if_running(container, SERVICE_CONTAINER)

            key = f"{str(event.params['source']).strip('/')}/central.dump"
            payload = io.BytesIO()
            try:
                self._s3_client(s3).download_fileobj(s3["bucketName"], key, payload)
            except Exception as exc:  # noqa: BLE001 - boto3 raises many types
                event.fail(f"Could not download s3://{s3['bucketName']}/{key}: {exc}")
                return

            container.push(DUMP_PATH, payload.getvalue(), make_dirs=True)

            try:
                process = container.exec(
                    ["sh", "-c", RESTORE_SCRIPT],
                    environment={**environment, "DUMP": DUMP_PATH},
                    timeout=BACKUP_TIMEOUT,
                    combine_stderr=True,
                )
                output, _ = process.wait_output()
            except ops.pebble.ExecError as exc:
                text = str(exc.stdout or "")
                if "server version mismatch" in text:
                    event.fail(VERSION_MISMATCH_HINT)
                else:
                    event.fail(f"pg_restore failed: {_reason(exc)}")
                return

            event.set_results(
                {
                    "result": "database restored",
                    "output": _tail(output),
                    "note": (
                        "Submission attachments in the blob store were not part of this restore."
                    ),
                }
            )
        finally:
            with contextlib.suppress(ops.pebble.PathError):
                container.remove_path(DUMP_PATH)
            # Bring everything back up, migrations included.
            self._reconcile(migrate=True)

    def _database_has_tables(self, container: ops.Container, environment: dict[str, str]) -> bool:
        """Return whether the target database already has application tables."""
        try:
            process = container.exec(
                [
                    "psql",
                    "-tAc",
                    "select count(*) from information_schema.tables where table_schema = 'public'",
                ],
                environment=environment,
                timeout=60,
            )
            output, _ = process.wait_output()
        except ops.pebble.ExecError:
            # If the check itself cannot run, assume the database is populated:
            # refusing is the safe direction.
            return True
        digits = "".join(c for c in output if c.isdigit())
        return bool(digits) and int(digits) > 0

    def _pending_blob_count(self, container: ops.Container) -> int | None:
        """Return how many blobs are still waiting to move to S3, if knowable.

        Counted with a direct query rather than with `s3.js count-blobs`, which
        requires the pgrowlocks extension. The database charm does not install
        it and the relation user cannot add it, so upstream's counter is not
        usable against a charm-managed database.
        """
        database = self._database_config()
        if database is None:
            return None
        try:
            process = container.exec(
                [
                    "psql",
                    "--no-psqlrc",
                    "--tuples-only",
                    "--no-align",
                    "--command",
                    "select count(*) from blobs where s3_status = 'pending'",
                ],
                environment=self._backup_environment(database),
                timeout=60,
            )
            output, _ = process.wait_output()
        except ops.pebble.ExecError:
            return None
        digits = "".join(c for c in output if c.isdigit())
        return int(digits) if digits else None

    def _run_migrations(self, container: ops.Container) -> str:
        """Run the database migrations and return their output.

        Run with ``exec`` rather than as a one-shot Pebble service because that
        is the only way to see the exit code: Pebble reports a service's state,
        not the status its process exited with, and a migration that fails
        silently is exactly the failure worth catching.

        :raises MigrationError: if the migrations exit non-zero.
        """
        process = container.exec(
            MIGRATIONS_COMMAND,
            working_dir=WORKING_DIR,
            environment=self._service_environment(),
            timeout=MIGRATION_TIMEOUT,
            combine_stderr=True,
        )
        try:
            output, _ = process.wait_output()
        except ops.pebble.ExecError as exc:
            raise MigrationError(exc.exit_code, str(exc.stdout or "")) from exc
        logger.info("database migrations completed")
        return output


if __name__ == "__main__":  # pragma: no cover
    ops.main(OdkCentralCharm)
