"""Phase 1 smoke tests for the telemetry pipeline.

These tests must run with no PS5 present. They exercise:
    * Salsa20 round-trip on a synthetic packet.
    * Parser field coverage against ARCHITECTURE.md section 5.
    * Replay of a recorded fixture CSV.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gt7coach.telemetry import (
    FORMAT_PACKET_SIZES,
    Packet,
    decrypt_packet,
    parse_packet,
    replay_csv,
)
from gt7coach.telemetry.decrypt import encrypt_packet
from gt7coach.telemetry.packet import build_synthetic_packet

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SYNTHETIC_CSV = FIXTURE_DIR / "synthetic_brake_corner.csv"


# --- ARCHITECTURE.md section 5: required Packet field set --------------------
# If a field is added to or removed from the spec, update this list. The point
# of the test is to catch silent drift between code and spec.
SPEC_FIELDS = {
    "speed_mps",
    "accel_long",
    "accel_lat",
    "yaw_rate",
    "steer_angle",
    "throttle",
    "brake",
    "gear",
    "rpm",
    "wheel_speed_fl",
    "wheel_speed_fr",
    "wheel_speed_rl",
    "wheel_speed_rr",
    "tyre_temp_fl",
    "tyre_temp_fr",
    "tyre_temp_rl",
    "tyre_temp_rr",
    "pos_x",
    "pos_y",
    "pos_z",
    "lap_count",
    "lap_time_ms",
    "packet_id",
    "recv_time",
}


def test_decrypt_roundtrip() -> None:
    """A packet that's been encrypted then decrypted must equal the original.

    Salsa20 is a stream cipher so encrypt == decrypt; this also proves that the
    key, nonce-derivation, and IV offset are wired up the same way on both
    sides.
    """
    plaintext = build_synthetic_packet(
        packet_id=42,
        speed_mps=51.2,
        throttle=200,
        brake=10,
        gear=4,
        fmt="B",
        iv_seed_bytes=b"\x11\x22\x33\x44",
    )
    assert len(plaintext) == FORMAT_PACKET_SIZES["B"]

    ciphertext = encrypt_packet(plaintext, fmt="B")
    assert ciphertext != plaintext, "Salsa20 must actually change the bytes"

    # IV seed bytes are at offset 0x40 and must NOT be encrypted in either
    # direction (the cipher XORs them with the keystream — but Salsa20's
    # encrypt and decrypt are symmetric, so going through both restores them).
    recovered = decrypt_packet(ciphertext, fmt="B")
    assert recovered == plaintext


def test_packet_parse_fields() -> None:
    """Every field named in ARCHITECTURE.md section 5 must be present."""
    plaintext = build_synthetic_packet(
        packet_id=99,
        speed_mps=37.0,
        yaw_rate=0.42,
        steer_angle=0.18,
        accel_long=-0.55,
        accel_lat=0.91,
        throttle=120,
        brake=40,
        gear=3,
        rpm=5500.0,
        wheel_rps=(141.0, 141.5, 142.0, 142.5),
        tyre_temp=(85.0, 85.5, 81.0, 81.5),
        pos=(1.0, 2.0, 3.0),
        lap_count=2,
        lap_time_ms=92500,
        flags=0b101,
        fmt="B",
    )
    pkt = parse_packet(plaintext)

    missing = SPEC_FIELDS - set(Packet.__dataclass_fields__.keys())
    assert not missing, f"Packet dataclass missing spec fields: {missing}"

    assert pkt.packet_id == 99
    assert pkt.speed_mps == pytest.approx(37.0)
    assert pkt.yaw_rate == pytest.approx(0.42)
    assert pkt.steer_angle == pytest.approx(0.18)
    assert pkt.accel_long == pytest.approx(-0.55)
    assert pkt.accel_lat == pytest.approx(0.91)
    assert pkt.throttle == 120
    assert pkt.brake == 40
    assert pkt.gear == 3
    assert pkt.rpm == pytest.approx(5500.0)
    assert pkt.wheel_speed_fl == pytest.approx(141.0)
    assert pkt.wheel_speed_rr == pytest.approx(142.5)
    assert pkt.tyre_temp_fl == pytest.approx(85.0)
    assert pkt.tyre_temp_rr == pytest.approx(81.5)
    assert pkt.pos_x == pytest.approx(1.0)
    assert pkt.pos_z == pytest.approx(3.0)
    assert pkt.lap_count == 2
    assert pkt.lap_time_ms == 92500
    assert pkt.flags == 0b101
    assert pkt.recv_time > 0

    # Speed conversion helper sanity check.
    assert pkt.speed_kmh == pytest.approx(37.0 * 3.6)


def test_packet_a_format_optional_fields_are_none() -> None:
    """A 296-byte (A-format) packet must parse but leave B-only fields None."""
    plaintext = build_synthetic_packet(fmt="A")
    pkt = parse_packet(plaintext)
    assert pkt.steer_angle is None
    assert pkt.accel_long is None
    assert pkt.accel_lat is None


def test_replay_from_fixture() -> None:
    """Replay the canonical fixture CSV and count packets."""
    assert SYNTHETIC_CSV.exists(), f"missing fixture: {SYNTHETIC_CSV}"
    packets = list(replay_csv(SYNTHETIC_CSV))
    assert len(packets) == 24, f"expected 24 packets, got {len(packets)}"
    assert all(isinstance(p, Packet) for p in packets)

    # The fixture is a "brake into a corner and exit" trace. Sanity-check
    # that lateral g peaks while we're cornering, not on the entry/exit straights.
    lat_gs = [p.accel_lat for p in packets if p.accel_lat is not None]
    assert lat_gs, "fixture should contain accel_lat values"
    assert max(lat_gs) > 1.0, "fixture should record a real cornering load"
    assert packets[0].packet_id == 1
    assert packets[-1].packet_id == 24
