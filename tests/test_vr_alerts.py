"""Tests for the VR voice-HUD alert detector + phrase formatter."""

from __future__ import annotations

from gt7coach.config import VRAlertsConfig
from gt7coach.detectors.base import Event
from gt7coach.detectors.vr_alerts import VRAlertDetector, format_vr_phrase
from tests._synth import make_packet


def _cfg(**overrides) -> VRAlertsConfig:
    c = VRAlertsConfig()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


# ---- tire temperature -------------------------------------------------------


def test_tyre_hot_fires_when_any_wheel_exceeds_threshold() -> None:
    det = VRAlertDetector(cfg=_cfg(tyre_temp_hot_c=100.0))
    p = make_packet(recv_time=10.0, tyre_temp=(80.0, 115.0, 80.0, 80.0))
    events = det.feed(p)
    assert len(events) == 1
    assert events[0].type == "vr.tyre_hot"
    assert events[0].evidence["tyre"] == "front right"
    assert events[0].evidence["temp_c"] == 115.0


def test_tyre_hot_suppressed_within_local_cooldown() -> None:
    det = VRAlertDetector(cfg=_cfg(tyre_temp_hot_c=100.0))
    det.feed(make_packet(recv_time=0.0, tyre_temp=(115.0, 80.0, 80.0, 80.0)))
    # 5s later — local cooldown is 30s.
    events = det.feed(make_packet(recv_time=5.0, tyre_temp=(115.0, 80.0, 80.0, 80.0)))
    assert events == []


def test_tyre_cold_fires_when_any_wheel_below_threshold() -> None:
    det = VRAlertDetector(cfg=_cfg(tyre_temp_cold_c=70.0))
    p = make_packet(recv_time=10.0, tyre_temp=(85.0, 85.0, 55.0, 85.0))
    events = det.feed(p)
    assert any(e.type == "vr.tyre_cold" for e in events)


def test_tyre_alerts_disabled_emits_nothing() -> None:
    det = VRAlertDetector(cfg=_cfg(tyre_temp_enabled=False))
    p = make_packet(recv_time=10.0, tyre_temp=(200.0, 200.0, 200.0, 200.0))
    assert det.feed(p) == []


# ---- fuel -------------------------------------------------------------------


def test_low_fuel_fires_after_burn_rate_learned() -> None:
    cfg = _cfg(fuel_low_laps_remaining=3.0, fuel_critical_laps_remaining=1.5)
    det = VRAlertDetector(cfg=cfg)
    # Lap 1: full tank at 60L
    det.feed(make_packet(recv_time=0.0, lap_count=1, fuel_level=60.0, fuel_capacity=100.0))
    # Lap 2: 55L → burnt 5L/lap
    det.feed(make_packet(recv_time=90.0, lap_count=2, fuel_level=55.0, fuel_capacity=100.0))
    # Mid-lap 2 at 12L → 12/5 = 2.4 laps remaining → fires low_fuel (≤3)
    events = det.feed(
        make_packet(recv_time=100.0, lap_count=2, fuel_level=12.0, fuel_capacity=100.0)
    )
    assert any(e.type == "vr.low_fuel" for e in events)


def test_fuel_critical_outranks_low_fuel() -> None:
    cfg = _cfg(fuel_low_laps_remaining=3.0, fuel_critical_laps_remaining=1.5)
    det = VRAlertDetector(cfg=cfg)
    det.feed(make_packet(recv_time=0.0, lap_count=1, fuel_level=60.0, fuel_capacity=100.0))
    det.feed(make_packet(recv_time=90.0, lap_count=2, fuel_level=55.0, fuel_capacity=100.0))
    # 6L / 5L per lap = 1.2 laps → critical (≤1.5), and only critical fires.
    events = det.feed(
        make_packet(recv_time=100.0, lap_count=2, fuel_level=6.0, fuel_capacity=100.0)
    )
    types = [e.type for e in events]
    assert "vr.fuel_critical" in types
    assert "vr.low_fuel" not in types


def test_fuel_alert_quiet_before_burn_rate_known() -> None:
    det = VRAlertDetector(cfg=_cfg())
    # First lap of a session — no burn rate learned yet, so no fuel alert.
    events = det.feed(make_packet(recv_time=0.0, lap_count=1, fuel_level=2.0, fuel_capacity=100.0))
    assert all(not e.type.startswith("vr.low_fuel") for e in events)


def test_fuel_alert_ignores_zero_capacity_packets() -> None:
    det = VRAlertDetector(cfg=_cfg())
    p = make_packet(recv_time=0.0, fuel_level=0.0, fuel_capacity=0.0)
    assert det.feed(p) == []


# ---- coolant ----------------------------------------------------------------


def test_oil_hot_fires_above_threshold() -> None:
    det = VRAlertDetector(cfg=_cfg(oil_hot_c=130.0))
    events = det.feed(make_packet(recv_time=0.0, oil_temp=135.0, water_temp=80.0))
    assert any(e.type == "vr.oil_hot" for e in events)


def test_water_hot_fires_above_threshold() -> None:
    det = VRAlertDetector(cfg=_cfg(water_hot_c=110.0))
    events = det.feed(make_packet(recv_time=0.0, oil_temp=90.0, water_temp=115.0))
    assert any(e.type == "vr.water_hot" for e in events)


# ---- shift assist -----------------------------------------------------------


def test_shift_up_fires_only_on_crossing() -> None:
    det = VRAlertDetector(cfg=_cfg(shift_assist_enabled=True))
    # Frame 1: below shift threshold.
    det.feed(make_packet(recv_time=0.0, rpm=6000.0, rev_light_min=7500, rev_light_max=8500))
    # Frame 2: above — should fire once.
    e2 = det.feed(make_packet(recv_time=0.1, rpm=7800.0, rev_light_min=7500, rev_light_max=8500))
    # Frame 3: still above — should NOT re-fire.
    e3 = det.feed(make_packet(recv_time=0.2, rpm=7900.0, rev_light_min=7500, rev_light_max=8500))
    assert any(e.type == "vr.shift_up" for e in e2)
    assert all(e.type != "vr.shift_up" for e in e3)


# ---- self-delta -------------------------------------------------------------


def test_self_delta_down_fires_on_pb() -> None:
    det = VRAlertDetector(cfg=_cfg(self_delta_threshold_ms=200))
    # Lap finishes with last=80.000, best=80.500 → -500ms → fires "_down".
    events = det.feed(make_packet(recv_time=0.0, lap_time_ms=80_000, best_lap_ms=80_500))
    types = [e.type for e in events]
    assert "vr.self_delta_down" in types


def test_self_delta_up_fires_when_slower() -> None:
    det = VRAlertDetector(cfg=_cfg(self_delta_threshold_ms=200))
    events = det.feed(make_packet(recv_time=0.0, lap_time_ms=82_000, best_lap_ms=80_000))
    assert any(e.type == "vr.self_delta_up" for e in events)


def test_self_delta_quiet_inside_threshold() -> None:
    det = VRAlertDetector(cfg=_cfg(self_delta_threshold_ms=300))
    events = det.feed(make_packet(recv_time=0.0, lap_time_ms=80_100, best_lap_ms=80_000))
    assert all(not e.type.startswith("vr.self_delta") for e in events)


def test_self_delta_only_fires_once_per_lap() -> None:
    det = VRAlertDetector(cfg=_cfg(self_delta_threshold_ms=200))
    det.feed(make_packet(recv_time=0.0, lap_time_ms=80_000, best_lap_ms=80_500))
    # Subsequent packets with the same lap_time_ms must not re-fire.
    e2 = det.feed(make_packet(recv_time=0.1, lap_time_ms=80_000, best_lap_ms=80_500))
    assert all(not e.type.startswith("vr.self_delta") for e in e2)


# ---- phrase formatter -------------------------------------------------------


def test_format_vr_phrase_tyre_hot() -> None:
    ev = Event(
        type="vr.tyre_hot",
        severity=0.7,
        t_offset=0.0,
        evidence={"tyre": "front right", "temp_c": 115.0},
    )
    assert format_vr_phrase(ev) == "front right tire hot, 115.0 degrees."


def test_format_vr_phrase_self_delta_renders_seconds() -> None:
    ev = Event(
        type="vr.self_delta_up",
        severity=0.3,
        t_offset=0.0,
        evidence={"last_lap_ms": 82_000, "best_lap_ms": 80_000, "delta_ms": 2000},
    )
    assert format_vr_phrase(ev) == "Down 2.0 seconds."


def test_format_vr_phrase_unknown_type_returns_none() -> None:
    ev = Event(type="braking.late_brake", severity=0.5, t_offset=0.0, evidence={})
    assert format_vr_phrase(ev) is None
