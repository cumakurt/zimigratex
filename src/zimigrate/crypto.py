from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from zimigrate.errors import ArchiveError, ConfigurationError
from zimigrate.util import atomic_output

MAGIC = b"ZMG1"
NONCE_SIZE = 12
TAG_SIZE = 16
HEADER_SIZE = len(MAGIC) + NONCE_SIZE + TAG_SIZE
KEYCHECK_AAD = b"zimigrate-key-check-v1"
KEYCHECK_VALUE = b"zimigrate archive key is valid\n"


class CryptoBox:
    """Streaming AES-256-GCM encryption for archive records and mailbox blobs."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    @classmethod
    def derive(cls, passphrase: str, salt: bytes) -> CryptoBox:
        if len(passphrase) < 16:
            raise ConfigurationError("Archive passphrase must contain at least 16 characters")
        kdf = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1)
        return cls(kdf.derive(passphrase.encode("utf-8")))

    def encrypt_bytes(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_SIZE)
        encryptor = Cipher(algorithms.AES(self._key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()
        return MAGIC + nonce + encryptor.tag + ciphertext

    def decrypt_bytes(self, payload: bytes, aad: bytes) -> bytes:
        if len(payload) < HEADER_SIZE or payload[: len(MAGIC)] != MAGIC:
            raise ArchiveError("Encrypted archive record has an invalid header")
        nonce = payload[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
        tag = payload[len(MAGIC) + NONCE_SIZE : HEADER_SIZE]
        decryptor = Cipher(algorithms.AES(self._key), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(aad)
        try:
            return decryptor.update(payload[HEADER_SIZE:]) + decryptor.finalize()
        except InvalidTag as exc:
            raise ArchiveError("Archive passphrase is wrong or encrypted data is corrupt") from exc

    def encrypt_file(self, source: Path, destination: Path, aad: bytes) -> str:
        nonce = os.urandom(NONCE_SIZE)
        digest = hashlib.sha256()
        encryptor = Cipher(algorithms.AES(self._key), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(aad)
        with source.open("rb") as input_stream, atomic_output(destination, "w+b") as output_stream:
            output_stream.write(MAGIC + nonce + (b"\0" * TAG_SIZE))
            for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                digest.update(block)
                output_stream.write(encryptor.update(block))
            output_stream.write(encryptor.finalize())
            output_stream.seek(len(MAGIC) + NONCE_SIZE)
            output_stream.write(encryptor.tag)
        return digest.hexdigest()

    def decrypt_file(self, source: Path, destination: Path, aad: bytes) -> None:
        with source.open("rb") as input_stream:
            header = input_stream.read(HEADER_SIZE)
            if len(header) != HEADER_SIZE or header[: len(MAGIC)] != MAGIC:
                raise ArchiveError(f"Encrypted file has an invalid header: {source}")
            nonce = header[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
            tag = header[len(MAGIC) + NONCE_SIZE :]
            decryptor = Cipher(algorithms.AES(self._key), modes.GCM(nonce, tag)).decryptor()
            decryptor.authenticate_additional_data(aad)
            try:
                with atomic_output(destination, "wb") as output_stream:
                    for block in iter(lambda: input_stream.read(1024 * 1024), b""):
                        output_stream.write(decryptor.update(block))
                    output_stream.write(decryptor.finalize())
            except InvalidTag as exc:
                destination.unlink(missing_ok=True)
                raise ArchiveError(
                    f"Archive passphrase is wrong or encrypted file is corrupt: {source}"
                ) from exc
