"""Phase-2 detector tests.

Each detector is tested against synthetic traces. The corner segmenter is
also tested for hysteresis + min-dwell behaviour (legacy bug §11.7 must not
regress).
"""

from __future__ import annotations

from gt7coach.detectors import (
    CornerSegmenter,
    CornerSegmenterConfig,
    CornerTrace,
    LateBrakeConfig,
    UndersteerConfig,
    WheelspinConfig,
    detect_late_brake,
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
