"""Helpers for synthesising Packet streams for detector tests."""

from __future__ import annotations

from collections.abc import Iterator

from gt7coach.telemetry.packet import Packet


def make_packet(
    *,
    packet_id: int = 1,
    recv_time: float = 0.0,
    speed_kmh: float = 100.0,
    yaw_rate: float = 0.0,
    steer_angle: float | None = 0.0,
    accel_long: float | None = 0.0,
    accel_lat: float | None = 0.0,
    throttle: int = 0,
    brake: int = 0,
    gear: int = 4,
    rpm: float = 5000.0,
    wheel_rps: tuple[float, float, float, float] = (100.0, 100.0, 100.0, 100.0),
    tyre_temp: tuple[float, float, float, float] = (80.0, 80.0, 80.0, 80.0),
    pos: tuple[float, float, float] = (0.0, 0.0, 0.0),
    lap_count: int = 1,
    lap_time_ms: int = -1,
    best_lap_ms: int = -1,
    fuel_level: float = 50.0,
    fuel_capacity: float = 100.0,
    oil_temp: float = 95.0,
    water_temp: float = 85.0,
    rev_light_min: int = 7500,
    rev_light_max: int = 8500,
    time_of_day_ms: int = 12 * 3600 * 1000,
    flags: int = 0,
) -> Packet:
    """Construct a fully-populated Packet with sensible defaults.

    Wheel RPS values match the legacy convention (positive magnitudes); the
    detectors all take ``abs()`` so signed vs unsigned doesn't matter.
    """
    return Packet(
        packet_id=packet_id,
        recv_time=recv_time,
        speed_mps=speed_kmh / 3.6,
        yaw_rate=yaw_rate,
        steer_angle=steer_angle,
        accel_long=accel_long,
        accel_lat=accel_lat,
        throttle=throttle,
        brake=brake,
        gear=gear,
        rpm=rpm,
        wheel_speed_fl=wheel_rps[0],
        wheel_speed_fr=wheel_rps[1],
        wheel_speed_rl=wheel_rps[2],
        wheel_speed_rr=wheel_rps[3],
        tyre_temp_fl=tyre_temp[0],
        tyre_temp_fr=tyre_temp[1],
        tyre_temp_rl=tyre_temp[2],
        tyre_temp_rr=tyre_temp[3],
        pos_x=pos[0],
        pos_y=pos[1],
        pos_z=pos[2],
        lap_count=lap_count,
        lap_time_ms=lap_time_ms,
        best_lap_ms=best_lap_ms,
        fuel_level=fuel_level,
        fuel_capacity=fuel_capacity,
        oil_temp=oil_temp,
        water_temp=water_temp,
        rev_light_min=rev_light_min,
        rev_light_max=rev_light_max,
        time_of_day_ms=time_of_day_ms,
        flags=flags,
    )


def build_bad_corner_trace() -> list[Packet]:
    """Synthesise a multi-phase trace that triggers all three Phase-2 detectors.

    Layout (50 Hz, 20 ms per frame):
        Frames   0-19   straight at 200 km/h, full throttle, lat_g 0
        Frames  20-29   turn-in builds (steer 0.20 to 0.50), brake 0,
                         lat_g 0.05 g to 0.50 g -- below entry thresholds
        Frames  30-49   LATE BRAKE: brake 100 to 214, lat_g 0.5 to 1.45 g
                         -- corner ENTERS here (brake > 65 at frame 30)
                         -- peak brake lands 0.38 s after corner start
        Frames  50-79   mid-corner, lat_g 1.4 g, FRONT wheels at 1.20x rears
                         -> understeer
        Frames  80-109  corner exit, throttle 200+, lat_g still 0.6-1.0 g,
                         REAR wheels at 1.20x fronts -> wheelspin
        Frames 110-169  straight, lat_g 0 (>=0.5 s dwell to finalise corner)
    """
    G = 9.80665
    packets: list[Packet] = []
    pid = 1000
    rt = 1000.0
    dt = 0.020  # 50 Hz

    def push(**kwargs) -> None:
        nonlocal pid, rt
        packets.append(make_packet(packet_id=pid, recv_time=rt, **kwargs))
        pid += 1
        rt += dt

    # 0-19: straight, full throttle
    for _ in range(20):
        push(speed_kmh=200, throttle=255, accel_long=2.0, accel_lat=0.0)

    # 20-29: turn-in builds (steering rising, no brake, sub-threshold lat_g)
    for i in range(10):
        steer = 0.20 + 0.033 * i  # 0.20 .. 0.50
        lat = 0.05 * (i + 1) * G  # 0.05 g .. 0.50 g  (still under 0.95 entry)
        push(speed_kmh=200 - 2 * i, throttle=255, steer_angle=steer, accel_lat=lat)

    # 30-49: LATE BRAKE -- brake comes on hard while already steering
    for i in range(20):
        brake = 100 + 6 * i  # 100..214
        lat = (0.5 + 0.05 * i) * G  # 0.5 g .. 1.45 g
        speed = 180 - 3 * i
        push(speed_kmh=speed, throttle=0, brake=brake, steer_angle=0.50, accel_lat=lat)

    # 50-79: MID-CORNER, understeer -- FRONT wheels 1.20x rears, high lat_g
    for i in range(30):
        speed = 120 - 0.5 * i
        rear_rps = (speed / 3.6) / 0.33  # would be ground speed without slip
        front_rps = rear_rps * 1.20  # 20% over -> clear understeer
        push(
            speed_kmh=speed,
            throttle=20,
            brake=0,
            steer_angle=0.55,
            accel_lat=1.4 * G,
            wheel_rps=(front_rps, front_rps, rear_rps, rear_rps),
        )

    # 80-109: CORNER EXIT, wheelspin -- REAR wheels 1.20x fronts, throttle up
    for i in range(30):
        speed = 100 + 1.5 * i
        front_rps = (speed / 3.6) / 0.33
        rear_rps = front_rps * 1.20  # 20% wheelspin
        lat = max(0.6, 1.0 - 0.02 * i) * G
        push(
            speed_kmh=speed,
            throttle=200 + i,
            brake=0,
            steer_angle=0.35 - 0.01 * i,
            accel_lat=lat,
            wheel_rps=(front_rps, front_rps, rear_rps, rear_rps),
        )

    # 110-169: STRAIGHT, lat_g 0 -- satisfies the exit dwell
    for _ in range(60):
        push(speed_kmh=140, throttle=255, steer_angle=0.0, accel_lat=0.0)

    return packets


def stream(packets: list[Packet]) -> Iterator[Packet]:
    """Yield packets one at a time (just a readability helper)."""
    yield from packets
