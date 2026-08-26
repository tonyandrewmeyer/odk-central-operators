"""Charm library for the ``odk-xlsform`` interface.

XLSForm conversion is the first step in publishing an ODK form: a spreadsheet
is turned into an XForm before Central will accept it. ``pyxform-k8s`` runs
that converter, and ``odk-central-k8s`` needs its address in order to write the
``xlsform`` stanza of its ``config.json``.

The interface is deliberately small. The provider advertises where its HTTP
service can be reached; the requirer consumes it. There are no credentials —
the converter is stateless, unauthenticated and reachable only in-cluster.

## Provider (``pyxform-k8s``)

```python
from charms.pyxform_k8s.v0.xlsform import XlsformProvider


class PyxformCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.xlsform = XlsformProvider(self, relation_name="xlsform")
        # Re-advertise whenever the address or port could have changed.
        framework.observe(self.on.pyxform_pebble_ready, self._on_ready)

    def _on_ready(self, event: ops.PebbleReadyEvent) -> None:
        self.xlsform.publish(host=self._bind_address(), port=80)
```

## Requirer (``odk-central-k8s``)

```python
from charms.pyxform_k8s.v0.xlsform import XlsformProvider, XlsformRequirer


class CentralCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.xlsform = XlsformRequirer(self, relation_name="xlsform")
        framework.observe(self.xlsform.on.xlsform_ready, self._reconcile)
        framework.observe(self.xlsform.on.xlsform_gone, self._reconcile)

    def _reconcile(self, event: ops.EventBase) -> None:
        endpoint = self.xlsform.endpoint  # None while the relation is settling
```

Both sides emit custom events rather than making the charm interpret
``relation-changed`` itself: a charm should react to "the converter moved", not
to "some key in some databag changed".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import ops

if TYPE_CHECKING:
    from typing import Final

# The unique Charmhub library identifier, never change it.
LIBID = "154738bbab8937a617b0946ca2691a32"

# Increment this if you change the major API version of the library.
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib`, or reset
# it to 1 if you are raising the major API version.
LIBPATCH = 1

PYDEPS: list[str] = []

DEFAULT_RELATION_NAME: Final = "xlsform"
"""The endpoint name both charms use for this interface by convention."""

DEFAULT_PORT: Final = 80
"""The port ``pyxform-http`` listens on in its published image."""

logger = logging.getLogger(__name__)


class XlsformEndpoint:
    """Where the XLSForm conversion service can be reached.

    This maps directly onto the ``xlsform`` stanza of ODK Central's
    ``config.json``, which wants a bare host and a port rather than a URL.
    """

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def __eq__(self, other: object) -> bool:
        """Compare two endpoints by host and port."""
        if not isinstance(other, XlsformEndpoint):
            return NotImplemented
        return self.host == other.host and self.port == other.port

    def __hash__(self) -> int:
        """Hash the endpoint by host and port."""
        return hash((self.host, self.port))

    def __repr__(self) -> str:
        """Return a debugging representation of the endpoint."""
        return f"XlsformEndpoint(host={self.host!r}, port={self.port})"


class XlsformReadyEvent(ops.EventBase):
    """The XLSForm converter has advertised a usable address.

    Emitted on the requirer when the provider's databag first becomes complete,
    and again whenever the advertised host or port changes.
    """


class XlsformGoneEvent(ops.EventBase):
    """The XLSForm converter is no longer available.

    Emitted on the requirer when the relation is broken, or when the provider
    withdraws its address. Central cannot publish new forms in this state, but
    existing forms and submissions are unaffected, so the charm should degrade
    rather than stop.
    """


class XlsformRequirerEvents(ops.ObjectEvents):
    """Events emitted by :class:`XlsformRequirer`."""

    xlsform_ready = ops.EventSource(XlsformReadyEvent)
    xlsform_gone = ops.EventSource(XlsformGoneEvent)


class XlsformProvider(ops.Object):
    """Provider side of ``odk-xlsform``, implemented by ``pyxform-k8s``."""

    def __init__(
        self,
        charm: ops.CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
    ) -> None:
        super().__init__(charm, f"xlsform-provider-{relation_name}")
        self._charm = charm
        self._relation_name = relation_name

    def publish(self, host: str, port: int = DEFAULT_PORT) -> None:
        """Advertise the converter's address on every ``xlsform`` relation.

        Only the leader may write to the application databag; on a non-leader
        unit this is a no-op, which is correct because the leader will have
        published the same value.
        """
        if not self._charm.unit.is_leader():
            return
        for relation in self._charm.model.relations.get(self._relation_name, ()):
            relation.data[self._charm.app].update({"host": host, "port": str(port)})

    def withdraw(self) -> None:
        """Remove the advertised address, telling Central the converter is down."""
        if not self._charm.unit.is_leader():
            return
        for relation in self._charm.model.relations.get(self._relation_name, ()):
            databag = relation.data[self._charm.app]
            for key in ("host", "port"):
                databag.pop(key, None)


class XlsformRequirer(ops.Object):
    """Requirer side of ``odk-xlsform``, implemented by ``odk-central-k8s``."""

    on = XlsformRequirerEvents()

    def __init__(
        self,
        charm: ops.CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
    ) -> None:
        super().__init__(charm, f"xlsform-requirer-{relation_name}")
        self._charm = charm
        self._relation_name = relation_name

        events = charm.on[relation_name]
        self.framework.observe(events.relation_changed, self._on_relation_changed)
        self.framework.observe(events.relation_broken, self._on_relation_broken)

    @property
    def endpoint(self) -> XlsformEndpoint | None:
        """The converter's address, or ``None`` if it is not yet known.

        ``None`` is the normal state early in a deployment and is not an error:
        the charm should report ``WaitingStatus`` and try again when the
        ``xlsform_ready`` event arrives.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None or relation.app is None:
            return None
        return self._read(relation)

    def _read(self, relation: ops.Relation) -> XlsformEndpoint | None:
        """Parse one relation's databag, tolerating an incomplete or bad one."""
        if relation.app is None:
            return None
        databag = relation.data[relation.app]
        host = databag.get("host")
        port = databag.get("port")
        if not host or not port:
            # Normal while the relation is settling, not an error.
            return None
        try:
            return XlsformEndpoint(host=host, port=int(port))
        except ValueError:
            # A malformed port is the provider's bug. Treat the converter as
            # absent rather than raising, so this charm stays reconcilable.
            logger.warning("ignoring malformed xlsform port %r from %s", port, relation.app.name)
            return None

    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Emit ``xlsform_ready`` or ``xlsform_gone`` as the databag changes."""
        if self._read(event.relation) is None:
            self.on.xlsform_gone.emit()
        else:
            self.on.xlsform_ready.emit()

    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Emit ``xlsform_gone`` when the relation is removed."""
        self.on.xlsform_gone.emit()
