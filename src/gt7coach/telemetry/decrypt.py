"""Salsa20 decryption for GT7 UDP telemetry packets.

The Polyphony Digital "Simulator Interface" protocol encrypts each UDP packet
with Salsa20. The key is a fixed 32-byte string; the 8-byte nonce is derived
from a 4-byte seed inside the encrypted payload itself (at offset 0x40) XORed
with a magic constant that depends on the requested format.

References (verified before commit):
    - snipem/gt7dashboard  -> gt7dashboard/gt7communication.py (key + IV scheme)
    - zetetos/gt-telemetry -> internal/reader/udpreader.go    (per-format IV seed)
"""

from __future__ import annotations

from typing import Final

from Crypto.Cipher import Salsa20

# 32-byte key. Documented value is the ASCII string truncated to 32 bytes.
# Source: snipem/gt7dashboard, gt7dashboard/gt7communication.py
SALSA20_KEY: Final[bytes] = b"Simulator Interface Packet GT7 ver 0.0"[:32]

# Heartbeat byte -> XOR seed used to derive the Salsa20 nonce.
# Source: zetetos/gt-telemetry, internal/reader/udpreader.go (getIVSeedForFormat)
FORMAT_IV_SEEDS: Final[dict[str, int]] = {
    "A": 0xDEADBEAF,  # Standard   (296-byte packets)
    "B": 0xDEADBEEF,  # Addendum1  (316-byte packets, adds steering + envelope)
    "~": 0x55FABB4F,  # Addendum2  (344-byte packets, EV/torque vectoring)
    "C": 0xDEADBEEF,  # Addendum3  (368-byte packets, current_laptime etc.)
}

# Minimum on-wire packet size per format.
# Source: zetetos/gt-telemetry kaitai struct (internal/kaitai/gran_turismo_telemetry.ksy)
FORMAT_PACKET_SIZES: Final[dict[str, int]] = {
    "A": 296,
    "B": 316,
    "~": 344,
    "C": 368,
}

# Offset of the 4-byte IV seed inside the encrypted payload.
IV_SEED_OFFSET: Final[int] = 0x40


def _build_nonce(payload: bytes, xor_seed: int) -> bytes:
    """Build the 8-byte Salsa20 nonce from the on-wire seed bytes."""
    if len(payload) < IV_SEED_OFFSET + 4:
        raise ValueError(f"packet too short to contain IV seed ({len(payload)} bytes)")
    oiv = payload[IV_SEED_OFFSET : IV_SEED_OFFSET + 4]
    iv1 = int.from_bytes(oiv, "little")
    iv2 = iv1 ^ xor_seed
    return iv2.to_bytes(4, "little") + iv1.to_bytes(4, "little")


def decrypt_packet(payload: bytes, fmt: str = "A") -> bytes:
    """Decrypt a single GT7 telemetry packet.

    Args:
        payload: raw bytes received over UDP.
        fmt: the heartbeat format used to request the packet
            (``"A"``, ``"B"``, ``"~"``, or ``"C"``). Determines the IV seed.

    Returns:
        The decrypted packet bytes (same length as input).

    Note:
        The 4 bytes at :data:`IV_SEED_OFFSET` are the cipher's own nonce seed
        and are transmitted in plain text. Salsa20 is applied across the whole
        packet for simplicity; we then restore those 4 bytes from the input so
        the output is a clean round-trip with :func:`encrypt_packet`.
    """
    if fmt not in FORMAT_IV_SEEDS:
        raise ValueError(f"unknown packet format {fmt!r}; expected one of {list(FORMAT_IV_SEEDS)}")
    nonce = _build_nonce(payload, FORMAT_IV_SEEDS[fmt])
    cipher = Salsa20.new(key=SALSA20_KEY, nonce=nonce)
    out = bytearray(cipher.decrypt(payload))
    out[IV_SEED_OFFSET : IV_SEED_OFFSET + 4] = payload[IV_SEED_OFFSET : IV_SEED_OFFSET + 4]
    return bytes(out)


def encrypt_packet(payload: bytes, fmt: str = "A") -> bytes:
    """Encrypt a packet (for tests / synthetic fixtures).

    Salsa20 is a stream cipher, so the encrypt and decrypt operations are
    identical — the keystream XOR is its own inverse. The 4 seed bytes at
    :data:`IV_SEED_OFFSET` must already be present in the input and are kept
    as-is on output, so the result round-trips through :func:`decrypt_packet`.
    """
    return decrypt_packet(payload, fmt=fmt)
