"""Typed GT7 telemetry packet + zero-copy parser.

All byte offsets below are verified against:
    - zetetos/gt-telemetry  -> internal/kaitai/gran_turismo_telemetry.ksy
    - snipem/gt7dashboard   -> gt7dashboard/gt7communication.py
The kaitai struct in gt-telemetry is the most thorough reference; gt7dashboard
is the most widely deployed implementation. Where they disagree, kaitai wins.

The packet has three documented sizes; we parse whatever is present:

    A (296 bytes)  Standard
    B (316 bytes)  Standard + steering_wheel_angle + translational_envelope
    C (368 bytes)  B + EV/surface + current_laptime + per-wheel steering
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from time import monotonic

# ---- Offsets (kaitai-verified) ----------------------------------------------
# Source for every constant below:
#   zetetos/gt-telemetry, internal/kaitai/gran_turismo_telemetry.ksy
# (Cross-checked against snipem/gt7dashboard/gt7communication.py.)

_OFF_MAGIC = 0x00  # u4 little-endian; 0x47375330 ("0S7G") for GT7
_OFF_POS_X = 0x04  # f4
_OFF_POS_Y = 0x08  # f4
_OFF_POS_Z = 0x0C  # f4
_OFF_VEL_X = 0x10  # f4 (m/s)
_OFF_VEL_Y = 0x14
_OFF_VEL_Z = 0x18
_OFF_ROT_PITCH = 0x1C
_OFF_ROT_YAW = 0x20
_OFF_ROT_ROLL = 0x24
_OFF_HEADING = 0x28
_OFF_ANG_VEL_X = 0x2C  # rad/s
_OFF_ANG_VEL_Y = 0x30  # yaw rate (rad/s)
_OFF_ANG_VEL_Z = 0x34
_OFF_RIDE_HEIGHT = 0x38
_OFF_RPM = 0x3C
# 0x40..0x44 = IV seed (consumed by decrypt; ignored here)
_OFF_FUEL_LEVEL = 0x44
_OFF_FUEL_CAPACITY = 0x48
_OFF_SPEED_MPS = 0x4C  # f4 (m/s)
_OFF_MANIFOLD = 0x50
_OFF_OIL_PRESSURE = 0x54
_OFF_WATER_TEMP = 0x58
_OFF_OIL_TEMP = 0x5C
_OFF_TYRE_TEMP_FL = 0x60  # f4
_OFF_TYRE_TEMP_FR = 0x64
_OFF_TYRE_TEMP_RL = 0x68
_OFF_TYRE_TEMP_RR = 0x6C
_OFF_PACKET_ID = 0x70  # u4 (sequence_id)
_OFF_CURRENT_LAP = 0x74  # s2
_OFF_RACE_LAPS = 0x76
_OFF_BEST_LAPTIME_MS = 0x78  # s4 (-1 if unset)
_OFF_LAST_LAPTIME_MS = 0x7C  # s4
_OFF_TIME_OF_DAY = 0x80  # u4
_OFF_GRID_POS = 0x84
_OFF_RACE_ENTRANTS = 0x86
_OFF_REV_LIGHT_MIN = 0x88
_OFF_REV_LIGHT_MAX = 0x8A
_OFF_CALC_MAX_SPEED = 0x8C
_OFF_FLAGS = 0x8E  # u2 bitfield
_OFF_GEAR_PACKED = 0x90  # u1: low nibble = current gear, high nibble = suggested
_OFF_THROTTLE = 0x91  # u1 (0..255)
_OFF_BRAKE = 0x92  # u1 (0..255)
# 0x93 reserved
_OFF_ROAD_PLANE_X = 0x94
_OFF_ROAD_PLANE_Y = 0x98
_OFF_ROAD_PLANE_Z = 0x9C
_OFF_ROAD_PLANE_DIST = 0xA0  # u4
_OFF_WHEEL_RPS_FL = 0xA4  # f4 (rad/s)
_OFF_WHEEL_RPS_FR = 0xA8
_OFF_WHEEL_RPS_RL = 0xAC
_OFF_WHEEL_RPS_RR = 0xB0
_OFF_TYRE_RADIUS_FL = 0xB4  # f4 (m)
_OFF_TYRE_RADIUS_FR = 0xB8
_OFF_TYRE_RADIUS_RL = 0xBC
_OFF_TYRE_RADIUS_RR = 0xC0

# ---- B-format additions (offset 0x128+) -------------------------------------
# Steering and body forces are only present when the heartbeat byte is 'B' or
# later (packet length >= 316). See addendum_1_format in the kaitai struct.
_OFF_STEER_ANGLE_RAD = 0x128  # f4
_OFF_STEER_RATE_RAD_PER_SEC = 0x12C  # f4
_OFF_BODY_SWAY = 0x130  # f4  lateral, normalized (-1..+1)  -> accel_lat
_OFF_BODY_HEAVE = 0x134  # f4  vertical
_OFF_BODY_SURGE = 0x138  # f4  longitudinal                  -> accel_long

# ---- Constants --------------------------------------------------------------
GT7_MAGIC = 0x47375330  # ASCII "0S7G" little-endian -> "GT7" + version flag
MIN_PACKET_SIZE = 296
B_FORMAT_MIN_SIZE = 316
IV_OFF = 0x40  # 4-byte seed used by Salsa20 nonce derivation (see decrypt.py)


@dataclass(slots=True)
class Packet:
    """Decoded GT7 telemetry frame.

    Field set covers section 5 of ARCHITECTURE.md. Fields that only exist in
    the B/C heartbeat formats are typed ``float | None`` and are ``None`` when
    parsed from a 296-byte (A) packet.
    """

    # Identity / timing
    packet_id: int
    recv_time: float  # monotonic seconds at arrival

    # Kinematics
    speed_mps: float
    yaw_rate: float  # rad/s (angular_velocity_y)
    steer_angle: float | None  # rad (B+ only)
    accel_long: float | None  # body surge, normalized (B+ only)
    accel_lat: float | None  # body sway, normalized (B+ only)

    # Driver inputs
    throttle: int  # 0..255
    brake: int  # 0..255
    gear: int  # 0..15

    # Powertrain
    rpm: float

    # Per-wheel rotational speed (rad/s)
    wheel_speed_fl: float
    wheel_speed_fr: float
    wheel_speed_rl: float
    wheel_speed_rr: float

    # Per-wheel tyre temperatures (°C)
    tyre_temp_fl: float
    tyre_temp_fr: float
    tyre_temp_rl: float
    tyre_temp_rr: float

    # Map position
    pos_x: float
    pos_y: float
    pos_z: float

    # Lap data
    lap_count: int
    lap_time_ms: int  # last_laptime (-1 if not set)
    best_lap_ms: int  # -1 if unset

    # Fuel + engine (VR-alert telemetry)
    fuel_level: float  # litres
    fuel_capacity: float  # litres
    oil_temp: float  # °C
    water_temp: float  # °C
    rev_light_min: int  # RPM at which the shift LED first lights
    rev_light_max: int  # RPM at the hard limiter
    time_of_day_ms: int  # ms since midnight, in-game

    # Misc (preserved so detectors don't need to re-parse)
    flags: int

    # Channels whose offsets were documented above from the start but never
    # extracted. The tyre radii matter most: they turn wheel rotation into an
    # exact slip ratio, which previously had to be solved for by fitting a
    # radius against straight-line data. Defaulted so replaying a CSV recorded
    # before they existed still works.
    tyre_radius_fl: float = 0.0
    tyre_radius_fr: float = 0.0
    tyre_radius_rl: float = 0.0
    tyre_radius_rr: float = 0.0
    ride_height: float = 0.0
    roll_rate: float = 0.0  # angular velocity about the longitudinal axis
    pitch_rate: float = 0.0  # ...and about the lateral axis
    steer_rate: float = 0.0  # B-format only; 0.0 on an A-format packet

    @property
    def speed_kmh(self) -> float:
        return self.speed_mps * 3.6

    # ---- slip ratios ------------------------------------------------------
    # >1 means the wheel is outrunning the car (spinning under power); <1 means
    # it is turning slower than the car is moving (locking under braking).
    # Both return None when the radius is unknown (old capture) or the car is
    # too slow for the ratio to mean anything.

    def _axle_slip(self, rps_l: float, rps_r: float, rad_l: float, rad_r: float) -> float | None:
        if self.speed_mps < 3.0 or rad_l <= 0.0 or rad_r <= 0.0:
            return None
        surface = (abs(rps_l) * rad_l + abs(rps_r) * rad_r) / 2.0
        return surface / self.speed_mps

    @property
    def front_slip(self) -> float | None:
        return self._axle_slip(
            self.wheel_speed_fl, self.wheel_speed_fr, self.tyre_radius_fl, self.tyre_radius_fr
        )

    @property
    def rear_slip(self) -> float | None:
        return self._axle_slip(
            self.wheel_speed_rl, self.wheel_speed_rr, self.tyre_radius_rl, self.tyre_radius_rr
        )

    @property
    def combined_g(self) -> float:
        """Total grip the tyres are delivering — the traction-circle radius.

        The reference a beginner actually needs: compared against the car's own
        demonstrated peak it answers "how much of the available grip did I use
        through there", which needs no reference lap and stays valid on a
        compromised line in traffic.
        """
        lat = (self.accel_lat or 0.0) / 9.80665
        lon = (self.accel_long or 0.0) / 9.80665
        return (lat * lat + lon * lon) ** 0.5


# Pre-compiled struct for the contiguous A-format float block we care about.
# This is just a perf nicety; correctness is per-offset below.
_F32 = struct.Struct("<f")
_U32 = struct.Struct("<I")
_S16 = struct.Struct("<h")
_S32 = struct.Struct("<i")
_U16 = struct.Struct("<H")


def _f32(buf: bytes, off: int) -> float:
    return _F32.unpack_from(buf, off)[0]


def parse_packet(buf: bytes, recv_time: float | None = None) -> Packet:
    """Parse decrypted packet bytes into a :class:`Packet`.

    Args:
        buf: decrypted packet, at least 296 bytes long.
        recv_time: monotonic timestamp at receipt. Defaults to ``monotonic()``.
    """
    if len(buf) < MIN_PACKET_SIZE:
        raise ValueError(f"packet too short ({len(buf)} bytes; need >= {MIN_PACKET_SIZE})")

    magic = _U32.unpack_from(buf, _OFF_MAGIC)[0]
    if magic != GT7_MAGIC:
        raise ValueError(f"bad magic 0x{magic:08X}; expected 0x{GT7_MAGIC:08X}")

    has_b = len(buf) >= B_FORMAT_MIN_SIZE

    return Packet(
        packet_id=_U32.unpack_from(buf, _OFF_PACKET_ID)[0],
        recv_time=monotonic() if recv_time is None else recv_time,
        speed_mps=_f32(buf, _OFF_SPEED_MPS),
        yaw_rate=_f32(buf, _OFF_ANG_VEL_Y),
        steer_angle=_f32(buf, _OFF_STEER_ANGLE_RAD) if has_b else None,
        accel_long=_f32(buf, _OFF_BODY_SURGE) if has_b else None,
        accel_lat=_f32(buf, _OFF_BODY_SWAY) if has_b else None,
        throttle=buf[_OFF_THROTTLE],
        brake=buf[_OFF_BRAKE],
        gear=buf[_OFF_GEAR_PACKED] & 0x0F,
        rpm=_f32(buf, _OFF_RPM),
        wheel_speed_fl=_f32(buf, _OFF_WHEEL_RPS_FL),
        wheel_speed_fr=_f32(buf, _OFF_WHEEL_RPS_FR),
        wheel_speed_rl=_f32(buf, _OFF_WHEEL_RPS_RL),
        wheel_speed_rr=_f32(buf, _OFF_WHEEL_RPS_RR),
        tyre_temp_fl=_f32(buf, _OFF_TYRE_TEMP_FL),
        tyre_temp_fr=_f32(buf, _OFF_TYRE_TEMP_FR),
        tyre_temp_rl=_f32(buf, _OFF_TYRE_TEMP_RL),
        tyre_temp_rr=_f32(buf, _OFF_TYRE_TEMP_RR),
        pos_x=_f32(buf, _OFF_POS_X),
        pos_y=_f32(buf, _OFF_POS_Y),
        pos_z=_f32(buf, _OFF_POS_Z),
        lap_count=_S16.unpack_from(buf, _OFF_CURRENT_LAP)[0],
        lap_time_ms=_S32.unpack_from(buf, _OFF_LAST_LAPTIME_MS)[0],
        best_lap_ms=_S32.unpack_from(buf, _OFF_BEST_LAPTIME_MS)[0],
        fuel_level=_f32(buf, _OFF_FUEL_LEVEL),
        fuel_capacity=_f32(buf, _OFF_FUEL_CAPACITY),
        oil_temp=_f32(buf, _OFF_OIL_TEMP),
        water_temp=_f32(buf, _OFF_WATER_TEMP),
        rev_light_min=_U16.unpack_from(buf, _OFF_REV_LIGHT_MIN)[0],
        rev_light_max=_U16.unpack_from(buf, _OFF_REV_LIGHT_MAX)[0],
        time_of_day_ms=_U32.unpack_from(buf, _OFF_TIME_OF_DAY)[0],
        flags=_U16.unpack_from(buf, _OFF_FLAGS)[0],
        tyre_radius_fl=_f32(buf, _OFF_TYRE_RADIUS_FL),
        tyre_radius_fr=_f32(buf, _OFF_TYRE_RADIUS_FR),
        tyre_radius_rl=_f32(buf, _OFF_TYRE_RADIUS_RL),
        tyre_radius_rr=_f32(buf, _OFF_TYRE_RADIUS_RR),
        ride_height=_f32(buf, _OFF_RIDE_HEIGHT),
        roll_rate=_f32(buf, _OFF_ANG_VEL_X),
        pitch_rate=_f32(buf, _OFF_ANG_VEL_Z),
        steer_rate=(
            _f32(buf, _OFF_STEER_RATE_RAD_PER_SEC) if len(buf) >= B_FORMAT_MIN_SIZE else 0.0
        ),
    )


def build_synthetic_packet(
    *,
    packet_id: int = 1,
    speed_mps: float = 50.0,
    yaw_rate: float = 0.1,
    steer_angle: float = 0.05,
    accel_long: float = -0.3,
    accel_lat: float = 0.4,
    throttle: int = 200,
    brake: int = 0,
    gear: int = 3,
    rpm: float = 6500.0,
    wheel_rps: tuple[float, float, float, float] = (75.0, 75.0, 76.0, 76.0),
    tyre_temp: tuple[float, float, float, float] = (85.0, 85.0, 80.0, 80.0),
    pos: tuple[float, float, float] = (10.0, 0.5, -20.0),
    lap_count: int = 1,
    lap_time_ms: int = 92500,
    best_lap_ms: int = -1,
    fuel_level: float = 50.0,
    fuel_capacity: float = 100.0,
    oil_temp: float = 95.0,
    water_temp: float = 85.0,
    rev_light_min: int = 7500,
    rev_light_max: int = 8500,
    time_of_day_ms: int = 12 * 3600 * 1000,
    flags: int = 0,
    fmt: str = "B",
    iv_seed_bytes: bytes = b"\x00\x00\x00\x00",
) -> bytes:
    """Build a plaintext packet that round-trips through :func:`parse_packet`.

    This is only used by tests and fixtures; production receivers never call
    this. The returned bytes are NOT Salsa20-encrypted — call
    :func:`gt7coach.telemetry.decrypt.encrypt_packet` on them if you want a
    realistic on-wire payload.
    """
    size = 316 if fmt == "B" else 296
    buf = bytearray(size)
    _U32.pack_into(buf, _OFF_MAGIC, GT7_MAGIC)
    _F32.pack_into(buf, _OFF_POS_X, pos[0])
    _F32.pack_into(buf, _OFF_POS_Y, pos[1])
    _F32.pack_into(buf, _OFF_POS_Z, pos[2])
    _F32.pack_into(buf, _OFF_ANG_VEL_Y, yaw_rate)
    _F32.pack_into(buf, _OFF_RPM, rpm)
    buf[IV_OFF : IV_OFF + 4] = iv_seed_bytes
    _F32.pack_into(buf, _OFF_SPEED_MPS, speed_mps)
    _F32.pack_into(buf, _OFF_TYRE_TEMP_FL, tyre_temp[0])
    _F32.pack_into(buf, _OFF_TYRE_TEMP_FR, tyre_temp[1])
    _F32.pack_into(buf, _OFF_TYRE_TEMP_RL, tyre_temp[2])
    _F32.pack_into(buf, _OFF_TYRE_TEMP_RR, tyre_temp[3])
    _U32.pack_into(buf, _OFF_PACKET_ID, packet_id)
    _S16.pack_into(buf, _OFF_CURRENT_LAP, lap_count)
    _S32.pack_into(buf, _OFF_LAST_LAPTIME_MS, lap_time_ms)
    _S32.pack_into(buf, _OFF_BEST_LAPTIME_MS, best_lap_ms)
    _F32.pack_into(buf, _OFF_FUEL_LEVEL, fuel_level)
    _F32.pack_into(buf, _OFF_FUEL_CAPACITY, fuel_capacity)
    _F32.pack_into(buf, _OFF_OIL_TEMP, oil_temp)
    _F32.pack_into(buf, _OFF_WATER_TEMP, water_temp)
    _U16.pack_into(buf, _OFF_REV_LIGHT_MIN, rev_light_min)
    _U16.pack_into(buf, _OFF_REV_LIGHT_MAX, rev_light_max)
    _U32.pack_into(buf, _OFF_TIME_OF_DAY, time_of_day_ms)
    _U16.pack_into(buf, _OFF_FLAGS, flags)
    buf[_OFF_GEAR_PACKED] = gear & 0x0F
    buf[_OFF_THROTTLE] = throttle
    buf[_OFF_BRAKE] = brake
    _F32.pack_into(buf, _OFF_WHEEL_RPS_FL, wheel_rps[0])
    _F32.pack_into(buf, _OFF_WHEEL_RPS_FR, wheel_rps[1])
    _F32.pack_into(buf, _OFF_WHEEL_RPS_RL, wheel_rps[2])
    _F32.pack_into(buf, _OFF_WHEEL_RPS_RR, wheel_rps[3])
    if fmt == "B":
        _F32.pack_into(buf, _OFF_STEER_ANGLE_RAD, steer_angle)
        _F32.pack_into(buf, _OFF_BODY_SWAY, accel_lat)
        _F32.pack_into(buf, _OFF_BODY_SURGE, accel_long)
    return bytes(buf)
