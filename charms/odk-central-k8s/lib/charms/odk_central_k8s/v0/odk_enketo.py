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

    def publish(self, base_url: str, support_email: str = "") -> None:
        """Grant the three Juju secrets to the relation and publish their IDs.

        Safe to call on every hook: it is a reconcile, not a one-shot, so a
        rotation is published simply by calling it again.
        """
        raise NotImplementedError  # Implemented in the enketo phase.

    @property
    def enketo_url(self) -> str | None:
        """The in-cluster URL Central should use for ``enketo.url``.

        ``None`` until Enketo answers. Central starts with a placeholder rather
        than waiting, because Enketo cannot answer until Central has published
        the secrets.
        """
        raise NotImplementedError  # Implemented in the enketo phase.


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

    @property
    def central(self) -> CentralDetails | None:
        """Central's secrets and base URL, or ``None`` if not yet published."""
        raise NotImplementedError  # Implemented in the enketo phase.

    def publish_url(self, url: str) -> None:
        """Tell Central the URL at which it should reach Enketo."""
        raise NotImplementedError  # Implemented in the enketo phase.
