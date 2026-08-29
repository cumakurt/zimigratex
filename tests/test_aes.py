from __future__ import annotations

import unittest

from zimigrate.aes import aes128_ecb_decrypt, aes128_ecb_encrypt


class AesTests(unittest.TestCase):
    def test_nist_aes128_ecb_block(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        plaintext = bytes.fromhex("00112233445566778899aabbccddeeff")
        ciphertext = aes128_ecb_encrypt(key, plaintext)
        self.assertEqual(ciphertext[:16], bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a"))
        self.assertEqual(aes128_ecb_decrypt(key, ciphertext), plaintext)

    def test_roundtrip_with_padding(self) -> None:
        key = b"0123456789abcdef"
        plaintext = "pässword-value".encode()
        self.assertEqual(aes128_ecb_decrypt(key, aes128_ecb_encrypt(key, plaintext)), plaintext)


if __name__ == "__main__":
    unittest.main()
