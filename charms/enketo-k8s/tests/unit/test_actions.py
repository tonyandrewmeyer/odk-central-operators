"""Unit tests for the enketo-k8s actions."""

from __future__ import annotations

import pytest
from charm import EnketoCharm
from ops import testing

from conftest import odk_enketo_relation


def redis_exec(return_code: int = 0, stdout: str = "OK", stderr: str = "") -> testing.Exec:
    """Return a fake exec result for redis-cli."""
    return testing.Exec(
        command_prefix=["redis-cli"],
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
    )


def redis_container(name: str, **kwargs: object) -> testing.Container:
    """Return a Redis sidecar container whose redis-cli succeeds."""
    return testing.Container(name, can_connect=True, execs={redis_exec()}, **kwargs)  # type: ignore[arg-type]


# flush-cache


def test_flush_cache_flushes_and_says_what_it_cost(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """Flushing is safe, but it is not free: every form is transformed again."""
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), redis_container("redis-cache")},
        relations={odk_enketo_relation()},
        leader=True,
    )

    ctx.run(ctx.on.action("flush-cache"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["result"] == "OK"
    assert "transformed again" in ctx.action_results["note"]


def test_flush_cache_targets_only_the_cache_instance(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """The durable instance holds in-flight form state and must never be flushed."""
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), redis_container("redis-cache")},
        relations={odk_enketo_relation()},
        leader=True,
    )

    ctx.run(ctx.on.action("flush-cache"), state_in)

    flushes = [
        call for calls in ctx.exec_history.values() for call in calls if "FLUSHALL" in call.command
    ]
    assert len(flushes) == 1
    # 6380 is the cache. Flushing 6379 would destroy submissions in progress.
    assert "6380" in flushes[0].command
    assert "6379" not in flushes[0].command


def test_flush_cache_refuses_a_relation_backed_cache(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """An external Redis is the providing charm's to manage, not this one's."""
    redis = testing.Relation(
        "redis-cache",
        remote_app_name="redis-k8s",
        remote_units_data={0: {"hostname": "redis-k8s-0.redis.svc", "port": "6379"}},
    )
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), redis_container("redis-cache")},
        relations={odk_enketo_relation(), redis},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("flush-cache"), state_in)

    assert "relation-backed" in excinfo.value.message


def test_flush_cache_refuses_when_both_endpoints_are_the_same(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """Relating one Redis to both endpoints would make a flush destructive."""
    same = {0: {"hostname": "redis-k8s-0.redis.svc", "port": "6379"}}
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), redis_container("redis-cache")},
        relations={
            odk_enketo_relation(),
            testing.Relation("redis-main", remote_app_name="redis-k8s", remote_units_data=same),
            testing.Relation("redis-cache", remote_app_name="redis-k8s", remote_units_data=same),
        },
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("flush-cache"), state_in)

    assert "destroy in-flight form state" in excinfo.value.message


def test_flush_cache_needs_the_container(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """The action fails cleanly when Pebble is not reachable."""
    state_in = testing.State(
        containers={
            enketo,
            redis_container("redis-main"),
            testing.Container("redis-cache", can_connect=False),
        },
        relations={odk_enketo_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("flush-cache"), state_in)

    assert "not ready" in excinfo.value.message


def test_flush_cache_reports_a_redis_failure(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """A redis-cli failure fails the action, not the charm."""
    cache = testing.Container(
        "redis-cache",
        can_connect=True,
        execs={redis_exec(return_code=1, stderr="NOAUTH Authentication required")},
    )
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), cache},
        relations={odk_enketo_relation()},
        leader=True,
    )

    with pytest.raises(testing.ActionFailed) as excinfo:
        ctx.run(ctx.on.action("flush-cache"), state_in)

    assert "NOAUTH" in excinfo.value.message


# redis-info


def test_redis_info_returns_both_instances(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """Diagnosis needs both, and they are reported separately."""
    main = testing.Container(
        "redis-main", can_connect=True, execs={redis_exec(stdout="# Server\r\nrole:master")}
    )
    cache = testing.Container(
        "redis-cache", can_connect=True, execs={redis_exec(stdout="# Server\r\nrole:cache")}
    )
    state_in = testing.State(
        containers={enketo, main, cache}, relations={odk_enketo_relation()}, leader=True
    )

    ctx.run(ctx.on.action("redis-info"), state_in)

    assert ctx.action_results is not None
    assert "role:master" in ctx.action_results["main"]
    assert "role:cache" in ctx.action_results["cache"]


def test_redis_info_names_a_relation_backed_instance(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """There is nothing local to query, and saying so is the useful answer."""
    redis = testing.Relation(
        "redis-main",
        remote_app_name="redis-k8s",
        remote_units_data={0: {"hostname": "redis-k8s-0.redis.svc", "port": "6379"}},
    )
    state_in = testing.State(
        containers={enketo, redis_container("redis-main"), redis_container("redis-cache")},
        relations={odk_enketo_relation(), redis},
        leader=True,
    )

    ctx.run(ctx.on.action("redis-info"), state_in)

    assert ctx.action_results is not None
    assert "relation-backed" in ctx.action_results["main"]


def test_redis_info_tolerates_an_unreachable_container(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """One instance being unreachable must not lose the other's output."""
    state_in = testing.State(
        containers={
            enketo,
            testing.Container("redis-main", can_connect=False),
            redis_container("redis-cache"),
        },
        relations={odk_enketo_relation()},
        leader=True,
    )

    ctx.run(ctx.on.action("redis-info"), state_in)

    assert ctx.action_results is not None
    assert ctx.action_results["main"] == "container not ready"
    assert "OK" in ctx.action_results["cache"]


def test_redis_info_reports_a_failed_query(
    ctx: testing.Context[EnketoCharm], enketo: testing.Container
) -> None:
    """A failure is reported per instance rather than failing the action."""
    main = testing.Container(
        "redis-main",
        can_connect=True,
        execs={redis_exec(return_code=1, stderr="Could not connect")},
    )
    state_in = testing.State(
        containers={enketo, main, redis_container("redis-cache")},
        relations={odk_enketo_relation()},
        leader=True,
    )

    ctx.run(ctx.on.action("redis-info"), state_in)

    assert ctx.action_results is not None
    assert "INFO failed" in ctx.action_results["main"]
