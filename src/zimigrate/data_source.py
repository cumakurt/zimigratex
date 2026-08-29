"""Compatibility helpers for Zimbra's data-source credential encoding."""

from __future__ import annotations

import base64
import hashlib
from binascii import Error as Base64Error

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from zimigrate.errors import ArchiveError
from zimigrate.models import Attributes

ENCRYPTED_DATA_SOURCE_ATTRIBUTES = {
    "zimbraDataSourcePassword",
    "zimbraDataSourceOAuthToken",
    "zimbraDataSourceOAuthClientSecret",
    "zimbraDataSourceSmtpAuthPassword",
}
ENCODING_VERSION = 1
SALT_SIZE = 16
AES_BLOCK_BITS = 128


def decrypt_data_source_secrets(attributes: Attributes) -> Attributes:
    """Return attributes with Zimbra LDAP credential values converted to plaintext."""
    result = {name: list(values) for name, values in attributes.items()}
    encrypted_names = ENCRYPTED_DATA_SOURCE_ATTRIBUTES.intersection(result)
    if not encrypted_names:
        return result
    identifiers = result.get("zimbraDataSourceId", [])
    if len(identifiers) != 1 or not identifiers[0]:
        raise ArchiveError("Encrypted data source has no unique zimbraDataSourceId")
    data_source_id = identifiers[0]
    for name in encrypted_names:
        result[name] = [_decrypt_secret(value, data_source_id) for value in result[name]]
    return result


def _decrypt_secret(value: str, data_source_id: str) -> str:
    try:
        payload = base64.b64decode(value, validate=True)
        if len(payload) < 1 + SALT_SIZE + (AES_BLOCK_BITS // 8):
            raise ValueError("encoded value is too short")
        if payload[0] != ENCODING_VERSION:
            raise ValueError("encoding version is unsupported")
        salt = payload[1 : 1 + SALT_SIZE]
        ciphertext = payload[1 + SALT_SIZE :]
        # Zimbra's long-standing LDAP data-source encoding (FOSS 8.x-10.x) derives
        # this storage key with MD5. The resulting plaintext is immediately protected
        # by zimigrate's AES-GCM archive.
        key = hashlib.md5(salt + data_source_id.encode("utf-8"), usedforsecurity=False).digest()
        decryptor = Cipher(
            algorithms.AES(key),
            modes.ECB(),  # nosec B305
        ).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(AES_BLOCK_BITS).unpadder()
        return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except (Base64Error, UnicodeDecodeError, ValueError) as exc:
        raise ArchiveError(
            "Data source credential does not use the supported Zimbra encoding"
        ) from exc
