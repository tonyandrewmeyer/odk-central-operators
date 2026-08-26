#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm for Enketo, which renders ODK XForms as web forms."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import ops
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v1.loki_push_api import LogForwarder
from charms.odk_central_k8s.v0.odk_enketo import (
    ENKETO_PORT,
    CentralDetails,
    OdkEnketoRequirer,
    SecretLengthError,
)
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.redis_k8s.v0.redis import RedisRelationCharmEvents, RedisRequires

logger = logging.getLogger(__name__)

ENKETO_CONTAINER = "enketo"
REDIS_MAIN_CONTAINER = "redis-main"
REDIS_CACHE_CONTAINER = "redis-cache"

REDIS_MAIN_PORT = 6379
# Not the default. Enketo's own config expects the cache here, and changing it
# breaks nothing visibly until cache lookups start failing.
REDIS_CACHE_PORT = 6380

TEMPLATE_DIR = Path(__file__).parent / "templates"

# The stock enketo image, unlike upstream's derived build, ships no config.json
# and no start-enketo.sh. ENKETO_SRC_DIR is /srv/src/enketo here, but
# enketo-express reads its configuration from its own package directory.
ENKETO_DIR = "/srv/src/enketo/packages/enketo-express"
ENKETO_CONFIG_PATH = f"{ENKETO_DIR}/config/config.json"
ENKETO_COMMAND = "yarn workspace enketo-express start"

REDIS_CONFIG_PATH = "/usr/local/etc/redis/redis.conf"
REDIS_COMMAND = f"redis-server {REDIS_CONFIG_PATH}"

# Upstream's start-enketo.sh stats these and refuses to start on any other
# size. The charm does not run that script -- the stock image does not contain
# it -- but the files are written anyway, at the same sizes and paths, so that
# the container matches what upstream's tooling expects.
SECRET_FILES = {
    "/etc/secrets/enketo-secret": "encryption_key",
    "/etc/secrets/enketo-less-secret": "less_secure_key",
    "/etc/secrets/enketo-api-key": "api_key",
}

# Neither Enketo nor Redis exposes Prometheus metrics, so the charm ships a
# small exporter. It speaks RESP to both Redis instances over a socket rather
# than shelling out to redis-cli, which the Enketo image does not have -- and
# which also makes a relation-backed instance scrape exactly like a sidecar.
EXPORTER_SERVICE = "exporter"
EXPORTER_PORT = 9104
EXPORTER_SOURCE = TEMPLATE_DIR.parent / "exporter" / "enketo-exporter.js"
EXPORTER_PATH = "/srv/enketo-exporter.js"

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def push_if_changed(
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

    if permissions is not None:
        # Remove first: these are written read-only, and overwriting a 0400
        # file fails for any process that is not root.
        try:
            container.remove_path(path)
        except ops.pebble.PathError:
            pass

    container.push(path, content, make_dirs=True, permissions=permissions)
    return True


def stop_if_running(container: ops.Container, service_name: str) -> None:
    """Stop a Pebble service, tolerating one that was never started."""
    if not container.can_connect():
        return
    service = container.get_services().get(service_name)
    if service is not None and service.is_running():
        container.stop(service_name)


class RedisEndpoint:
    """Where one Redis instance can be reached, and whether it is in-charm."""

    def __init__(self, host: str, port: int, *, related: bool) -> None:
        self.host = host
        self.port = port
        self.related = related

    def __repr__(self) -> str:
        """Return a debugging representation of the endpoint."""
        return f"RedisEndpoint({self.host}:{self.port}, related={self.related})"


class EnketoCharm(ops.CharmBase):
    """Run Enketo and its two Redis instances against secrets from ODK Central."""

    # The redis charm library emits its own event on the charm, so the charm
    # has to carry the library's event source. It is a single event covering
    # both redis relations, which is fine: everything reconciles together.
    on = RedisRelationCharmEvents()

    def __init__(self, framework: ops.Framework) -> None:
        super().__init__(framework)

        self.central = OdkEnketoRequirer(self)
        self.redis_main = RedisRequires(self, relation_name="redis-main")
        self.redis_cache = RedisRequires(self, relation_name="redis-cache")
        self.logging = LogForwarder(self, relation_name="logging")
        self.metrics = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=[{"static_configs": [{"targets": [f"*:{EXPORTER_PORT}"]}]}],
            refresh_event=[self.on.config_changed],
        )
        self.dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")

        for event in (
            self.on.install,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on.leader_elected,
            self.on[ENKETO_CONTAINER].pebble_ready,
            self.on[REDIS_MAIN_CONTAINER].pebble_ready,
            self.on[REDIS_CACHE_CONTAINER].pebble_ready,
            self.central.on.odk_enketo_ready,
            self.central.on.odk_enketo_gone,
            # One event covers both redis relations. Observing the raw
            # relation events as well would reconcile twice per hook.
            self.on.redis_relation_updated,
        ):
            framework.observe(event, self._on_lifecycle_event)

        framework.observe(self.on.flush_cache_action, self._on_flush_cache_action)
        framework.observe(self.on.redis_info_action, self._on_redis_info_action)

    # Event handling

    def _on_lifecycle_event(self, event: ops.EventBase) -> None:
        """Reconcile on any event that could have changed the desired state."""
        self._reconcile()

    # Reconciliation

    def _reconcile(self) -> None:
        """Bring the workloads into line with configuration and relation data.

        Every hook routes through here rather than each handler doing its own
        partial update, so that the charm's behaviour does not depend on which
        event happened to arrive.
        """
        invalid = self._invalid_config()
        if invalid:
            self.unit.status = ops.BlockedStatus(invalid)
            return

        self._reconcile_redis()

        container = self.unit.get_container(ENKETO_CONTAINER)
        if not container.can_connect():
            self.unit.status = ops.WaitingStatus("waiting for the enketo container")
            return

        try:
            central = self._central_details()
        except SecretLengthError as exc:
            # Refuse loudly rather than writing a file the workload will reject
            # with a message that does not point at the cause.
            logger.error("refusing a badly sized shared secret: %s", exc)
            stop_if_running(container, ENKETO_CONTAINER)
            self.unit.status = ops.BlockedStatus(str(exc))
            return

        if central is None:
            # Enketo cannot serve web forms without the shared secrets, and
            # must not keep serving with stale ones.
            stop_if_running(container, ENKETO_CONTAINER)
            self.unit.status = ops.BlockedStatus(
                "waiting for the odk-enketo relation to odk-central-k8s"
            )
            return

        changed = self._push_secret_files(container, central)
        changed |= push_if_changed(
            container,
            ENKETO_CONFIG_PATH,
            json.dumps(self._render_config(central), indent=2),
            permissions=0o600,
        )

        changed |= push_if_changed(container, EXPORTER_PATH, EXPORTER_SOURCE.read_text())

        container.add_layer(ENKETO_CONTAINER, self._enketo_layer(), combine=True)
        container.replan()
        self.unit.set_ports(ENKETO_PORT, EXPORTER_PORT)

        # Enketo reads its configuration once, at startup. replan above only
        # restarts the service if the *layer* changed, so a new config.json or
        # a rotated secret needs an explicit restart -- otherwise the workload
        # keeps running against the values it started with.
        service = container.get_services().get(ENKETO_CONTAINER)
        if changed and service is not None and service.is_running():
            logger.info("enketo configuration changed; restarting the workload")
            container.restart(ENKETO_CONTAINER)

        self.central.publish_url(self._enketo_url())
        self.unit.status = self._status()

    def _central_details(self) -> CentralDetails | None:
        """Return Central's published data.

        :raises SecretLengthError: if a shared secret is the wrong size.
        """
        return self.central.central

    def _status(self) -> ops.StatusBase:
        """Return a status that names which Redis instances are relation-backed."""
        related = [
            name
            for name, endpoint in (
                ("main", self._redis_endpoint(REDIS_MAIN_CONTAINER)),
                ("cache", self._redis_endpoint(REDIS_CACHE_CONTAINER)),
            )
            if endpoint.related
        ]
        if related:
            return ops.ActiveStatus(f"relation-backed redis: {', '.join(related)}")
        return ops.ActiveStatus()

    def _invalid_config(self) -> str | None:
        """Return a message describing the first invalid config option, if any."""
        if str(self.config["log-level"]).upper() not in VALID_LOG_LEVELS:
            return f"log-level must be one of {', '.join(VALID_LOG_LEVELS)}"
        if int(self.config["text-field-character-limit"]) < 1:
            return "text-field-character-limit must be at least 1"
        if not str(self.config["payload-limit"]).strip():
            return "payload-limit must not be empty"
        return None

    # Redis

    def _redis_endpoint(self, container_name: str) -> RedisEndpoint:
        """Return where one Redis instance lives, relation first, sidecar second."""
        if container_name == REDIS_MAIN_CONTAINER:
            requirer, port = self.redis_main, REDIS_MAIN_PORT
        else:
            requirer, port = self.redis_cache, REDIS_CACHE_PORT

        data = requirer.relation_data or {}
        host = data.get("hostname")
        if host:
            return RedisEndpoint(host, int(data.get("port") or port), related=True)
        # The sidecar shares the pod, so it is reachable on localhost.
        return RedisEndpoint("127.0.0.1", port, related=False)

    def _reconcile_redis(self) -> None:
        """Run each Redis sidecar, or stand it down when a relation supplies one."""
        for container_name, template, config_key in (
            (REDIS_MAIN_CONTAINER, "redis-main.conf", "redis-main-maxmemory"),
            (REDIS_CACHE_CONTAINER, "redis-cache.conf", "redis-cache-maxmemory"),
        ):
            container = self.unit.get_container(container_name)
            if not container.can_connect():
                continue

            if self._redis_endpoint(container_name).related:
                # An external Redis is in use; stop paying for the sidecar.
                stop_if_running(container, container_name)
                continue

            changed = push_if_changed(
                container,
                REDIS_CONFIG_PATH,
                self._redis_config(template, str(self.config[config_key])),
            )
            container.add_layer(container_name, self._redis_layer(container_name), combine=True)
            container.replan()

            # redis-server reads its config file once, at startup.
            service = container.get_services().get(container_name)
            if changed and service is not None and service.is_running():
                container.restart(container_name)

    def _redis_config(self, template: str, maxmemory: str) -> str:
        """Return a Redis config file with the configured memory limit applied."""
        config = (TEMPLATE_DIR / template).read_text()
        limit = maxmemory.strip()
        if not limit:
            return config
        # allkeys-lru is only safe on the cache; the durable instance would
        # evict in-flight form state, so it is left to refuse writes instead.
        policy = "allkeys-lru" if template == "redis-cache.conf" else "noeviction"
        return f"{config}\nmaxmemory {limit}\nmaxmemory-policy {policy}\n"

    def _redis_layer(self, container_name: str) -> ops.pebble.Layer:
        """Build the Pebble layer for one Redis sidecar."""
        port = REDIS_MAIN_PORT if container_name == REDIS_MAIN_CONTAINER else REDIS_CACHE_PORT
        return ops.pebble.Layer(
            {
                "summary": f"{container_name} for enketo",
                "services": {
                    container_name: {
                        "override": "replace",
                        "summary": container_name,
                        "command": REDIS_COMMAND,
                        "startup": "enabled",
                        "working-dir": "/data",
                        "on-failure": "restart",
                    },
                },
                "checks": {
                    f"{container_name}-up": {
                        "override": "replace",
                        "level": "ready",
                        "period": "15s",
                        "threshold": 3,
                        "exec": {"command": f"redis-cli -p {port} ping"},
                    },
                },
            }
        )

    # Enketo

    def _enketo_url(self) -> str:
        """Return the URL at which Central should reach this Enketo.

        The Kubernetes Service Juju creates for the application, so the address
        survives scaling and rescheduling. The ``/-`` suffix is Enketo's base
        path, which Central expects to be part of the URL.
        """
        return f"http://{self.app.name}.{self.model.name}.svc.cluster.local:{ENKETO_PORT}/-"

    def _push_secret_files(self, container: ops.Container, central: CentralDetails) -> bool:
        """Write the three shared secrets to disk at their exact byte lengths.

        :returns: whether any of them changed.
        :raises SecretLengthError: before anything is written, if a size is wrong.
        """
        # Validate all three first: a partial write would leave the container
        # in a state that is worse than not writing at all.
        central.secrets.validate()

        changed = False
        for path, attribute in SECRET_FILES.items():
            value: str = getattr(central.secrets, attribute)
            # No trailing newline. A stray one makes a 64-byte secret a 65-byte
            # file, which upstream's startup check rejects.
            changed |= push_if_changed(container, path, value, permissions=0o400)
        return changed

    def _render_config(self, central: CentralDetails) -> dict[str, Any]:
        """Build Enketo's config.json.

        Only the values that differ from Enketo's own defaults are set: the
        stock image's defaults are KoBoCAT's, not ODK's.
        """
        main = self._redis_endpoint(REDIS_MAIN_CONTAINER)
        cache = self._redis_endpoint(REDIS_CACHE_CONTAINER)
        base_url = central.base_url.rstrip("/")
        parsed = urlparse(base_url)

        return {
            "app name": "Enketo",
            "base path": "-",
            "port": str(ENKETO_PORT),
            "encryption key": central.secrets.encryption_key,
            "less secure encryption key": central.secrets.less_secure_key,
            "id length": 31,
            "linked form and data server": {
                "name": "ODK Central",
                # Enketo wants the bare host here, not a URL.
                "server url": parsed.hostname or base_url,
                "api key": central.secrets.api_key,
                "authentication": {
                    "type": "cookie",
                    "url": f"{base_url}/login?next={{RETURNURL}}",
                },
            },
            "redis": {
                "main": {"host": main.host, "port": str(main.port)},
                "cache": {"host": cache.host, "port": str(cache.port)},
            },
            "support": {"email": central.support_email or "support@getodk.org"},
            "offline enabled": bool(self.config["offline-enabled"]),
            "payload limit": str(self.config["payload-limit"]),
            "text field character limit": int(self.config["text-field-character-limit"]),
            "exclude non-relevant": bool(self.config["exclude-non-relevant"]),
            "query parameter to pass to submission": "st",
            "hide powered by": True,
            "logo": {"source": "", "href": ""},
        }

    def _exporter_environment(self) -> dict[str, str]:
        """Return the exporter's view of where the two Redis instances live.

        Supplied by the charm rather than discovered, so that a relation-backed
        instance is scraped in exactly the same way as a sidecar.
        """
        main = self._redis_endpoint(REDIS_MAIN_CONTAINER)
        cache = self._redis_endpoint(REDIS_CACHE_CONTAINER)
        return {
            "EXPORTER_PORT": str(EXPORTER_PORT),
            "ENKETO_PORT": str(ENKETO_PORT),
            "REDIS_MAIN_HOST": main.host,
            "REDIS_MAIN_PORT": str(main.port),
            "REDIS_CACHE_HOST": cache.host,
            "REDIS_CACHE_PORT": str(cache.port),
            "EXPORTER_INTERVAL": "60",
        }

    def _enketo_layer(self) -> ops.pebble.Layer:
        """Build the Pebble layer for the Enketo workload."""
        return ops.pebble.Layer(
            {
                "summary": "enketo web forms",
                "description": "Renders ODK XForms as offline-capable web forms.",
                "services": {
                    ENKETO_CONTAINER: {
                        "override": "replace",
                        "summary": "enketo-express",
                        "command": ENKETO_COMMAND,
                        "startup": "enabled",
                        "working-dir": "/srv/src/enketo",
                        "environment": {
                            "NODE_ENV": "production",
                            "ENKETO_LOG_LEVEL": str(self.config["log-level"]).lower(),
                        },
                        "on-failure": "restart",
                    },
                    EXPORTER_SERVICE: {
                        "override": "replace",
                        "summary": "prometheus exporter",
                        "command": f"node {EXPORTER_PATH}",
                        "startup": "enabled",
                        "environment": self._exporter_environment(),
                        "on-failure": "restart",
                    },
                },
                "checks": {
                    "enketo-up": {
                        "override": "replace",
                        "level": "ready",
                        "period": "15s",
                        "threshold": 5,
                        "tcp": {"port": ENKETO_PORT},
                    },
                },
            }
        )

    # Actions

    def _on_flush_cache_action(self, event: ops.ActionEvent) -> None:
        """Flush the cache Redis instance, and only ever that one."""
        cache = self._redis_endpoint(REDIS_CACHE_CONTAINER)
        main = self._redis_endpoint(REDIS_MAIN_CONTAINER)

        if (cache.host, cache.port) == (main.host, main.port):
            event.fail(
                "The cache and durable Redis endpoints are the same. Flushing would "
                "destroy in-flight form state, so this action refuses to run."
            )
            return

        if cache.related:
            event.fail(
                "The cache is relation-backed. Flush it through the Redis charm "
                "that provides it rather than from here."
            )
            return

        container = self.unit.get_container(REDIS_CACHE_CONTAINER)
        if not container.can_connect():
            event.fail("The redis-cache container is not ready.")
            return

        try:
            process = container.exec(["redis-cli", "-p", str(cache.port), "FLUSHALL"])
            output, _ = process.wait_output()
        except ops.pebble.ExecError as exc:
            event.fail(f"FLUSHALL failed: {exc.stderr or exc.stdout}")
            return

        event.set_results(
            {
                "result": output.strip(),
                "note": "Every form will be transformed again on next use.",
            }
        )

    def _on_redis_info_action(self, event: ops.ActionEvent) -> None:
        """Return INFO from both Redis instances for diagnosis."""
        results: dict[str, str] = {}
        for label, container_name in (
            ("main", REDIS_MAIN_CONTAINER),
            ("cache", REDIS_CACHE_CONTAINER),
        ):
            endpoint = self._redis_endpoint(container_name)
            if endpoint.related:
                results[label] = f"relation-backed at {endpoint.host}:{endpoint.port}"
                continue

            container = self.unit.get_container(container_name)
            if not container.can_connect():
                results[label] = "container not ready"
                continue

            try:
                process = container.exec(["redis-cli", "-p", str(endpoint.port), "INFO"])
                output, _ = process.wait_output()
                results[label] = output
            except ops.pebble.ExecError as exc:
                results[label] = f"INFO failed: {exc.stderr or exc.stdout}"

        event.set_results(results)


if __name__ == "__main__":  # pragma: no cover
    ops.main(EnketoCharm)
