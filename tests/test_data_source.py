from __future__ import annotations

import unittest

from zimigrate.data_source import decrypt_data_source_secrets
from zimigrate.errors import ArchiveError


class DataSourceSecretTests(unittest.TestCase):
    def test_zimbra_credential_encoding_is_decrypted_with_its_data_source_id(self) -> None:
        data_source_id = "79d2c81e-8a3b-4b0a-b58d-2f981103eaab"
        encrypted = "AQABAgMEBQYHCAkKCwwNDg/zVnZc7+MThYWfUuadiETf"

        result = decrypt_data_source_secrets(
            {
                "zimbraDataSourceId": [data_source_id],
                "zimbraDataSourcePassword": [encrypted],
                "zimbraDataSourceName": ["external"],
            }
        )

        self.assertEqual(result["zimbraDataSourcePassword"], ["pässword-value"])
        self.assertEqual(result["zimbraDataSourceName"], ["external"])

    def test_chunked_ldap_base64_credentials_are_decrypted(self) -> None:
        data_source_id = "79d2c81e-8a3b-4b0a-b58d-2f981103eaab"
        encrypted = "AQABAgMEBQYHCAkKCwwNDg8nDbrrhN+rZEyeGLs+X5RW"
        wrapped = "\n".join(encrypted[index : index + 16] for index in range(0, len(encrypted), 16))

        result = decrypt_data_source_secrets(
            {
                "zimbraDataSourceId": [data_source_id],
                "zimbraDataSourcePassword": [wrapped],
            }
        )

        self.assertEqual(result["zimbraDataSourcePassword"], ["secret"])

    def test_wrong_data_source_id_is_rejected(self) -> None:
        encrypted = "AQABAgMEBQYHCAkKCwwNDg8okvDWGqWeRBU8+ZJEe8Lo"

        with self.assertRaises(ArchiveError):
            decrypt_data_source_secrets(
                {
                    "zimbraDataSourceId": ["different-id"],
                    "zimbraDataSourcePassword": [encrypted],
                }
            )


if __name__ == "__main__":
    unittest.main()
