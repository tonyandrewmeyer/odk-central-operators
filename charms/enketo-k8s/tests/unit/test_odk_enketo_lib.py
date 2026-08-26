"""Unit tests for the shared secret handling in the odk_enketo charm library.

The three shared secrets have exact byte lengths that upstream's
``start-enketo.sh`` asserts on with ``stat``. Getting one wrong produces a
workload that refuses to start with a message that does not point at the cause,
so the library checks the sizes itself, before anything reaches a container.
"""

from __future__ import annotations

import pytest
from charms.odk_central_k8s.v0.odk_enketo import (
    SECRET_LABEL_API_KEY,
    SECRET_LABEL_ENCRYPTION_KEY,
    SECRET_LABEL_LESS_SECURE_KEY,
    SECRET_LENGTHS,
    EnketoSecrets,
    SecretLengthError,
)


def _valid() -> EnketoSecrets:
    """Return a set of secrets at exactly the required lengths."""
    return EnketoSecrets(
        api_key="a" * 128,
        encryption_key="b" * 64,
        less_secure_key="c" * 32,
    )


def test_upstream_lengths_are_what_start_enketo_asserts() -> None:
    """The declared lengths match upstream's start-enketo.sh assertions."""
    assert SECRET_LENGTHS == {
        SECRET_LABEL_API_KEY: 128,
        SECRET_LABEL_ENCRYPTION_KEY: 64,
        SECRET_LABEL_LESS_SECURE_KEY: 32,
    }


def test_correctly_sized_secrets_validate() -> None:
    """Secrets at the exact lengths pass validation."""
    _valid().validate()


@pytest.mark.parametrize(
    ("field", "value", "label", "expected"),
    [
        ("api_key", "a" * 127, SECRET_LABEL_API_KEY, 128),
        ("api_key", "a" * 129, SECRET_LABEL_API_KEY, 128),
        ("encryption_key", "b" * 63, SECRET_LABEL_ENCRYPTION_KEY, 64),
        ("less_secure_key", "c" * 33, SECRET_LABEL_LESS_SECURE_KEY, 32),
    ],
)
def test_wrong_length_is_rejected(field: str, value: str, label: str, expected: int) -> None:
    """A secret of the wrong length raises, naming the offending secret."""
    secrets = _valid()
    setattr(secrets, field, value)

    with pytest.raises(SecretLengthError) as excinfo:
        secrets.validate()

    assert excinfo.value.label == label
    assert excinfo.value.expected == expected
    assert excinfo.value.actual == len(value)


def test_trailing_newline_is_rejected() -> None:
    """A trailing newline makes a 64-byte secret a 65-byte file, which aborts Enketo."""
    secrets = _valid()
    secrets.encryption_key = "b" * 64 + "\n"

    with pytest.raises(SecretLengthError) as excinfo:
        secrets.validate()

    assert excinfo.value.actual == 65


def test_length_is_measured_in_bytes_not_characters() -> None:
    """A non-ASCII value of the right character count is still the wrong size.

    Upstream stats the file, so what matters is encoded bytes. A ``len()``
    check on the string would wrongly accept this.
    """
    secrets = _valid()
    secrets.less_secure_key = "é" * 32  # 32 characters, 64 bytes

    with pytest.raises(SecretLengthError) as excinfo:
        secrets.validate()

    assert excinfo.value.actual == 64


def test_repr_does_not_disclose_the_values() -> None:
    """Secrets must not leak through a log line that interpolates the object."""
    rendered = repr(_valid())

    assert "a" * 128 not in rendered
    assert "b" * 64 not in rendered
    assert "c" * 32 not in rendered
