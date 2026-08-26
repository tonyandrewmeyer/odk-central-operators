#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for ODK Central: the API service and the nginx frontend proxy."""

from __future__ import annotations

import json
import logging
import secrets
import string
from typing import Any

import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.odk_central_k8s.v0.odk_enketo import (
    SECRET_LABEL_API_KEY,
    SECRET_LABEL_ENCRYPTION_KEY,
    SECRET_LABEL_LESS_SECURE_KEY,
    SECRET_LENGTHS,
    EnketoSecrets,
)
from charms.pyxform_k8s.v0.xlsform import XlsformRequirer

logger = logging.getLogger(__name__)

SERVICE_CONTAINER = "service"
NGINX_CONTAINER = "nginx"

API_PORT = 8383

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

# /v1/config/public is the only unauthenticated endpoint that also touches the
# database, so it proves the API and its database are both working. There is no
# /v1/version.json: the deployed version is a static file served by nginx.
HEALTH_PATH = "/v1/config/public"

MIGRATION_TIMEOUT = 3600

DATABASE_NAME = "odk"

# Placeholder used until Enketo answers over the relation. Central must not
# block on Enketo, because Enketo cannot start until Central has published the
# shared secrets: waiting for each other would deadlock the group.
ENKETO_URL_PLACEHOLDER = "http://enketo.invalid:8005/-"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")

# ASCII only, so that a character count is also a byte count. Upstream asserts
# on the byte length of the secret files.
SECRET_ALPHABET = string.ascii_letters + string.digits


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
        ):
            framework.observe(event, self._on_lifecycle_event)

    # Event handling

    def _on_lifecycle_event(self, event: ops.EventBase) -> None:
        """Reconcile on any event that could have changed the desired state."""
        self._reconcile()

    # Reconciliation

    def _reconcile(self) -> None:
        """Bring the workloads into line with configuration and relation data.

        Every hook routes through here rather than each handler doing its own
        partial update. That matters more in this charm than in most: its
        configuration depends on several relations that settle in any order, so
        per-relation handlers would each act on a different partial view.
        """
        invalid = self._invalid_config()
        if invalid:
            self.unit.status = ops.BlockedStatus(invalid)
            return

        container = self.unit.get_container(SERVICE_CONTAINER)
        if not container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for the service container")
            return

        shared_secrets = self._shared_secrets()
        if shared_secrets is None:
            self.unit.status = ops.WaitingStatus("waiting for the leader to generate secrets")
            return

        database = self._database_config()
        if database is None:
            self.unit.status = ops.WaitingStatus("waiting for the postgresql relation")
            return

        container.push(
            CONFIG_PATH,
            json.dumps(self._render_service_config(database, shared_secrets), indent=2),
            make_dirs=True,
            permissions=0o600,
        )

        self.unit.status = ops.MaintenanceStatus("running database migrations")
        try:
            self._run_migrations(container)
        except MigrationError as exc:
            logger.error("database migrations failed (%s):\n%s", exc.exit_code, exc.output)
            self.unit.status = ops.BlockedStatus("database migration failed; see juju debug-log")
            return

        container.add_layer(SERVICE_CONTAINER, self._service_layer(), combine=True)
        container.replan()
        self.unit.set_ports(API_PORT)

        self.unit.status = self._status()

    def _status(self) -> ops.StatusBase:
        """Return the status that reflects what is and is not yet wired up."""
        if self.xlsform.endpoint is None:
            return ops.ActiveStatus("api ready; no xlsform relation, form publishing will fail")
        return ops.ActiveStatus("api ready; awaiting enketo")

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

    def _base_url(self) -> str:
        """Return the public base URL Central should advertise."""
        hostname = str(self.config["external-hostname"]).strip()
        if hostname:
            return f"https://{hostname}"
        # No ingress and no configured hostname yet. Central needs *something*
        # here; links it generates will be wrong until one is set.
        return f"http://localhost:{API_PORT}"

    def _email_config(self) -> dict[str, Any]:
        """Return the ``email`` stanza.

        Without an smtp relation this points at a local relay that is not
        there, which is deliberate: Central only sends mail on account
        creation, password reset and project invitation, so those operations
        fail while everything else keeps working. An empty host would risk
        failing at transport construction, which happens at startup.
        """
        hostname = str(self.config["external-hostname"]).strip() or "localhost"
        sender = str(self.config["email-from"]).strip() or f"no-reply@{hostname}"
        return {
            "serviceAccount": sender,
            "transport": "smtp",
            "transportOpts": {
                "host": "localhost",
                "port": 25,
                "secure": False,
                "ignoreTLS": True,
                "auth": {"user": "", "pass": ""},
            },
        }

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
                    "url": ENKETO_URL_PLACEHOLDER,
                    "apiKey": shared_secrets.api_key,
                },
                "env": {
                    "domain": self._base_url(),
                    "sysadminAccount": str(self.config["sysadmin-email"]),
                },
                "oidc": {
                    "enabled": False,
                    "issuerUrl": "",
                    "clientId": "",
                    "clientSecret": "",
                },
                "external": {
                    "sentry": self._sentry_config(),
                    "s3blobStore": {
                        "server": "",
                        "accessKey": "",
                        "secretKey": "",
                        "bucketName": "",
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
