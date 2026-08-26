"""Charm library for the ``odk-enketo`` interface.

ODK Central and Enketo share three secrets. Upstream's Docker Compose
deployment generates them in a one-shot ``secrets`` container that writes three
files into a volume mounted by both services. Juju has no shared volume between
applications, so this interface replaces that container: ``odk-central-k8s``
generates the values, stores them as application-owned Juju secrets, grants
them to the related application, and sends the secret IDs across the relation.

The relation is bidirectional, and the ordering matters:

1. On ``relation-created`` the provider grants and publishes the three secret
   IDs plus the deployment's public base URL. It does **not** wait for Enketo.
2. The requirer reads the secrets, writes them to disk, renders Enketo's
   config and starts the workload.
3. The requirer publishes the URL at which Central should reach Enketo.
4. The provider re-renders its own config with that URL and restarts.

Central must not block its own startup on step 3, or the group deadlocks:
Enketo cannot start until Central has published the secrets in step 1.

## The secret lengths are load-bearing

Upstream's ``start-enketo.sh`` asserts with ``stat -c "%s"`` that the three
secret files are **exactly** 64, 32 and 128 bytes. A trailing newline makes a
64-byte secret a 65-byte file and Enketo refuses to start, with a message that
does not obviously point at the cause. The provider generates values at exactly
those lengths and the requirer re-checks before writing anything to disk.

## Provider (``odk-central-k8s``)

```python
from charms.odk_central_k8s.v0.odk_enketo import OdkEnketoProvider


class CentralCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.enketo = OdkEnketoProvider(self, relation_name="odk-enketo")
        framework.observe(self.enketo.on.enketo_url_changed, self._reconcile)
```

## Requirer (``enketo-k8s``)

```python
from charms.odk_central_k8s.v0.odk_enketo import OdkEnketoRequirer


class EnketoCharm(ops.CharmBase):
    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.central = OdkEnketoRequirer(self, relation_name="odk-enketo")
        framework.observe(self.central.on.odk_enketo_ready, self._reconcile)
        framework.observe(self.central.on.odk_enketo_gone, self._reconcile)
```
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import ops

if TYPE_CHECKING:
    from typing import Final

# The unique Charmhub library identifier, never change it.
LIBID = "7f88e6dea94765e19e1c1aae693ec666"

# Increment this if you change the major API version of the library.
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib`, or reset
# it to 1 if you are raising the major API version.
LIBPATCH = 1

PYDEPS: list[str] = []

DEFAULT_RELATION_NAME: Final = "odk-enketo"
"""The endpoint name both charms use for this interface by convention."""

ENKETO_PORT: Final = 8005
"""The port Enketo listens on, per upstream's ``config.json.template``."""

SECRET_LABEL_API_KEY: Final = "odk-enketo-api-key"
SECRET_LABEL_ENCRYPTION_KEY: Final = "odk-enketo-secret"
SECRET_LABEL_LESS_SECURE_KEY: Final = "odk-enketo-less-secret"

SECRET_LENGTHS: Final = {
    SECRET_LABEL_API_KEY: 128,
    SECRET_LABEL_ENCRYPTION_KEY: 64,
    SECRET_LABEL_LESS_SECURE_KEY: 32,
}
"""Exact byte lengths upstream's ``start-enketo.sh`` asserts on."""

logger = logging.getLogger(__name__)


class SecretLengthError(ValueError):
    """A shared secret is not exactly the length Enketo requires.

    Raised before anything is written to a container, so that a bad value fails
    loudly in the charm rather than as an opaque workload startup abort.
    """

    def __init__(self, label: str, expected: int, actual: int) -> None:
        super().__init__(
            f"{label} must be exactly {expected} bytes, got {actual}. "
            f"Enketo's start-enketo.sh asserts on this size and will refuse to start."
        )
        self.label = label
        self.expected = expected
        self.actual = actual


class EnketoSecrets:
    """The three values Central shares with Enketo.

    Held together in one object because they are always rotated together: a
    rotation that updates some but not all of them leaves the two workloads
    disagreeing, which surfaces to users as a 403 on every web form.
    """

    def __init__(self, api_key: str, encryption_key: str, less_secure_key: str) -> None:
        self.api_key = api_key
        self.encryption_key = encryption_key
        self.less_secure_key = less_secure_key

    def validate(self) -> None:
        """Check every value is exactly the length Enketo asserts on.

        :raises SecretLengthError: if any value is the wrong size in bytes.
        """
        for label, value in (
            (SECRET_LABEL_API_KEY, self.api_key),
            (SECRET_LABEL_ENCRYPTION_KEY, self.encryption_key),
            (SECRET_LABEL_LESS_SECURE_KEY, self.less_secure_key),
        ):
            expected = SECRET_LENGTHS[label]
            # Bytes, not characters: a non-ASCII value would pass a len() check
            # and still fail upstream's stat().
            actual = len(value.encode())
            if actual != expected:
                raise SecretLengthError(label, expected, actual)

    def __eq__(self, other: object) -> bool:
        """Compare two secret sets by value."""
        if not isinstance(other, EnketoSecrets):
            return NotImplemented
        return (
            self.api_key == other.api_key
            and self.encryption_key == other.encryption_key
            and self.less_secure_key == other.less_secure_key
        )

    def __hash__(self) -> int:
        """Hash the secret set by value."""
        return hash((self.api_key, self.encryption_key, self.less_secure_key))

    def __repr__(self) -> str:
        """Return a representation that never discloses the values."""
        return "EnketoSecrets(api_key=***, encryption_key=***, less_secure_key=***)"


class CentralDetails:
    """Everything Enketo needs from Central to render its config."""

    def __init__(self, secrets: EnketoSecrets, base_url: str, support_email: str = "") -> None:
        self.secrets = secrets
        self.base_url = base_url
        self.support_email = support_email

    def __repr__(self) -> str:
        """Return a representation that never discloses the secrets."""
        return f"CentralDetails(base_url={self.base_url!r}, secrets=***)"


class OdkEnketoReadyEvent(ops.EventBase):
    """Central has published the shared secrets and the public base URL.

    Emitted on the requirer once every field is present, and again whenever any
    of them changes — including on secret rotation, which arrives as a
    ``secret-changed`` event rather than a ``relation-changed`` one.
    """


class OdkEnketoGoneEvent(ops.EventBase):
    """Central has withdrawn the shared secrets, or the relation was broken.

    Enketo cannot serve web forms without them and should stop rather than
    keep serving with stale credentials.
    """


class EnketoUrlChangedEvent(ops.EventBase):
    """Enketo has advertised the URL at which Central should reach it.

    Emitted on the provider. Central re-renders ``config.json`` with the real
    ``enketo.url`` in place of its startup placeholder and restarts the API,
    but does not restart nginx.
    """


class OdkEnketoRequirerEvents(ops.ObjectEvents):
    """Events emitted by :class:`OdkEnketoRequirer`."""

    odk_enketo_ready = ops.EventSource(OdkEnketoReadyEvent)
    odk_enketo_gone = ops.EventSource(OdkEnketoGoneEvent)


class OdkEnketoProviderEvents(ops.ObjectEvents):
    """Events emitted by :class:`OdkEnketoProvider`."""

    enketo_url_changed = ops.EventSource(EnketoUrlChangedEvent)


# Databag keys. The secrets themselves never cross the relation: only the IDs
# of Juju secrets that the provider has granted to the requirer.
FIELD_API_KEY_ID = "api-key-secret-id"
FIELD_ENCRYPTION_KEY_ID = "encryption-key-secret-id"
FIELD_LESS_SECURE_KEY_ID = "less-secure-key-secret-id"
FIELD_BASE_URL = "base-url"
FIELD_SUPPORT_EMAIL = "support-email"
FIELD_ENKETO_URL = "enketo-url"

SECRET_ID_FIELDS = {
    SECRET_LABEL_API_KEY: FIELD_API_KEY_ID,
    SECRET_LABEL_ENCRYPTION_KEY: FIELD_ENCRYPTION_KEY_ID,
    SECRET_LABEL_LESS_SECURE_KEY: FIELD_LESS_SECURE_KEY_ID,
}


class OdkEnketoProvider(ops.Object):
    """Provider side of ``odk-enketo``, implemented by ``odk-central-k8s``."""

    on = OdkEnketoProviderEvents()

    def __init__(
        self,
        charm: ops.CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
    ) -> None:
        super().__init__(charm, f"odk-enketo-provider-{relation_name}")
        self._charm = charm
        self._relation_name = relation_name

        events = charm.on[relation_name]
        self.framework.observe(events.relation_changed, self._on_relation_changed)
        self.framework.observe(events.relation_broken, self._on_relation_changed)

    def publish(self, base_url: str, support_email: str = "") -> None:
        """Grant the three Juju secrets to the relation and publish their IDs.

        Safe to call on every hook: it is a reconcile, not a one-shot, so a
        rotation is published simply by calling it again.
        """
        if not self._charm.unit.is_leader():
            return

        for relation in self._charm.model.relations.get(self._relation_name, ()):
            databag = {FIELD_BASE_URL: base_url, FIELD_SUPPORT_EMAIL: support_email}
            for label, field in SECRET_ID_FIELDS.items():
                secret = self._charm.model.get_secret(label=label)
                # Granting is idempotent, and has to happen before the requirer
                # can read the secret by ID.
                secret.grant(relation)
                if secret.id is None:  # pragma: no cover - defensive
                    raise RuntimeError(f"secret {label} has no ID to publish")
                databag[field] = secret.id
            relation.data[self._charm.app].update(databag)

    @property
    def enketo_url(self) -> str | None:
        """The in-cluster URL Central should use for ``enketo.url``.

        ``None`` until Enketo answers. Central starts with a placeholder rather
        than waiting, because Enketo cannot answer until Central has published
        the secrets.
        """
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None or relation.app is None:
            return None
        return relation.data[relation.app].get(FIELD_ENKETO_URL) or None

    def _on_relation_changed(self, event: ops.RelationEvent) -> None:
        """Tell the charm that Enketo's advertised URL may have changed."""
        self.on.enketo_url_changed.emit()


class OdkEnketoRequirer(ops.Object):
    """Requirer side of ``odk-enketo``, implemented by ``enketo-k8s``."""

    on = OdkEnketoRequirerEvents()

    def __init__(
        self,
        charm: ops.CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
    ) -> None:
        super().__init__(charm, f"odk-enketo-requirer-{relation_name}")
        self._charm = charm
        self._relation_name = relation_name

        events = charm.on[relation_name]
        self.framework.observe(events.relation_changed, self._on_relation_changed)
        self.framework.observe(events.relation_broken, self._on_relation_broken)
        # A rotation arrives as secret-changed, not relation-changed: the IDs in
        # the databag stay the same while their contents move on.
        self.framework.observe(charm.on.secret_changed, self._on_secret_changed)

    @property
    def central(self) -> CentralDetails | None:
        """Central's secrets and base URL, or ``None`` if not yet published."""
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None or relation.app is None:
            return None
        return self._read(relation)

    def _read(self, relation: ops.Relation) -> CentralDetails | None:
        """Parse one relation's databag, tolerating an incomplete one."""
        if relation.app is None:
            return None
        databag = relation.data[relation.app]

        base_url = databag.get(FIELD_BASE_URL)
        if not base_url:
            return None

        values: dict[str, str] = {}
        for label, field in SECRET_ID_FIELDS.items():
            secret_id = databag.get(field)
            if not secret_id:
                return None
            try:
                content = self._charm.model.get_secret(id=secret_id).get_content(refresh=True)
            except (ops.SecretNotFoundError, ops.ModelError):
                # Normal between the ID being published and the grant landing.
                logger.debug("secret %s for %s is not readable yet", secret_id, field)
                return None
            values[label] = content["value"]

        secrets = EnketoSecrets(
            api_key=values[SECRET_LABEL_API_KEY],
            encryption_key=values[SECRET_LABEL_ENCRYPTION_KEY],
            less_secure_key=values[SECRET_LABEL_LESS_SECURE_KEY],
        )
        # Fail here rather than after writing a bad file into the container.
        secrets.validate()

        return CentralDetails(
            secrets=secrets,
            base_url=base_url,
            support_email=databag.get(FIELD_SUPPORT_EMAIL, ""),
        )

    def publish_url(self, url: str) -> None:
        """Tell Central the URL at which it should reach Enketo."""
        if not self._charm.unit.is_leader():
            return
        for relation in self._charm.model.relations.get(self._relation_name, ()):
            relation.data[self._charm.app][FIELD_ENKETO_URL] = url

    def _on_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Emit ready or gone as Central's published data changes."""
        if self._read(event.relation) is None:
            self.on.odk_enketo_gone.emit()
        else:
            self.on.odk_enketo_ready.emit()

    def _on_relation_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Emit gone when Central goes away."""
        self.on.odk_enketo_gone.emit()

    def _on_secret_changed(self, event: ops.SecretChangedEvent) -> None:
        """Re-read the secrets when Central rotates them."""
        relation = self._charm.model.get_relation(self._relation_name)
        if relation is None or relation.app is None:
            return
        databag = relation.data[relation.app]
        if event.secret.id not in {databag.get(field) for field in SECRET_ID_FIELDS.values()}:
            return
        if self._read(relation) is None:
            self.on.odk_enketo_gone.emit()
        else:
            self.on.odk_enketo_ready.emit()
