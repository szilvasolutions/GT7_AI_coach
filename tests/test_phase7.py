"""Phase-7 tests: async-result callback, slide detection, clean-corner positivity,
real track DB, and lap-end summaries."""

from __future__ import annotations

from collections import Counter

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    MockProvider,
    RateLimiter,
    RateLimiterConfig,
)
from gt7coach.coach.advisor import AdvisorResult
from gt7coach.coach.laps import LapTracker, _canned_summary, _format_lap_time
from gt7coach.detectors import CornerTrace, Event
from gt7coach.detectors.incident import IncidentDetector, IncidentDetectorConfig
from gt7coach.detectors.quality import detect_clean_corner
from gt7coach.tracks import load_default_tracks
from gt7coach.voice import NullVoiceEngine
from tests._synth import make_packet

G = 9.80665


# ---- async-result callback -------------------------------------------------


def _trace(packets):
    return CornerTrace(packets=packets)


def test_on_result_callback_fires_for_async_worker_completion() -> None:
    captured: list[tuple[int, AdvisorResult]] = []

    def on_result(idx, _trace_arg, result):
        captured.append((idx, result))

    provider = MockProvider(responder=lambda _s, _u: "Brake earlier next time.")
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(min_severity=0.0, async_mode=True),
        on_result=on_result,
    )
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    advisor.on_corner(
        _trace(pkts),
        [Event(type="braking.late_brake", severity=0.8, t_offset=0.0)],
        corner_idx=7,
    )
    advisor.flush(timeout=2.0)
    advisor.stop()
    # Exactly one callback invocation, with the actual advice + the right
    # corner index threaded through.
    assert len(captured) == 1
    idx, result = captured[0]
    assert idx == 7
    assert isinstance(result, AdvisorResult)
    assert result.advice == "Brake earlier next time."
    assert result.suppressed_reason is None


def test_on_result_callback_fires_for_synchronous_suppressions() -> None:
    captured: list[tuple[int, AdvisorResult]] = []

    def on_result(idx, _trace_arg, result):
        captured.append((idx, result))

    provider = MockProvider()
    advisor = Advisor(
        provider=provider,
        voice=NullVoiceEngine(),
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0)),
        config=AdvisorConfig(async_mode=False, min_severity=0.5),
        on_result=on_result,
    )
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    # Severity below threshold -> the synchronous "below-min-severity" path
    # also fires the callback now.
    advisor.on_corner(
        _trace(pkts),
        [Event(type="braking.late_brake", severity=0.2, t_offset=0.0)],
        corner_idx=4,
    )
    assert len(captured) == 1
    idx, result = captured[0]
    assert idx == 4
    assert result.suppressed_reason is not None
    assert "below-min-severity" in result.suppressed_reason


# ---- slide incident ---------------------------------------------------------


def test_slide_incident_fires_on_wheelspin_with_speed_drop() -> None:
    """The user's corner #25 scenario: rear wheels spinning fast, speed dumped."""
    det = IncidentDetector(IncidentDetectorConfig(cooldown_s=0.1))
    pkts = []
    # First 0.5 s at high speed.
    for i in range(30):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=90,
                wheel_rps=(75.0, 75.0, 76.0, 76.0),
            )
        )
    # Speed crashes to 30 km/h with rear wheels spinning at 2x front (slide).
    for i in range(30, 60):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=30,
                wheel_rps=(20.0, 20.0, 40.0, 40.0),  # rear/front = 2.0
            )
        )

    incidents = []
    for p in pkts:
        inc = det.feed(p)
        if inc is not None:
            incidents.append(inc)

    assert any(i.type == "slide" for i in incidents), (
        f"expected a slide incident, got types {[i.type for i in incidents]}"
    )


def test_no_slide_at_full_throttle_high_speed_exit() -> None:
    """High-speed wheelspin during corner exit must NOT trigger a slide."""
    det = IncidentDetector()
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=120,  # above slide_max_speed_kmh (90 default)
            wheel_rps=(80.0, 80.0, 100.0, 100.0),  # 1.25 ratio
        )
        for i in range(60)
    ]
    for p in pkts:
        inc = det.feed(p)
        assert not (inc and inc.type == "slide"), "got false-positive slide at high speed"


# ---- quality.clean_corner --------------------------------------------------


def test_clean_corner_fires_on_high_g_no_lift() -> None:
    pkts = []
    for i in range(60):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=120,
                accel_lat=1.4 * G,
                throttle=200,
            )
        )
    trace = _trace(pkts)
    events = detect_clean_corner(trace, other_events=[])
    assert len(events) == 1
    assert events[0].type == "quality.clean_corner"
    assert events[0].severity < 0.3


def test_clean_corner_quiet_when_other_events_present() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=120, accel_lat=1.4 * G, throttle=200)
        for i in range(60)
    ]
    trace = _trace(pkts)
    # If ANY other event is present, the corner isn't clean.
    other = [Event(type="braking.late_brake", severity=0.5, t_offset=0.0)]
    assert detect_clean_corner(trace, other_events=other) == []


def test_clean_corner_quiet_below_g_threshold() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=120, accel_lat=0.7 * G, throttle=200)
        for i in range(60)
    ]
    assert detect_clean_corner(_trace(pkts), other_events=[]) == []


def test_clean_corner_quiet_if_driver_lifted_off() -> None:
    pkts = []
    for i in range(60):
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=i * 0.02,
                speed_kmh=120,
                accel_lat=1.4 * G,
                throttle=10 if 20 < i < 50 else 200,  # 30-frame lift mid-corner
            )
        )
    assert detect_clean_corner(_trace(pkts), other_events=[]) == []


# ---- real track DB ---------------------------------------------------------


def test_track_database_loads_84_tracks() -> None:
    tracks = load_default_tracks()
    assert len(tracks) >= 80, f"expected ~84 tracks, got {len(tracks)}"
    assert "DeepForestRaceway" in tracks
    dfr = tracks["DeepForestRaceway"]
    assert dfr.display_name == "Deep Forest Raceway"
    assert len(dfr.polyline_x) > 100
    assert len(dfr.polyline_z) == len(dfr.polyline_x)
    # Shape description should be templated, never empty.
    assert len(dfr.shape_description) > 10
    # Bbox should bound the polyline.
    x_min, x_max, z_min, z_max = dfr.bbox
    assert x_min <= min(dfr.polyline_x)
    assert x_max >= max(dfr.polyline_x)
    assert z_min <= min(dfr.polyline_z)
    assert z_max >= max(dfr.polyline_z)


def test_track_database_includes_popular_tracks() -> None:
    tracks = load_default_tracks()
    expected_ids = {
        "DeepForestRaceway",
        "SuzukaCircuit",
        "BrandsHatchIndyCircuit",
        "WeathertechRacewayLagunaSeca",
        "TrialMountainCircuit",
    }
    missing = expected_ids - set(tracks)
    assert not missing, f"expected tracks missing from DB: {missing}"


# ---- lap summary -----------------------------------------------------------


def test_format_lap_time_basic() -> None:
    assert _format_lap_time(83456) == "1:23.456"
    assert _format_lap_time(125700) == "2:05.700"
    assert _format_lap_time(0) == ""


def test_canned_summary_personal_best() -> None:
    out = _canned_summary(last_lap_ms=82000, best_lap_ms=82000, delta_ms=0, counts=Counter())
    assert "personal best" in out.lower()
    assert "1:22.000" in out


def test_canned_summary_dominant_mistake() -> None:
    counts = Counter({"throttle.early_lift": 4, "braking.late_brake": 1})
    out = _canned_summary(last_lap_ms=85000, best_lap_ms=82000, delta_ms=3000, counts=counts)
    assert "1:25.000" in out
    assert "throttle" in out.lower() or "gas" in out.lower()


def test_lap_tracker_speaks_on_lap_transition() -> None:
    provider = MockProvider(responder=lambda _s, _u: "Tidy lap, on it.")
    voice = NullVoiceEngine()
    tracker = LapTracker(provider=provider, voice=voice, driver_style="smooth")

    # First packet (lap 1, no time yet).
    tracker.feed_packet(make_packet(packet_id=1, recv_time=0.0, lap_count=1, lap_time_ms=-1))
    tracker.feed_events([Event(type="throttle.early_lift", severity=0.5, t_offset=0.0)])
    # Cross the line into lap 2 — lap_time_ms reports lap 1's time.
    spoken = tracker.feed_packet(
        make_packet(packet_id=100, recv_time=90.0, lap_count=2, lap_time_ms=82000)
    )
    assert spoken == "Tidy lap, on it."
    assert voice.spoken == ["Tidy lap, on it."]
    assert tracker.best_lap_ms == 82000


def test_lap_tracker_uses_canned_fallback_when_provider_fails() -> None:
    def boom(_s, _u):
        from gt7coach.coach.providers import ProviderError

        raise ProviderError("transient")

    provider = MockProvider(responder=boom)
    voice = NullVoiceEngine()
    tracker = LapTracker(provider=provider, voice=voice)

    tracker.feed_packet(make_packet(packet_id=1, recv_time=0.0, lap_count=1, lap_time_ms=-1))
    spoken = tracker.feed_packet(
        make_packet(packet_id=100, recv_time=90.0, lap_count=2, lap_time_ms=82000)
    )
    # Some canned line that includes the lap time.
    assert spoken is not None
    assert "1:22.000" in spoken


def test_lap_tracker_ignores_first_lap_transition_with_no_time() -> None:
    """lap_time_ms=-1 means "no completed lap yet" — don't speak."""
    provider = MockProvider(responder=lambda _s, _u: "Boom!")
    voice = NullVoiceEngine()
    tracker = LapTracker(provider=provider, voice=voice)

    tracker.feed_packet(make_packet(packet_id=1, recv_time=0.0, lap_count=0, lap_time_ms=-1))
    # Lap transitions 0 -> 1 with no time recorded yet (formation lap).
    spoken = tracker.feed_packet(
        make_packet(packet_id=100, recv_time=5.0, lap_count=1, lap_time_ms=-1)
    )
    assert spoken is None
    assert voice.spoken == []
