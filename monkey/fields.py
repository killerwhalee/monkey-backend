"""Transparent field-level encryption for sensitive secrets at rest.

``EncryptedTextField`` Fernet-encrypts its value before it hits the database and
decrypts it on load, so KIS app keys/secrets are never stored in plaintext. The
key comes from ``settings.FIELD_ENCRYPTION_KEY`` (a dedicated key, NOT derived
from ``DJANGO_SECRET`` — rotating the Django secret must not orphan the creds).

Ciphertext is non-deterministic, so these columns can't be filtered/indexed/
unique'd at the DB level. That's fine here: the encrypted fields are write-only
over the API and never queried.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        key = getattr(settings, "FIELD_ENCRYPTION_KEY", "")
        if not key:
            raise ImproperlyConfigured(
                "FIELD_ENCRYPTION_KEY is not set; cannot encrypt/decrypt secrets."
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


class EncryptedTextField(models.TextField):
    """A ``TextField`` whose value is Fernet-encrypted at rest."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        return _get_fernet().encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value in (None, ""):
            return value
        try:
            return _get_fernet().decrypt(value.encode()).decode()
        except InvalidToken:
            # A value that isn't valid ciphertext (e.g. legacy plaintext or a key
            # mismatch) is surfaced as empty rather than crashing every read.
            return ""

    def to_python(self, value):
        return value
