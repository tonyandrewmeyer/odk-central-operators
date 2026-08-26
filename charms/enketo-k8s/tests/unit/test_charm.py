"""Unit tests for the enketo-k8s charm."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
from charm import ENKETO_CONFIG_PATH, SECRET_FILES, EnketoCharm
from charms.odk_central_k8s.v0.odk_enketo import (
    FIELD_BASE_URL,
    FIELD_ENKETO_URL,
    SECRET_LABEL_ENCRYPTION_KEY,
)
from ops import testing

from conftest import (
    API_KEY,
    ENCRYPTION_KEY,
    LESS_SECURE_KEY,
    container_named,
    enketo_config,
    file_path,
    odk_enketo_relation,
    read_file,
    redis_config,
)

# Startup and the shared-secret gate


def test_blocked_without_the_central_relation(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
) -> None:
    """Enketo cannot serve web forms without the shared secrets."""
    state_in = testing.State(containers={enketo, *redis_containers}, leader=True)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "odk-enketo" in state_out.unit_status.message


def test_incomplete_relation_is_not_enough(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    secrets: set[testing.Secret],
) -> None:
    """A relation missing the base URL leaves Enketo blocked, not half-started."""
    relation = odk_enketo_relation()
    relation.remote_app_data.pop(FIELD_BASE_URL)
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={relation},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)


def test_starts_once_central_has_published(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """With the secrets and base URL present, the workload runs."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.ActiveStatus)
    statuses = container_named(state_out, "enketo").service_statuses
    assert statuses["enketo"] == testing.pebble.ServiceStatus.ACTIVE


def test_enketo_is_stopped_when_the_relation_goes_away(
    ctx: testing.Context[EnketoCharm],
    redis_containers: set[testing.Container],
    stops: list[str],
) -> None:
    """Serving with stale credentials is worse than not serving."""
    enketo = testing.Container(
        "enketo",
        can_connect=True,
        layers={
            "enketo": testing.pebble.Layer(
                {
                    "services": {
                        "enketo": {
                            "override": "replace",
                            "command": "yarn workspace enketo-express start",
                            "startup": "enabled",
                        }
                    }
                }
            )
        },
        service_statuses={"enketo": testing.pebble.ServiceStatus.ACTIVE},
    )
    state_in = testing.State(containers={enketo, *redis_containers}, leader=True)

    ctx.run(ctx.on.config_changed(), state_in)

    assert "enketo" in stops


# The secret files


def test_secret_files_are_written_at_the_exact_sizes(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Upstream stats these files and refuses to start on any other size."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    sizes = {
        "/etc/secrets/enketo-secret": 64,
        "/etc/secrets/enketo-less-secret": 32,
        "/etc/secrets/enketo-api-key": 128,
    }
    for path, expected in sizes.items():
        written = file_path(state_out, ctx, "enketo", path)
        assert written.stat().st_size == expected, path


def test_secret_files_have_no_trailing_newline(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """A stray newline makes a 64-byte secret a 65-byte file and aborts startup."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    for path in SECRET_FILES:
        assert not read_file(state_out, ctx, "enketo", path).endswith("\n"), path


def test_secret_files_are_not_readable_by_others(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """These are credentials shared with Central."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    for path in SECRET_FILES:
        mode = file_path(state_out, ctx, "enketo", path).stat().st_mode
        assert (mode & 0o077) == 0, path


def test_a_badly_sized_secret_blocks_before_anything_is_written(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
) -> None:
    """A 65-byte secret must fail in the charm, not as an opaque startup abort."""
    bad = {
        testing.Secret(id="secret:api", tracked_content={"value": API_KEY}, label="a"),
        testing.Secret(
            id="secret:enc",
            tracked_content={"value": ENCRYPTION_KEY + "\n"},
            label=SECRET_LABEL_ENCRYPTION_KEY,
        ),
        testing.Secret(id="secret:less", tracked_content={"value": LESS_SECURE_KEY}, label="c"),
    }
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=bad,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert "64 bytes" in state_out.unit_status.message
    assert not file_path(state_out, ctx, "enketo", ENKETO_CONFIG_PATH).exists()


# The rendered configuration


def test_config_uses_the_shared_secrets(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """The API key must match Central's or every web form returns 403."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = enketo_config(state_out, ctx)
    assert config["encryption key"] == ENCRYPTION_KEY
    assert config["less secure encryption key"] == LESS_SECURE_KEY
    assert config["linked form and data server"]["api key"] == API_KEY


def test_config_points_back_at_central(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Enketo needs a bare host for the server, and a full URL to send logins to."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    linked = enketo_config(state_out, ctx)["linked form and data server"]
    assert linked["server url"] == "odk.example.com"
    assert linked["authentication"]["url"] == ("https://odk.example.com/login?next={RETURNURL}")


def test_config_overrides_enketos_own_defaults(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """The stock image's defaults are KoBoCAT's, not ODK's."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
        config={
            "payload-limit": "10mb",
            "text-field-character-limit": 5000,
            "exclude-non-relevant": False,
            "offline-enabled": False,
        },
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = enketo_config(state_out, ctx)
    assert config["app name"] == "Enketo"
    assert config["base path"] == "-"
    assert config["payload limit"] == "10mb"
    assert config["text field character limit"] == 5000
    assert config["exclude non-relevant"] is False
    assert config["offline enabled"] is False
    assert "kobotoolbox" not in str(config).lower()


def test_config_file_is_not_world_readable(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """config.json contains all three shared secrets."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    mode = file_path(state_out, ctx, "enketo", ENKETO_CONFIG_PATH).stat().st_mode
    assert (mode & 0o077) == 0


# Publishing back to Central


def test_url_is_published_back_to_central(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Central rewrites enketo.url from this and restarts its API."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    url = state_out.get_relation(central.id).local_app_data[FIELD_ENKETO_URL]
    # The Kubernetes Service, not a unit IP, and with Enketo's base path.
    assert url.endswith(".svc.cluster.local:8005/-")


def test_non_leader_does_not_publish(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Only the leader writes application databag content."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=False,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert state_out.get_relation(central.id).local_app_data == {}


# Redis


def test_both_redis_sidecars_run_by_default(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Charmhub has no stable Redis charm, so the default is in-charm."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    for name in ("redis-main", "redis-cache"):
        statuses = container_named(state_out, name).service_statuses
        assert statuses[name] == testing.pebble.ServiceStatus.ACTIVE, name


def test_cache_keeps_the_non_default_port(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """Enketo's own defaults expect the cache on 6380."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert "port 6380" in redis_config(state_out, ctx, "redis-cache")
    assert "port 6379" in redis_config(state_out, ctx, "redis-main")
    redis = enketo_config(state_out, ctx)["redis"]
    assert redis["main"] == {"host": "127.0.0.1", "port": "6379"}
    assert redis["cache"] == {"host": "127.0.0.1", "port": "6380"}


def test_durable_instance_persists_to_the_juju_storage(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """The dump has to land on the mounted storage, not the container overlay."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = redis_config(state_out, ctx, "redis-main")
    assert "dir /data" in config
    assert "dbfilename enketo-main.rdb" in config
    assert "save 300 1" in config


def test_cache_does_not_snapshot(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
) -> None:
    """The cache has no storage, so snapshotting would only cost IO."""
    assert 'save ""' in redis_config(
        ctx.run(
            ctx.on.config_changed(),
            testing.State(
                containers={enketo, *redis_containers},
                relations={central},
                secrets=secrets,
                leader=True,
            ),
        ),
        ctx,
        "redis-cache",
    )


@pytest.mark.parametrize(
    ("container", "config_key", "expected_policy"),
    [
        ("redis-cache", "redis-cache-maxmemory", "allkeys-lru"),
        ("redis-main", "redis-main-maxmemory", "noeviction"),
    ],
)
def test_maxmemory_policy_differs_between_the_instances(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
    container: str,
    config_key: str,
    expected_policy: str,
) -> None:
    """Evicting from the durable instance would lose in-flight form state."""
    state_in = testing.State(
        containers={enketo, *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
        config={config_key: "128mb"},
    )

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    config = redis_config(state_out, ctx, container)
    assert "maxmemory 128mb" in config
    assert f"maxmemory-policy {expected_policy}" in config


def test_a_redis_relation_stands_down_the_sidecar(
    ctx: testing.Context[EnketoCharm],
    central: testing.Relation,
    secrets: set[testing.Secret],
    stops: list[str],
) -> None:
    """When an external Redis is related, stop paying for the in-charm one."""
    running_cache = testing.Container(
        "redis-cache",
        can_connect=True,
        layers={
            "redis-cache": testing.pebble.Layer(
                {
                    "services": {
                        "redis-cache": {
                            "override": "replace",
                            "command": "redis-server /usr/local/etc/redis/redis.conf",
                            "startup": "enabled",
                        }
                    }
                }
            )
        },
        service_statuses={"redis-cache": testing.pebble.ServiceStatus.ACTIVE},
    )
    redis_relation = testing.Relation(
        "redis-cache",
        remote_app_name="redis-k8s",
        remote_units_data={0: {"hostname": "redis-k8s-0.redis.svc", "port": "6379"}},
    )
    state_in = testing.State(
        containers={
            testing.Container("enketo", can_connect=True),
            testing.Container("redis-main", can_connect=True),
            running_cache,
        },
        relations={central, redis_relation},
        secrets=secrets,
        leader=True,
    )

    state_out = ctx.run(ctx.on.relation_changed(redis_relation), state_in)

    assert "redis-cache" in stops
    cache = enketo_config(state_out, ctx)["redis"]["cache"]
    assert cache == {"host": "redis-k8s-0.redis.svc", "port": "6379"}
    assert "relation-backed redis: cache" in str(state_out.unit_status.message)


# Configuration validation


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"log-level": "CHATTY"}, "log-level"),
        ({"text-field-character-limit": 0}, "text-field-character-limit"),
        ({"payload-limit": "  "}, "payload-limit"),
    ],
)
def test_invalid_config_blocks(
    ctx: testing.Context[EnketoCharm],
    enketo: testing.Container,
    redis_containers: set[testing.Container],
    config: dict[str, Any],
    expected: str,
) -> None:
    """Invalid configuration blocks with a message naming the offending option."""
    state_in = testing.State(containers={enketo, *redis_containers}, config=config)

    state_out = ctx.run(ctx.on.config_changed(), state_in)

    assert isinstance(state_out.unit_status, testing.BlockedStatus)
    assert expected in state_out.unit_status.message


# Configuration changes have to reach a running workload


def _enketo_mounts(tmp_path: pathlib.Path) -> dict[str, testing.Mount]:
    """Return mounts making the config and secret files survive between runs.

    Scenario gives each run a fresh mock filesystem, so without these the
    charm would see every file as new and restart the workload every time.
    """
    config_root = tmp_path / "srv"
    secrets_root = tmp_path / "secrets"
    config_root.mkdir()
    secrets_root.mkdir()
    return {
        "srv": testing.Mount(location="/srv", source=config_root),
        "secrets": testing.Mount(location="/etc/secrets", source=secrets_root),
    }


def _running_enketo(mounts: dict[str, testing.Mount]) -> testing.Container:
    """Return an enketo container that is already running."""
    return testing.Container(
        "enketo",
        can_connect=True,
        mounts=mounts,
        layers={
            "enketo": testing.pebble.Layer(
                {
                    "services": {
                        "enketo": {
                            "override": "replace",
                            "command": "yarn workspace enketo-express start",
                            "startup": "enabled",
                            "working-dir": "/srv/src/enketo",
                        }
                    }
                }
            )
        },
        service_statuses={"enketo": testing.pebble.ServiceStatus.ACTIVE},
    )


def test_a_config_change_restarts_enketo(
    ctx: testing.Context[EnketoCharm],
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
    restarts: list[str],
    tmp_path: pathlib.Path,
) -> None:
    """Enketo reads config.json once, at startup.

    Pebble's replan only restarts a service whose layer changed, so without an
    explicit restart the workload keeps running against its old configuration
    -- pointing at a Redis sidecar the charm has just stopped, for instance.
    """
    mounts = _enketo_mounts(tmp_path)

    state = testing.State(
        containers={_running_enketo(mounts), *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
        config={"payload-limit": "1mb"},
    )
    state = ctx.run(ctx.on.config_changed(), state)
    restarts.clear()

    ctx.run(
        ctx.on.config_changed(),
        testing.State(
            containers={_running_enketo(mounts), *redis_containers},
            relations={central},
            secrets=secrets,
            leader=True,
            config={"payload-limit": "25mb"},
        ),
    )

    assert "enketo" in restarts


def test_an_unchanged_config_does_not_restart_enketo(
    ctx: testing.Context[EnketoCharm],
    redis_containers: set[testing.Container],
    central: testing.Relation,
    secrets: set[testing.Secret],
    restarts: list[str],
    tmp_path: pathlib.Path,
) -> None:
    """An unrelated hook must not drop every in-progress web form session."""
    mounts = _enketo_mounts(tmp_path)

    state = testing.State(
        containers={_running_enketo(mounts), *redis_containers},
        relations={central},
        secrets=secrets,
        leader=True,
    )
    state = ctx.run(ctx.on.config_changed(), state)
    restarts.clear()

    ctx.run(
        ctx.on.update_status(),
        testing.State(
            containers={_running_enketo(mounts), *redis_containers},
            relations={central},
            secrets=secrets,
            leader=True,
        ),
    )

    # Scoped to enketo: the redis sidecars have no mounts in this test, so
    # their config files look new on every run.
    assert "enketo" not in restarts
