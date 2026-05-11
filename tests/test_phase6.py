"""Phase-6 tests: async LLM worker, zero-event skip, track detection, prompt enrichment."""

from __future__ import annotations

import threading
import time

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    CornerContext,
    MockProvider,
    RateLimiter,
    RateLimiterConfig,
    build_user_prompt,
)
from gt7coach.detectors import CornerTrace, Event
from gt7coach.tracks import TrackDetector
from gt7coach.voice import NullVoiceEngine
from tests._synth import make_packet

G = 9.80665


# ---- CornerTrace new properties (gear / RPM / coasting / tyres) -----------


def test_corner_trace_gear_rpm_coasting_from_synthetic() -> None:
    pkts = []
    for i in range(60):
        is_apex = i == 30
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=60 if is_apex else 100,
                gear=2 if is_apex else 4,
                rpm=4500.0 if is_apex else 7000.0,
                throttle=0,
                brake=0,
                tyre_temp=(50.0, 65.0, 80.0, 105.0),  # cold/warm/optimal/hot
            )
        )
    trace = CornerTrace(packets=pkts)
    assert trace.gear_at_apex == 2
    assert trace.rpm_at_apex == 4500.0
    assert trace.peak_rpm == 7000.0
    assert trace.coasting_fraction == 1.0  # all frames coasting
    fl, fr, rl, rr = trace.tire_temps_c
    assert fl < 60 and 60 <= fr < 75 and 75 <= rl <= 95 and rr > 100


# ---- TrackDetector --------------------------------------------------------


def _packet_at(x: float, z: float, recv_time: float = 0.0):
    return make_packet(packet_id=1, recv_time=recv_time, pos=(x, 0.0, z))


def test_track_detector_matches_deep_forest() -> None:
    det = TrackDetector()
    # Walk along the first ~10 polyline points of Deep Forest. The detector
    # waits for unambiguous data — feeding multiple in-line points lets it
    # rule out other tracks that may overlap at a single coincidence point.
    from gt7coach.tracks.database import load_default_tracks

    dfr = load_default_tracks()["DeepForestRaceway"]
    track = None
    for x, z in zip(dfr.polyline_x[:20], dfr.polyline_z[:20], strict=True):
        track = det.feed(_packet_at(x, z))
        if track is not None:
            break
    assert track is not None
    assert track.id == "DeepForestRaceway"


def test_track_detector_misses_outside_bbox_and_gives_up() -> None:
    det = TrackDetector(max_probes=3)
    out_of_range = _packet_at(-50000.0, -50000.0)
    for _ in range(3):
        assert det.feed(out_of_range) is None
    # Once exhausted, further packets are also None.
    assert det.feed(out_of_range) is None


def test_track_detector_force_overrides() -> None:
    det = TrackDetector()
    track = det.force("DeepForestRaceway")
    assert track.id == "DeepForestRaceway"
    # feed() now short-circuits and returns the forced track.
    assert det.feed(_packet_at(-50000.0, -50000.0)) is track


# ---- Async Advisor --------------------------------------------------------


def _trace(packets):
    return CornerTrace(packets=packets)


def _make_async_advisor(provider, *, voice=None):
    return Advisor(
        provider=provider,
        voice=voice or NullVoiceEngine(),
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth", min_severity=0.0, async_mode=True),
    )


def test_async_advisor_returns_queued_then_speaks_off_thread() -> None:
    """on_corner() returns immediately; the worker speaks asynchronously."""
    spoke_from: list[str] = []
    main_thread_id = threading.get_ident()

    def responder(_s, _u):
        # Record which thread the LLM call happens on.
        spoke_from.append("worker" if threading.get_ident() != main_thread_id else "main")
        return "Brake earlier next time."

    provider = MockProvider(responder=responder)
    voice = NullVoiceEngine()
    advisor = _make_async_advisor(provider, voice=voice)

    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    evt = Event(type="braking.late_brake", severity=0.8, t_offset=0.0)
    result = advisor.on_corner(_trace(pkts), [evt], now=0.0)
    assert result.advice is None
    assert result.suppressed_reason == "queued"

    advisor.flush(timeout=2.0)
    advisor.stop()
    assert spoke_from == ["worker"]
    assert voice.spoken == ["Brake earlier next time."]


def test_async_advisor_drop_newest_replaces_pending() -> None:
    """A slow LLM means a fresh corner replaces the previous pending one."""
    started = threading.Event()
    proceed = threading.Event()
    advice_calls: list[str] = []

    def slow_responder(_s, user):
        advice_calls.append(user)
        if len(advice_calls) == 1:
            started.set()
            proceed.wait(timeout=2.0)
        return "Open the wheel earlier on exit."

    provider = MockProvider(responder=slow_responder)
    voice = NullVoiceEngine()
    advisor = _make_async_advisor(provider, voice=voice)

    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    evt_a = Event(type="braking.late_brake", severity=0.8, t_offset=0.0, evidence={"id": "A"})
    evt_b = Event(type="braking.late_brake", severity=0.8, t_offset=0.0, evidence={"id": "B"})

    advisor.on_corner(_trace(pkts), [evt_a], now=0.0)
    assert started.wait(timeout=1.0), "worker should pick up the first corner"
    advisor.on_corner(_trace(pkts), [evt_b], now=1.0)  # replaces queued slot

    proceed.set()
    advisor.flush(timeout=2.0)
    advisor.stop()
    # Two LLM calls fired (the first slow one, then the queued-newest replacement).
    assert len(advice_calls) == 2
    assert "id=B" in advice_calls[1]


def test_async_advisor_skips_zero_event_corners_without_holding_rate_limiter() -> None:
    provider = MockProvider(responder=lambda _s, _u: "should never speak")
    voice = NullVoiceEngine()
    rl = RateLimiter(RateLimiterConfig(global_cooldown_s=4.0))
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=rl,
        config=AdvisorConfig(min_severity=0.0, async_mode=False),
    )
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    res = advisor.on_corner(_trace(pkts), [], now=0.0)
    assert res.advice is None
    assert res.suppressed_reason == "no events"
    # And the rate limiter is NOT held -- a subsequent real corner should fire.
    assert rl.allow("braking.late_brake", now=0.5)


# ---- Prompt with new fields ----------------------------------------------


def test_prompt_renders_track_car_lap_tyres_gear_rpm_coasting() -> None:
    ctx = CornerContext(
        peak_lat_g=1.4,
        min_speed_kmh=48.0,
        entry_speed_kmh=246.0,
        exit_speed_kmh=48.0,
        duration_s=6.5,
        corner_type="hairpin",
        total_yaw_deg=145.0,
        gear_at_apex=2,
        rpm_at_apex=4500.0,
        peak_rpm=7200.0,
        coasting_fraction=0.12,
        tyre_state="FL warm, FR warm, RL optimal, RR hot",
        lap_count=3,
        last_lap_ms=83456,
        best_lap_ms=82756,
        car_class="Gr.3 RWD",
        track_shape="mountain forest circuit with esses and a hairpin",
    )
    evt = Event(type="braking.late_brake", severity=0.9, t_offset=0.0)
    prompt = build_user_prompt(
        [evt],
        ctx,
        "smooth",
        recent_advice=[],
        recent_events=[("hairpin", "throttle.early_lift"), ("sweeper", "throttle.sawing")],
    )
    assert "Track context: mountain forest" in prompt
    assert "never name the track" in prompt
    assert "Car: Gr.3 RWD" in prompt
    assert "Lap 3" in prompt
    assert "1:23.456" in prompt
    assert "+0.70 vs best" in prompt
    assert "gear 2" in prompt
    assert "4500 rpm" in prompt
    assert "peak 7200 rpm" in prompt
    assert "coasted 12% of corner" in prompt
    assert "Tyres: FL warm" in prompt
    assert "Recent corners (fault pattern): hairpin/throttle.early_lift" in prompt


def test_prompt_omits_optional_blocks_when_empty() -> None:
    ctx = CornerContext(
        peak_lat_g=1.4,
        min_speed_kmh=48.0,
        entry_speed_kmh=100.0,
        exit_speed_kmh=48.0,
        duration_s=2.0,
        corner_type="slow_corner",
    )
    evt = Event(type="braking.late_brake", severity=0.5, t_offset=0.0)
    prompt = build_user_prompt([evt], ctx, "smooth")
    assert "Track context:" not in prompt
    assert "Car:" not in prompt
    assert "Lap " not in prompt
    assert "Tyres:" not in prompt
    assert "Recent corners" not in prompt


# ---- Tyre quantisation ---------------------------------------------------


def test_tyre_state_thresholds() -> None:
    from gt7coach.coach.advisor import _tyre_state

    assert _tyre_state(40.0) == "cold"
    assert _tyre_state(60.0) == "warm"
    assert _tyre_state(80.0) == "optimal"
    assert _tyre_state(110.0) == "hot"


# ---- Lap-time helper -----------------------------------------------------


def test_lap_time_formatting() -> None:
    from gt7coach.coach.prompt import _format_lap_time

    assert _format_lap_time(83456) == "1:23.456"
    assert _format_lap_time(60000) == "1:00.000"
    assert _format_lap_time(0) == ""
    assert _format_lap_time(-1) == ""


# ---- best-lap memory updates across calls --------------------------------


def test_advisor_tracks_best_lap_across_corners() -> None:
    provider = MockProvider(responder=lambda _s, _u: "Brake later.")
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(min_severity=0.0, async_mode=False),
    )
    # First corner: last_lap = 90 s -> becomes best.
    pkts_a = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100, lap_count=2, lap_time_ms=90000)
        for i in range(20)
    ]
    advisor.on_corner(
        _trace(pkts_a),
        [Event(type="braking.late_brake", severity=0.8, t_offset=0.0)],
        now=0.0,
    )
    assert advisor._best_lap_ms == 90000

    # Second corner: last_lap = 85 s -> replaces best.
    pkts_b = [
        make_packet(
            packet_id=100 + i,
            recv_time=2.0 + i * 0.02,
            speed_kmh=100,
            lap_count=3,
            lap_time_ms=85000,
        )
        for i in range(20)
    ]
    advisor.on_corner(
        _trace(pkts_b),
        [Event(type="braking.late_brake", severity=0.8, t_offset=0.0)],
        now=1.0,
    )
    assert advisor._best_lap_ms == 85000

    # Third corner: last_lap = 88 s -> stays at 85.
    pkts_c = [
        make_packet(
            packet_id=200 + i,
            recv_time=4.0 + i * 0.02,
            speed_kmh=100,
            lap_count=4,
            lap_time_ms=88000,
        )
        for i in range(20)
    ]
    advisor.on_corner(
        _trace(pkts_c),
        [Event(type="braking.late_brake", severity=0.8, t_offset=0.0)],
        now=2.0,
    )
    assert advisor._best_lap_ms == 85000


# silence "imported but unused" for `time` (used implicitly via wait() timeouts)
_ = time
