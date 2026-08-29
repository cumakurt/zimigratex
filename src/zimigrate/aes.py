"""AES-128 ECB for Zimbra LDAP data-source credential encoding.

zimigrate archives are stored in plaintext. This module only decodes the
AES-ECB format Zimbra uses for selected data-source attributes so the
destination can encrypt them against a newly assigned data-source ID.
"""

from __future__ import annotations

SBOX = bytes(
    (
        0x63,
        0x7C,
        0x77,
        0x7B,
        0xF2,
        0x6B,
        0x6F,
        0xC5,
        0x30,
        0x01,
        0x67,
        0x2B,
        0xFE,
        0xD7,
        0xAB,
        0x76,
        0xCA,
        0x82,
        0xC9,
        0x7D,
        0xFA,
        0x59,
        0x47,
        0xF0,
        0xAD,
        0xD4,
        0xA2,
        0xAF,
        0x9C,
        0xA4,
        0x72,
        0xC0,
        0xB7,
        0xFD,
        0x93,
        0x26,
        0x36,
        0x3F,
        0xF7,
        0xCC,
        0x34,
        0xA5,
        0xE5,
        0xF1,
        0x71,
        0xD8,
        0x31,
        0x15,
        0x04,
        0xC7,
        0x23,
        0xC3,
        0x18,
        0x96,
        0x05,
        0x9A,
        0x07,
        0x12,
        0x80,
        0xE2,
        0xEB,
        0x27,
        0xB2,
        0x75,
        0x09,
        0x83,
        0x2C,
        0x1A,
        0x1B,
        0x6E,
        0x5A,
        0xA0,
        0x52,
        0x3B,
        0xD6,
        0xB3,
        0x29,
        0xE3,
        0x2F,
        0x84,
        0x53,
        0xD1,
        0x00,
        0xED,
        0x20,
        0xFC,
        0xB1,
        0x5B,
        0x6A,
        0xCB,
        0xBE,
        0x39,
        0x4A,
        0x4C,
        0x58,
        0xCF,
        0xD0,
        0xEF,
        0xAA,
        0xFB,
        0x43,
        0x4D,
        0x33,
        0x85,
        0x45,
        0xF9,
        0x02,
        0x7F,
        0x50,
        0x3C,
        0x9F,
        0xA8,
        0x51,
        0xA3,
        0x40,
        0x8F,
        0x92,
        0x9D,
        0x38,
        0xF5,
        0xBC,
        0xB6,
        0xDA,
        0x21,
        0x10,
        0xFF,
        0xF3,
        0xD2,
        0xCD,
        0x0C,
        0x13,
        0xEC,
        0x5F,
        0x97,
        0x44,
        0x17,
        0xC4,
        0xA7,
        0x7E,
        0x3D,
        0x64,
        0x5D,
        0x19,
        0x73,
        0x60,
        0x81,
        0x4F,
        0xDC,
        0x22,
        0x2A,
        0x90,
        0x88,
        0x46,
        0xEE,
        0xB8,
        0x14,
        0xDE,
        0x5E,
        0x0B,
        0xDB,
        0xE0,
        0x32,
        0x3A,
        0x0A,
        0x49,
        0x06,
        0x24,
        0x5C,
        0xC2,
        0xD3,
        0xAC,
        0x62,
        0x91,
        0x95,
        0xE4,
        0x79,
        0xE7,
        0xC8,
        0x37,
        0x6D,
        0x8D,
        0xD5,
        0x4E,
        0xA9,
        0x6C,
        0x56,
        0xF4,
        0xEA,
        0x65,
        0x7A,
        0xAE,
        0x08,
        0xBA,
        0x78,
        0x25,
        0x2E,
        0x1C,
        0xA6,
        0xB4,
        0xC6,
        0xE8,
        0xDD,
        0x74,
        0x1F,
        0x4B,
        0xBD,
        0x8B,
        0x8A,
        0x70,
        0x3E,
        0xB5,
        0x66,
        0x48,
        0x03,
        0xF6,
        0x0E,
        0x61,
        0x35,
        0x57,
        0xB9,
        0x86,
        0xC1,
        0x1D,
        0x9E,
        0xE1,
        0xF8,
        0x98,
        0x11,
        0x69,
        0xD9,
        0x8E,
        0x94,
        0x9B,
        0x1E,
        0x87,
        0xE9,
        0xCE,
        0x55,
        0x28,
        0xDF,
        0x8C,
        0xA1,
        0x89,
        0x0D,
        0xBF,
        0xE6,
        0x42,
        0x68,
        0x41,
        0x99,
        0x2D,
        0x0F,
        0xB0,
        0x54,
        0xBB,
        0x16,
    )
)
INV_SBOX = bytes(SBOX.index(index) for index in range(256))
RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
BLOCK_SIZE = 16


def aes128_ecb_encrypt(key: bytes, plaintext: bytes) -> bytes:
    if len(key) != BLOCK_SIZE:
        raise ValueError("AES-128 key must be 16 bytes")
    padded = pkcs7_pad(plaintext)
    round_keys = _expand_key(key)
    return b"".join(
        _encrypt_block(padded[index : index + BLOCK_SIZE], round_keys)
        for index in range(0, len(padded), BLOCK_SIZE)
    )


def aes128_ecb_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    if len(key) != BLOCK_SIZE:
        raise ValueError("AES-128 key must be 16 bytes")
    if not ciphertext or len(ciphertext) % BLOCK_SIZE:
        raise ValueError("AES-128 ciphertext length is invalid")
    round_keys = _expand_key(key)
    padded = b"".join(
        _decrypt_block(ciphertext[index : index + BLOCK_SIZE], round_keys)
        for index in range(0, len(ciphertext), BLOCK_SIZE)
    )
    return pkcs7_unpad(padded)


def pkcs7_pad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    padding = block_size - (len(data) % block_size)
    return data + bytes([padding]) * padding


def pkcs7_unpad(data: bytes, block_size: int = BLOCK_SIZE) -> bytes:
    if not data or len(data) % block_size:
        raise ValueError("PKCS7 payload length is invalid")
    padding = data[-1]
    if padding < 1 or padding > block_size or data[-padding:] != bytes([padding]) * padding:
        raise ValueError("PKCS7 padding is invalid")
    return data[:-padding]


def _expand_key(key: bytes) -> list[bytes]:
    words = [key[index : index + 4] for index in range(0, BLOCK_SIZE, 4)]
    for index in range(4, 44):
        temp = words[index - 1]
        if index % 4 == 0:
            rotated = temp[1:] + temp[:1]
            temp = bytes(SBOX[byte] for byte in rotated)
            temp = bytes((temp[0] ^ RCON[index // 4], temp[1], temp[2], temp[3]))
        words.append(
            bytes(left ^ right for left, right in zip(words[index - 4], temp, strict=True))
        )
    return [b"".join(words[index : index + 4]) for index in range(0, 44, 4)]


def _encrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = _xor(block, round_keys[0])
    for round_key in round_keys[1:-1]:
        state = _mix_columns(_shift_rows(bytes(SBOX[byte] for byte in state)))
        state = _xor(state, round_key)
    state = _shift_rows(bytes(SBOX[byte] for byte in state))
    return _xor(state, round_keys[-1])


def _decrypt_block(block: bytes, round_keys: list[bytes]) -> bytes:
    state = _xor(block, round_keys[-1])
    state = bytes(INV_SBOX[byte] for byte in _inv_shift_rows(state))
    for round_key in reversed(round_keys[1:-1]):
        state = _xor(state, round_key)
        state = _inv_mix_columns(state)
        state = bytes(INV_SBOX[byte] for byte in _inv_shift_rows(state))
    return _xor(state, round_keys[0])


def _shift_rows(state: bytes) -> bytes:
    return bytes(
        (
            state[0],
            state[5],
            state[10],
            state[15],
            state[4],
            state[9],
            state[14],
            state[3],
            state[8],
            state[13],
            state[2],
            state[7],
            state[12],
            state[1],
            state[6],
            state[11],
        )
    )


def _inv_shift_rows(state: bytes) -> bytes:
    return bytes(
        (
            state[0],
            state[13],
            state[10],
            state[7],
            state[4],
            state[1],
            state[14],
            state[11],
            state[8],
            state[5],
            state[2],
            state[15],
            state[12],
            state[9],
            state[6],
            state[3],
        )
    )


def _mix_columns(state: bytes) -> bytes:
    return b"".join(_mix_column(state[index : index + 4]) for index in range(0, BLOCK_SIZE, 4))


def _inv_mix_columns(state: bytes) -> bytes:
    return b"".join(_inv_mix_column(state[index : index + 4]) for index in range(0, BLOCK_SIZE, 4))


def _mix_column(column: bytes) -> bytes:
    a, b, c, d = column
    return bytes(
        (
            _gmul(a, 2) ^ _gmul(b, 3) ^ c ^ d,
            a ^ _gmul(b, 2) ^ _gmul(c, 3) ^ d,
            a ^ b ^ _gmul(c, 2) ^ _gmul(d, 3),
            _gmul(a, 3) ^ b ^ c ^ _gmul(d, 2),
        )
    )


def _inv_mix_column(column: bytes) -> bytes:
    a, b, c, d = column
    return bytes(
        (
            _gmul(a, 14) ^ _gmul(b, 11) ^ _gmul(c, 13) ^ _gmul(d, 9),
            _gmul(a, 9) ^ _gmul(b, 14) ^ _gmul(c, 11) ^ _gmul(d, 13),
            _gmul(a, 13) ^ _gmul(b, 9) ^ _gmul(c, 14) ^ _gmul(d, 11),
            _gmul(a, 11) ^ _gmul(b, 13) ^ _gmul(c, 9) ^ _gmul(d, 14),
        )
    )


def _gmul(value: int, factor: int) -> int:
    result = 0
    for _ in range(8):
        if factor & 1:
            result ^= value
        high = value & 0x80
        value = (value << 1) & 0xFF
        if high:
            value ^= 0x1B
        factor >>= 1
    return result


def _xor(left: bytes, right: bytes) -> bytes:
    return bytes(first ^ second for first, second in zip(left, right, strict=True))
