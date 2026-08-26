"""Shared fixtures and helpers for the enketo-k8s unit tests."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import ops
import pytest
from charm import ENKETO_CONFIG_PATH, REDIS_CONFIG_PATH, EnketoCharm
from charms.odk_central_k8s.v0.odk_enketo import (
    FIELD_API_KEY_ID,
    FIELD_BASE_URL,
    FIELD_ENCRYPTION_KEY_ID,
    FIELD_LESS_SECURE_KEY_ID,
    FIELD_SUPPORT_EMAIL,
    SECRET_LABEL_API_KEY,
    SECRET_LABEL_ENCRYPTION_KEY,
    SECRET_LABEL_LESS_SECURE_KEY,
)
from ops import testing

API_KEY = "a" * 128
ENCRYPTION_KEY = "b" * 64
LESS_SECURE_KEY = "c" * 32
BASE_URL = "https://odk.example.com"


@pytest.fixture
def ctx() -> testing.Context[EnketoCharm]:
    """Return a Scenario context for the charm."""
    return testing.Context(EnketoCharm)


@pytest.fixture
def enketo() -> testing.Container:
    """Return a connectable enketo workload container."""
    return testing.Container("enketo", can_connect=True)


@pytest.fixture
def redis_containers() -> set[testing.Container]:
    """Return both connectable Redis sidecar containers."""
    return {
        testing.Container("redis-main", can_connect=True),
        testing.Container("redis-cache", can_connect=True),
    }


@pytest.fixture
def secrets() -> set[testing.Secret]:
    """Return the three shared secrets as Central would have granted them."""
    return {
        testing.Secret(
            id="secret:api",
            tracked_content={"value": API_KEY},
            label=SECRET_LABEL_API_KEY,
        ),
        testing.Secret(
            id="secret:enc",
            tracked_content={"value": ENCRYPTION_KEY},
            label=SECRET_LABEL_ENCRYPTION_KEY,
        ),
        testing.Secret(
            id="secret:less",
            tracked_content={"value": LESS_SECURE_KEY},
            label=SECRET_LABEL_LESS_SECURE_KEY,
        ),
    }


def odk_enketo_relation(**overrides: str) -> testing.Relation:
    """Return a settled odk-enketo relation from ODK Central."""
    data = {
        FIELD_API_KEY_ID: "secret:api",
        FIELD_ENCRYPTION_KEY_ID: "secret:enc",
        FIELD_LESS_SECURE_KEY_ID: "secret:less",
        FIELD_BASE_URL: BASE_URL,
        FIELD_SUPPORT_EMAIL: "ops-team@example.com",
    }
    data.update(overrides)
    return testing.Relation("odk-enketo", remote_app_name="odk-central-k8s", remote_app_data=data)


@pytest.fixture
def central() -> testing.Relation:
    """Return a settled odk-enketo relation."""
    return odk_enketo_relation()


def container_named(state: testing.State, name: str) -> testing.Container:
    """Return one container from a resulting state by name."""
    return next(c for c in state.containers if c.name == name)


def read_file(
    state: testing.State, ctx: testing.Context[EnketoCharm], container: str, path: str
) -> str:
    """Return the content of a file the charm pushed into a container."""
    root = container_named(state, container).get_filesystem(ctx)
    return (root / path.lstrip("/")).read_text()


def file_path(
    state: testing.State, ctx: testing.Context[EnketoCharm], container: str, path: str
) -> pathlib.Path:
    """Return the on-disk path of a file the charm pushed into a container."""
    return container_named(state, container).get_filesystem(ctx) / path.lstrip("/")


def enketo_config(state: testing.State, ctx: testing.Context[EnketoCharm]) -> dict[str, Any]:
    """Return Enketo's rendered config.json."""
    document: dict[str, Any] = json.loads(read_file(state, ctx, "enketo", ENKETO_CONFIG_PATH))
    return document


def redis_config(state: testing.State, ctx: testing.Context[EnketoCharm], container: str) -> str:
    """Return the Redis config the charm pushed into a sidecar."""
    return read_file(state, ctx, container, REDIS_CONFIG_PATH)


@pytest.fixture
def restarts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every Pebble service restart the charm performs.

    Scenario's resulting state cannot distinguish a service that was restarted
    from one that was already running, so the call itself is what gets asserted.
    """
    calls: list[str] = []
    original = ops.Container.restart

    def spy(self: ops.Container, *service_names: str) -> None:
        calls.extend(service_names)
        original(self, *service_names)

    monkeypatch.setattr(ops.Container, "restart", spy)
    return calls


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every Pebble service the charm stops."""
    calls: list[str] = []
    original = ops.Container.stop

    def spy(self: ops.Container, *service_names: str) -> None:
        calls.extend(service_names)
        original(self, *service_names)

    monkeypatch.setattr(ops.Container, "stop", spy)
    return calls
