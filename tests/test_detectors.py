"""Phase-2 detector tests.

Each detector is tested against synthetic traces. The corner segmenter is
also tested for hysteresis + min-dwell behaviour (legacy bug §11.7 must not
regress).
"""

from __future__ import annotations

import pytest

from gt7coach.detectors import (
    CornerSegmenter,
    CornerSegmenterConfig,
    CornerTrace,
    LateBrakeConfig,
    UndersteerConfig,
    WheelspinConfig,
    detect_late_brake,
    detect_lockup,
    detect_no_trail,
    detect_understeer,
    detect_wheelspin,
)
from tests._synth import build_bad_corner_trace, make_packet

G = 9.80665


# ---------- CornerSegmenter --------------------------------------------------


def _feed_all(seg: CornerSegmenter, packets):
    out: list[CornerTrace] = []
    for p in packets:
        c = seg.feed(p)
        if c is not None:
            out.append(c)
    last = seg.flush()
    if last is not None:
        out.append(last)
    return out


def test_segmenter_emits_no_corner_on_straight() -> None:
    seg = CornerSegmenter()
    packets = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=200, throttle=255)
        for i in range(100)
    ]
    assert _feed_all(seg, packets) == []


def test_segmenter_finds_one_corner_with_lateral_g() -> None:
    seg = CornerSegmenter()
    packets = (
        # 1s straight
        [
            make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=120, throttle=255)
            for i in range(50)
        ]
        # 1.5s cornering at 1.2 g lateral
        + [
            make_packet(
                packet_id=50 + i,
                recv_time=1.0 + i * 0.02,
                speed_kmh=100,
                throttle=120,
                accel_lat=1.2 * G,
            )
            for i in range(75)
        ]
        # 0.8s straight again — long enough to satisfy the exit dwell
        + [
            make_packet(
                packet_id=125 + i,
                recv_time=2.5 + i * 0.02,
                speed_kmh=120,
                throttle=255,
            )
            for i in range(40)
        ]
    )
    corners = _feed_all(seg, packets)
    assert len(corners) == 1
    assert corners[0].peak_lat_g >= 1.1
    assert corners[0].duration_s >= 1.0


def test_segmenter_hysteresis_does_not_bounce() -> None:
    """A signal that dips below threshold for < min_dwell_s must not split the corner."""
    seg = CornerSegmenter()
    pkts = []
    t = 0.0
    # Solid 2 s of cornering, but the signal briefly drops at t=0.8 s and t=1.5 s.
    for i in range(100):
        t = i * 0.02
        lat = 1.2 * G
        brake = 0
        if 40 <= i <= 43:  # ~0.08 s blip
            lat = 0.3 * G
        if 75 <= i <= 78:
            lat = 0.4 * G
        pkts.append(
            make_packet(
                packet_id=i, recv_time=t, speed_kmh=110, throttle=120, accel_lat=lat, brake=brake
            )
        )
    # Long-enough exit straight
    pkts += [
        make_packet(packet_id=100 + i, recv_time=t + 0.02 + i * 0.02, speed_kmh=130, throttle=255)
        for i in range(60)
    ]

    corners = _feed_all(seg, pkts)
    assert len(corners) == 1, f"expected 1 corner, got {len(corners)}"


def test_segmenter_drops_short_blips() -> None:
    """A 0.2 s cornering event under the min_corner_duration_s floor is discarded."""
    seg = CornerSegmenter(CornerSegmenterConfig(min_corner_duration_s=0.7))
    pkts = (
        [
            make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=120, throttle=255)
            for i in range(30)
        ]
        + [
            make_packet(
                packet_id=30 + i,
                recv_time=0.6 + i * 0.02,
                speed_kmh=100,
                accel_lat=1.2 * G,
            )
            for i in range(10)  # 0.2 s
        ]
        + [
            make_packet(packet_id=40 + i, recv_time=0.8 + i * 0.02, speed_kmh=130, throttle=255)
            for i in range(60)
        ]
    )
    assert _feed_all(seg, pkts) == []


# ---------- late_brake -------------------------------------------------------


def test_late_brake_fires_when_brake_peaks_after_turn_in() -> None:
    # 1 s trace: steering begins at t=0.10 s, brake peaks at t=0.60 s -> offset 0.5 s
    pkts = []
    for i in range(50):
        t = i * 0.02
        steer = 0.5 if t >= 0.10 else 0.0
        brake = 0
        if 0.20 <= t <= 0.60:
            brake = int(100 + (t - 0.20) * 250)  # ramps to ~200 by t=0.60
        elif t > 0.60:
            brake = max(0, int(200 - (t - 0.60) * 400))
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=t,
                speed_kmh=120,
                throttle=0,
                steer_angle=steer,
                brake=brake,
                accel_lat=1.0 * G,
            )
        )
    trace = CornerTrace(packets=pkts)
    events = detect_late_brake(trace)
    assert len(events) == 1
    e = events[0]
    assert e.type == "braking.late_brake"
    assert 0.0 < e.severity <= 1.0
    assert e.evidence["offset_after_turn_in_s"] >= 0.30


def test_late_brake_quiet_when_brake_peaks_before_steering() -> None:
    pkts = []
    for i in range(50):
        t = i * 0.02
        brake = 200 if t < 0.30 else 0
        steer = 0.5 if t >= 0.40 else 0.0
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=t,
                speed_kmh=120,
                throttle=0,
                steer_angle=steer,
                brake=brake,
                accel_lat=1.0 * G,
            )
        )
    assert detect_late_brake(CornerTrace(packets=pkts)) == []


def test_late_brake_quiet_when_no_meaningful_brake() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, brake=10, steer_angle=0.4, accel_lat=1.0 * G)
        for i in range(50)
    ]
    assert detect_late_brake(CornerTrace(packets=pkts)) == []


# ---------- wheelspin --------------------------------------------------------


def test_wheelspin_fires_when_rear_axle_outpaces_front() -> None:
    pkts = []
    for i in range(30):
        t = i * 0.02
        # car at 90 km/h => 25 m/s ground speed; front rolls true, rear spinning 20% faster
        front = 25.0 / 0.33
        rear = front * 1.20
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=t,
                speed_kmh=90,
                throttle=240,
                wheel_rps=(front, front, rear, rear),
            )
        )
    events = detect_wheelspin(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "throttle.wheelspin"
    assert events[0].evidence["peak_ratio"] >= 1.15
    assert events[0].severity > 0


def test_wheelspin_quiet_at_low_throttle() -> None:
    front = 25.0 / 0.33
    rear = front * 1.20
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            throttle=50,
            wheel_rps=(front, front, rear, rear),
        )
        for i in range(30)
    ]
    assert detect_wheelspin(CornerTrace(packets=pkts)) == []


def test_wheelspin_quiet_with_equal_axle_speeds() -> None:
    rps = 25.0 / 0.33
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, throttle=240, wheel_rps=(rps, rps, rps, rps))
        for i in range(30)
    ]
    assert detect_wheelspin(CornerTrace(packets=pkts)) == []


# ---------- understeer -------------------------------------------------------


def test_understeer_fires_when_front_outpaces_rear_under_load() -> None:
    pkts = []
    for i in range(30):
        front = 25.0 / 0.33
        rear = front / 1.25  # front 25% faster than rear
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=90,
                throttle=80,
                accel_lat=1.2 * G,
                wheel_rps=(front, front, rear, rear),
            )
        )
    events = detect_understeer(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "steering.understeer"
    assert events[0].evidence["peak_lat_g"] >= 1.1


def test_understeer_quiet_without_lateral_load() -> None:
    front = 25.0 / 0.33
    rear = front / 1.25
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=90,
            throttle=80,
            accel_lat=0.1 * G,
            wheel_rps=(front, front, rear, rear),
        )
        for i in range(30)
    ]
    assert detect_understeer(CornerTrace(packets=pkts)) == []


def test_understeer_quiet_when_axles_track_evenly() -> None:
    rps = 25.0 / 0.33
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=90,
            throttle=80,
            accel_lat=1.2 * G,
            wheel_rps=(rps, rps, rps, rps),
        )
        for i in range(30)
    ]
    assert detect_understeer(CornerTrace(packets=pkts)) == []


# ---------- lockup -----------------------------------------------------------


def test_lockup_fires_when_wheel_near_zero_at_speed() -> None:
    pkts = []
    for i in range(30):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=90,
                brake=255,
                wheel_rps=(0.5, 50.0, 50.0, 50.0),
            )
        )
    events = detect_lockup(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "braking.lockup"
    assert events[0].evidence["min_wheel_rps"] < 5.0


def test_lockup_quiet_at_low_speed() -> None:
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=10,
            brake=255,
            wheel_rps=(0.0, 0.0, 0.0, 0.0),
        )
        for i in range(30)
    ]
    assert detect_lockup(CornerTrace(packets=pkts)) == []


def test_lockup_quiet_without_brake() -> None:
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=90,
            brake=0,
            wheel_rps=(0.5, 50.0, 50.0, 50.0),
        )
        for i in range(30)
    ]
    assert detect_lockup(CornerTrace(packets=pkts)) == []


def test_lockup_quiet_when_wheels_are_rolling() -> None:
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=90,
            brake=255,
            wheel_rps=(75.0, 75.0, 75.0, 75.0),
        )
        for i in range(30)
    ]
    assert detect_lockup(CornerTrace(packets=pkts)) == []


# ---------- end-to-end: bad-corner trace through segmenter + detectors -------


def test_replay_known_bad_trace_fires_all_three_detectors() -> None:
    """Phase-2 spec requirement: replay a known-bad fixture and assert ≥1
    event of each detector type.
    """
    seg = CornerSegmenter()
    corners = _feed_all(seg, build_bad_corner_trace())
    assert corners, "segmenter found no corners in synthetic trace"

    all_late = []
    all_spin = []
    all_under = []
    for trace in corners:
        all_late.extend(detect_late_brake(trace, config=LateBrakeConfig(min_peak_brake=80)))
        all_spin.extend(detect_wheelspin(trace, config=WheelspinConfig(min_throttle=100)))
        all_under.extend(detect_understeer(trace, config=UndersteerConfig(min_lat_g=0.7)))

    assert all_late, "late_brake detector found nothing in known-bad trace"
    assert all_spin, "wheelspin detector found nothing in known-bad trace"
    assert all_under, "understeer detector found nothing in known-bad trace"

    # Sanity: severities are sane floats in [0, 1].
    for evt in (*all_late, *all_spin, *all_under):
        assert 0.0 < evt.severity <= 1.0
        assert evt.t_offset >= 0
        assert isinstance(evt.evidence, dict)


# --- corner thresholds must follow the car, not a Gr.3 assumption -----------


def test_corner_thresholds_scale_with_the_cars_grip():
    """Tuned on a Gr.3 pulling ~2.1 g. A rally car on dirt manages ~0.7 and
    would never cross a fixed 0.95 g entry threshold — the coach would go
    silent for a whole stage."""
    seg = CornerSegmenter()
    assert seg._entry_lat_g == pytest.approx(0.95), "fixed default before the car is known"

    seg.set_grip_limit(2.1)  # the car the thresholds were tuned on
    assert seg._entry_lat_g == pytest.approx(0.95, abs=0.01)
    assert seg._exit_lat_g == pytest.approx(0.53, abs=0.01)

    seg.set_grip_limit(0.7)  # rally car on dirt
    assert seg._entry_lat_g < 0.4, "a dirt car must be able to trigger a corner"
    assert seg._exit_lat_g < 0.2


def test_a_low_grip_car_still_produces_corners():
    """With fixed thresholds a 0.7 g car never starts a corner on lateral load."""
    fixed = CornerSegmenter()
    scaled = CornerSegmenter()
    scaled.set_grip_limit(0.7)

    found_fixed = found_scaled = 0
    for i in range(400):
        # A long dirt corner: 0.55 g sustained, no heavy braking.
        cornering = 120 < i < 300
        pkt = make_packet(
            recv_time=i / 60,
            speed_kmh=90.0,
            accel_lat=(0.55 * 9.80665) if cornering else 0.0,
            brake=0,
            throttle=180,
        )
        if fixed.feed(pkt) is not None:
            found_fixed += 1
        if scaled.feed(pkt) is not None:
            found_scaled += 1

    assert found_fixed == 0, "baseline: the fixed threshold cannot see this corner"
    assert found_scaled >= 1, "scaled thresholds must detect it"


# --- brake released before the apex (the dominant habit in four sessions) ---


def _braking_corner(brake_at_apex: int, coast_frames: int = 40):
    """Decelerate to a minimum, with the brake either carried in or dumped."""
    pkts = []
    n = 90
    for i in range(n):
        v = 180.0 - 1.2 * i if i < 60 else 108.0 + 1.0 * (i - 60)
        if i < 60 - coast_frames:
            brake = 220
        elif i < 60:
            brake = brake_at_apex
        else:
            brake = 0
        pkts.append(
            make_packet(recv_time=i / 60, speed_kmh=v, brake=brake, accel_lat=1.2 * 9.80665)
        )
    return CornerTrace(packets=pkts)


def test_no_trail_fires_when_the_brake_is_dumped_before_the_apex():
    """Across four real sessions the driver released fully before the slowest
    point in 89-98% of braked corners, and nothing ever named it."""
    events = detect_no_trail(_braking_corner(brake_at_apex=0))
    assert events, "coasting to the apex must be detected"
    assert events[0].type == "braking.no_trail"
    assert events[0].evidence["coast_before_apex_s"] > 0.25


def test_no_trail_stays_quiet_when_the_brake_is_carried_in():
    assert detect_no_trail(_braking_corner(brake_at_apex=90)) == []


def test_no_trail_ignores_corners_without_real_braking():
    pkts = [
        make_packet(recv_time=i / 60, speed_kmh=150.0, brake=0, accel_lat=1.0 * 9.80665)
        for i in range(60)
    ]
    assert detect_no_trail(CornerTrace(packets=pkts)) == []


def test_no_trail_ignores_a_momentary_release_at_the_apex():
    assert detect_no_trail(_braking_corner(brake_at_apex=0, coast_frames=8)) == []
