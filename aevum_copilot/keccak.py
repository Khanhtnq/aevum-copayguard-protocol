"""Pure-Python Ethereum Keccak-256 (pre-FIPS-202 0x01 padding), selectors, and EIP-55 checksums."""

from __future__ import annotations

import re

_MASK_64 = (1 << 64) - 1

_ROTATIONS = (
    (0, 36, 3, 41, 18),
    (1, 44, 10, 45, 2),
    (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56),
    (27, 20, 39, 8, 14),
)

_ROUND_CONSTANTS = (
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
)

_HEX_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _rotl64(val: int, shift: int) -> int:
    if shift == 0:
        return val
    return ((val << shift) | (val >> (64 - shift))) & _MASK_64


def _keccak_f1600(lanes: list[list[int]]) -> None:
    for rc in _ROUND_CONSTANTS:
        c = [lanes[x][0] ^ lanes[x][1] ^ lanes[x][2] ^ lanes[x][3] ^ lanes[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl64(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                lanes[x][y] ^= d[x]

        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl64(lanes[x][y], _ROTATIONS[x][y])

        for x in range(5):
            for y in range(5):
                lanes[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])

        lanes[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    """Compute Ethereum 256-bit Keccak digest (rate=136 bytes, pad=0x01..0x80)."""
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] |= 0x80

    lanes = [[0] * 5 for _ in range(5)]
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for idx in range(rate // 8):
            word = int.from_bytes(block[idx * 8 : (idx + 1) * 8], "little")
            lanes[idx % 5][idx // 5] ^= word
        _keccak_f1600(lanes)

    out = bytearray()
    for y in range(5):
        for x in range(5):
            out.extend(lanes[x][y].to_bytes(8, "little"))
            if len(out) >= 32:
                return bytes(out[:32])
    return bytes(out[:32])


def keccak256_text(text: str) -> str:
    return "0x" + keccak256(text.encode("utf-8")).hex()


def function_selector(signature: str) -> str:
    return "0x" + keccak256(signature.encode("ascii"))[:4].hex()


def is_valid_evm_address(address: str) -> bool:
    return bool(_HEX_ADDRESS_RE.fullmatch(address.strip()))


def to_checksum_address(address: str) -> str:
    cleaned = address.strip()
    if not is_valid_evm_address(cleaned):
        raise ValueError(f"Invalid EVM address: {address!r}")
    lower_hex = cleaned[2:].lower()
    digest_hex = keccak256(lower_hex.encode("ascii")).hex()
    chars = [
        ch.upper() if int(digest_hex[idx], 16) >= 8 else ch for idx, ch in enumerate(lower_hex)
    ]
    return "0x" + "".join(chars)


def normalize_payee_handle(handle: str) -> str:
    normalized = handle.strip().lower().lstrip("@")
    if not normalized:
        raise ValueError("Payee handle must not be empty")
    return normalized
