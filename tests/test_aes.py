from __future__ import annotations

import unittest

from zimigrate.aes import aes128_ecb_decrypt


class AesTests(unittest.TestCase):
    def test_nist_aes128_ecb_block(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        ciphertext = bytes.fromhex(
            "69c4e0d86a7b0430d8cdb78070b4c55a954f64f2e4e86e9eee82d20216684899"
        )
        self.assertEqual(aes128_ecb_decrypt(key, ciphertext), plaintext)

    def test_rejects_invalid_padding(self) -> None:
        key = b"0123456789abcdef"
        with self.assertRaises(ValueError):
            aes128_ecb_decrypt(key, bytes(16))


if __name__ == "__main__":
    unittest.main()
