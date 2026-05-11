"""Tests for Phase-4 detectors and summary helpers."""

from __future__ import annotations

import json
from pathlib import Path

from gt7coach.config import default_config, load
from gt7coach.detectors import (
    CornerTrace,
    detect_early_lift,
    detect_late_apex,
    detect_oversteer,
    detect_sawing,
    detect_trail_off_too_fast,
)
from gt7coach.session.summarizer import aggregate, summarise
from tests._synth import make_packet

G = 9.80665


# ---- steering.oversteer -----------------------------------------------------


def test_oversteer_fires_when_rear_outpaces_front_under_load() -> None:
    pkts = []
    for i in range(30):
        front = 25.0 / 0.33
        rear = front * 1.25  # rear 25% faster than front under load
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=90,
                accel_lat=1.2 * G,
                wheel_rps=(front, front, rear, rear),
            )
        )
    events = detect_oversteer(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "steering.oversteer"
    assert events[0].evidence["peak_ratio"] >= 1.20


def test_oversteer_quiet_without_load() -> None:
    front = 25.0 / 0.33
    rear = front * 1.25
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            accel_lat=0.1 * G,
            wheel_rps=(front, front, rear, rear),
        )
        for i in range(30)
    ]
    assert detect_oversteer(CornerTrace(packets=pkts)) == []


# ---- braking.trail_off_too_fast --------------------------------------------


def test_trail_off_fires_on_sudden_release() -> None:
    pkts = []
    for i in range(40):
        t = i * 0.02
        # Brake builds to 200 over 0.4 s, sits for 0.2 s, then DROPS to 30 in
        # one frame -- classic "stab" release. Lat g is high throughout.
        if t < 0.40:
            brake = int(200 * (t / 0.40))
        elif t < 0.60:
            brake = 200
        else:
            brake = 30
        pkts.append(
            make_packet(packet_id=i, recv_time=t, brake=brake, accel_lat=1.2 * G, steer_angle=0.5)
        )
    events = detect_trail_off_too_fast(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "braking.trail_off_too_fast"
    # 170-unit drop in one 0.02 s tick = 8500 units/s; well above 1500 threshold.
    assert events[0].evidence["peak_release_rate"] >= 1500
    assert events[0].evidence["drop_in_one_tick"] >= 130


def test_trail_off_quiet_on_smooth_release() -> None:
    pkts = []
    for i in range(60):
        t = i * 0.02
        if t < 0.40:
            brake = int(200 * (t / 0.40))
        else:
            brake = max(0, int(200 - 100 * (t - 0.40)))  # smooth taper
        pkts.append(
            make_packet(packet_id=i, recv_time=t, brake=brake, accel_lat=1.0 * G, steer_angle=0.5)
        )
    assert detect_trail_off_too_fast(CornerTrace(packets=pkts)) == []


# ---- throttle.sawing --------------------------------------------------------


def test_sawing_fires_on_repeated_direction_changes() -> None:
    pkts = []
    throttle_seq = [200, 50, 200, 50, 200, 50, 200, 50, 200] * 8  # many changes
    for i, thr in enumerate(throttle_seq):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                accel_lat=1.0 * G,
                throttle=thr,
            )
        )
    events = detect_sawing(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "throttle.sawing"
    assert events[0].evidence["changes"] >= 4


def test_sawing_quiet_on_steady_throttle() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, throttle=200, accel_lat=1.0 * G)
        for i in range(80)
    ]
    assert detect_sawing(CornerTrace(packets=pkts)) == []


# ---- throttle.early_lift ----------------------------------------------------


def test_early_lift_fires_when_off_throttle_under_load() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, throttle=20, accel_lat=1.2 * G)
        for i in range(40)
    ]
    events = detect_early_lift(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "throttle.early_lift"
    assert events[0].evidence["min_throttle"] <= 50


def test_early_lift_quiet_when_throttle_present() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, throttle=180, accel_lat=1.2 * G)
        for i in range(40)
    ]
    assert detect_early_lift(CornerTrace(packets=pkts)) == []


# ---- line.late_apex ---------------------------------------------------------


def test_late_apex_fires_when_min_speed_after_max_steer() -> None:
    pkts = []
    # max steer at i=10 (t=0.20s), min speed at i=40 (t=0.80s) -> offset 0.60s
    for i in range(60):
        t = i * 0.02
        speed = max(40.0, 100 - 1.2 * i) if i < 40 else 40.0 + 1.5 * (i - 40)
        steer = 0.6 if 5 <= i <= 15 else 0.2
        pkts.append(make_packet(packet_id=i, recv_time=t, speed_kmh=speed, steer_angle=steer))
    events = detect_late_apex(CornerTrace(packets=pkts))
    assert len(events) == 1
    assert events[0].type == "line.late_apex"
    assert events[0].evidence["offset_after_apex_s"] >= 0.30


def test_late_apex_quiet_on_classic_line() -> None:
    pkts = []
    # max steer at i=20 (t=0.40s), min speed also at i=20 -> offset 0s -> no event
    for i in range(40):
        t = i * 0.02
        steer = 0.7 if 15 <= i <= 25 else 0.3
        speed = 100 - 1.5 * min(i, 20) + 1.5 * max(0, i - 20)
        pkts.append(make_packet(packet_id=i, recv_time=t, speed_kmh=speed, steer_angle=steer))
    assert detect_late_apex(CornerTrace(packets=pkts)) == []


# ---- session.summarizer -----------------------------------------------------


def test_aggregate_reads_events_jsonl(tmp_path: Path) -> None:
    session_dir = tmp_path / "run_x"
    session_dir.mkdir()
    events = [
        {
            "corner_idx": 1,
            "trace": {"min_speed_kmh": 60.0, "peak_lat_g": 1.2},
            "events": [
                {"type": "braking.late_brake", "severity": 0.6, "t_offset": 0.5, "evidence": {}}
            ],
        },
        {
            "corner_idx": 2,
            "trace": {"min_speed_kmh": 85.0, "peak_lat_g": 1.5},
            "events": [
                {"type": "braking.late_brake", "severity": 0.8, "t_offset": 0.5, "evidence": {}},
                {"type": "throttle.wheelspin", "severity": 0.3, "t_offset": 1.0, "evidence": {}},
            ],
        },
    ]
    with (session_dir / "events.jsonl").open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    stats = aggregate(session_dir)
    assert stats.total_corners == 2
    assert stats.total_events == 3
    assert stats.event_counts == {"braking.late_brake": 2, "throttle.wheelspin": 1}
    assert stats.event_avg_severity["braking.late_brake"] == 0.7
    assert stats.peak_lat_g == 1.5
    assert stats.fastest_corner_speed_kmh == 85.0
    assert stats.slowest_corner_speed_kmh == 60.0


def test_summarise_writes_summary_files(tmp_path: Path) -> None:
    from gt7coach.coach import MockProvider

    session_dir = tmp_path / "run_y"
    session_dir.mkdir()
    (session_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "corner_idx": 1,
                "trace": {"min_speed_kmh": 60.0, "peak_lat_g": 1.2},
                "events": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    provider = MockProvider(responder=lambda _s, u: f"Mock summary based on: {u[:30]}")
    text = summarise(session_dir, provider=provider)
    assert text.startswith("Mock summary")
    assert (session_dir / "summary.txt").read_text(encoding="utf-8") == text
    assert (session_dir / "summary_prompt.txt").is_file()


# ---- config -----------------------------------------------------------------


def test_default_config_enables_all_phase4_detectors() -> None:
    cfg = default_config()
    expected = {
        "corner.segment",
        "braking.late_brake",
        "braking.lockup",
        "braking.trail_off_too_fast",
        "throttle.wheelspin",
        "throttle.sawing",
        "throttle.early_lift",
        "steering.understeer",
        "steering.oversteer",
        "line.late_apex",
    }
    assert expected.issubset(cfg.detectors_enabled)


def test_load_config_yaml_overrides_defaults(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        """
network:
  ps5_ip: 192.168.1.120
  packet_format: A
coach:
  provider: gemini
  driver_style: aggressive
  global_rate_limit_seconds: 6
voice:
  engine: piper
  speed: 200
detectors:
  enabled: [braking.late_brake]
  thresholds:
    corner_min_speed_kmh: 55
    corner_entry_brake: 80
session:
  generate_summary: false
""",
        encoding="utf-8",
    )
    cfg = load(yaml_path)
    assert cfg.network.ps5_ip == "192.168.1.120"
    assert cfg.network.packet_format == "A"
    assert cfg.coach_provider == "gemini"
    assert cfg.advisor.driver_style == "aggressive"
    assert cfg.rate_limiter.global_cooldown_s == 6.0
    assert cfg.voice.engine == "piper"
    assert cfg.voice.speed == 200
    assert cfg.detectors_enabled == {"braking.late_brake"}
    assert cfg.corner.min_speed_kmh == 55.0
    assert cfg.corner.entry_brake == 80
    assert cfg.session.generate_summary is False


def test_load_config_missing_file_falls_back_to_defaults(tmp_path: Path) -> None:
    cfg = load(tmp_path / "nope.yaml")
    assert cfg.network.ps5_ip == "auto"
    assert cfg.advisor.driver_style == "smooth"
