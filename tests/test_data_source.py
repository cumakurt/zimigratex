from __future__ import annotations

import base64
import hashlib
import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from zimigrate.data_source import decrypt_data_source_secrets
from zimigrate.errors import ArchiveError


class DataSourceSecretTests(unittest.TestCase):
    def test_zimbra_credential_encoding_is_decrypted_with_its_data_source_id(self) -> None:
        data_source_id = "79d2c81e-8a3b-4b0a-b58d-2f981103eaab"
        encrypted = _zimbra_encrypt("pässword-value", data_source_id)

        result = decrypt_data_source_secrets(
            {
                "zimbraDataSourceId": [data_source_id],
                "zimbraDataSourcePassword": [encrypted],
                "zimbraDataSourceName": ["external"],
            }
        )

        self.assertEqual(result["zimbraDataSourcePassword"], ["pässword-value"])
        self.assertEqual(result["zimbraDataSourceName"], ["external"])

    def test_wrong_data_source_id_is_rejected(self) -> None:
        encrypted = _zimbra_encrypt("secret", "source-id")

        with self.assertRaises(ArchiveError):
            decrypt_data_source_secrets(
                {
                    "zimbraDataSourceId": ["different-id"],
                    "zimbraDataSourcePassword": [encrypted],
                }
            )


def _zimbra_encrypt(value: str, data_source_id: str) -> str:
    salt = bytes(range(16))
    key = hashlib.md5(salt + data_source_id.encode("utf-8"), usedforsecurity=False).digest()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(value.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(bytes([1]) + salt + ciphertext).decode("ascii")


if __name__ == "__main__":
    unittest.main()
