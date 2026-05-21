"""Phase-5 tests: corner_type, duration cap, top-3 events, recent-advice."""

from __future__ import annotations

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    CornerContext,
    MockProvider,
    RateLimiter,
    RateLimiterConfig,
    build_user_prompt,
)
from gt7coach.coach.advisor import _top_events
from gt7coach.detectors import (
    CornerSegmenter,
    CornerSegmenterConfig,
    CornerTrace,
    Event,
)
from gt7coach.voice import NullVoiceEngine
from tests._synth import make_packet

G = 9.80665


# ---- CornerTrace.corner_type + total_yaw_deg + yaw_sign_flips --------------


def _trace(packets):
    return CornerTrace(packets=packets)


def test_corner_type_hairpin() -> None:
    """Slow + high yaw -> hairpin."""
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=55, yaw_rate=1.2)
        for i in range(80)  # 1.6 s, ~110 degrees of turn
    ]
    t = _trace(pkts)
    assert t.corner_type == "hairpin"
    assert t.total_yaw_deg > 100


def test_corner_type_fast_corner() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=180, yaw_rate=0.4)
        for i in range(60)  # ~28 degrees
    ]
    t = _trace(pkts)
    assert t.corner_type == "fast_corner"


def test_corner_type_sweeper() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=140, yaw_rate=0.6)
        for i in range(250)  # 5 s, fast-ish
    ]
    t = _trace(pkts)
    assert t.corner_type == "sweeper"


def test_corner_type_chicane_detects_sign_flip() -> None:
    """yaw_rate flips sign -> chicane."""
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=120, yaw_rate=0.8) for i in range(30)
    ] + [
        make_packet(packet_id=30 + i, recv_time=(30 + i) * 0.02, speed_kmh=120, yaw_rate=-0.8)
        for i in range(30)
    ]
    t = _trace(pkts)
    assert t.yaw_sign_flips >= 1
    assert t.corner_type == "chicane"


def test_corner_type_slow_corner_default() -> None:
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=85, yaw_rate=0.3) for i in range(40)
    ]
    t = _trace(pkts)
    assert t.corner_type == "slow_corner"


# ---- CornerSegmenter.max_corner_duration_s ----------------------------------


def test_segmenter_force_splits_at_max_duration() -> None:
    """A 12 s cornering trace must split into 2 pieces with the 8 s cap."""
    seg = CornerSegmenter(CornerSegmenterConfig(max_corner_duration_s=4.0))
    pkts = []
    for i in range(500):  # 10 s at 50 Hz
        t = i * 0.02
        steer = 0.6 if i < 250 else -0.6  # zero-cross at i=250 -> good split point
        pkts.append(
            make_packet(
                packet_id=i,
                recv_time=t,
                speed_kmh=110,
                accel_lat=1.2 * G,
                steer_angle=steer,
                yaw_rate=0.8 if i < 250 else -0.8,
            )
        )
    # Then 1 s of straight so the segmenter can finalise the trailing piece.
    pkts += [
        make_packet(packet_id=500 + i, recv_time=10.0 + i * 0.02, speed_kmh=140, throttle=255)
        for i in range(60)
    ]
    out = []
    for p in pkts:
        c = seg.feed(p)
        if c is not None:
            out.append(c)
    leftover = seg.flush()
    if leftover is not None:
        out.append(leftover)

    assert len(out) >= 2, f"expected at least 2 segments, got {len(out)}"
    for c in out:
        assert c.duration_s <= 5.0, f"segment too long: {c.duration_s}"


def test_segmenter_no_split_when_cap_disabled() -> None:
    """max_corner_duration_s=0 keeps the legacy single-segment behaviour."""
    seg = CornerSegmenter(CornerSegmenterConfig(max_corner_duration_s=0))
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100, accel_lat=1.2 * G)
        for i in range(500)  # 10 s
    ]
    pkts += [
        make_packet(packet_id=500 + i, recv_time=10.0 + i * 0.02, speed_kmh=140, throttle=255)
        for i in range(60)
    ]
    out = []
    for p in pkts:
        c = seg.feed(p)
        if c is not None:
            out.append(c)
    leftover = seg.flush()
    if leftover is not None:
        out.append(leftover)
    assert len(out) == 1
    assert out[0].duration_s > 9.0


# ---- _top_events -----------------------------------------------------------


def test_top_events_dedupes_by_type() -> None:
    """Five wheelspins + one late_brake -> only one wheelspin entry, top severity."""
    events = [
        Event(type="throttle.wheelspin", severity=0.3, t_offset=0.0),
        Event(type="throttle.wheelspin", severity=0.7, t_offset=0.5),
        Event(type="throttle.wheelspin", severity=0.9, t_offset=1.0),
        Event(type="braking.late_brake", severity=0.5, t_offset=0.0),
        Event(type="steering.understeer", severity=0.6, t_offset=2.0),
    ]
    top = _top_events(events, n=3)
    assert len(top) == 3
    assert {e.type for e in top} == {
        "throttle.wheelspin",
        "braking.late_brake",
        "steering.understeer",
    }
    # The wheelspin we kept must be the highest-severity one.
    wheelspin = next(e for e in top if e.type == "throttle.wheelspin")
    assert wheelspin.severity == 0.9


def test_top_events_respects_n_cap() -> None:
    events = [Event(type=f"x{i}", severity=0.5 + i * 0.05, t_offset=0.0) for i in range(10)]
    assert len(_top_events(events, n=3)) == 3
    assert len(_top_events(events, n=5)) == 5


# ---- build_user_prompt new fields ------------------------------------------


def test_user_prompt_includes_corner_type_and_recent_advice() -> None:
    ctx = CornerContext(
        peak_lat_g=1.45,
        min_speed_kmh=68.0,
        entry_speed_kmh=280.0,
        exit_speed_kmh=120.0,
        duration_s=4.2,
        corner_type="hairpin",
        total_yaw_deg=135.0,
    )
    evt = Event(
        type="braking.late_brake",
        severity=0.65,
        t_offset=0.5,
        evidence={"peak_brake": 230},
    )
    prompt = build_user_prompt(
        [evt],
        ctx,
        "smooth",
        recent_advice=[
            ("throttle.early_lift", "Carry throttle through the apex."),
            ("braking.late_brake", "Brake earlier into Turn 3."),
        ],
    )
    assert "hairpin" in prompt
    assert "135" in prompt  # total yaw degrees
    assert "Recent advice" in prompt
    assert "Carry throttle through the apex" in prompt


def test_user_prompt_omits_recent_block_when_empty() -> None:
    ctx = CornerContext.from_trace(
        _trace([make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)])
    )
    evt = Event(type="braking.late_brake", severity=0.5, t_offset=0.0)
    prompt = build_user_prompt([evt], ctx, "smooth", recent_advice=[])
    assert "Recent advice" not in prompt


# ---- Advisor: recent-advice memory + top-3 prompt --------------------------


def _make_advisor(provider):
    return Advisor(
        provider=provider,
        voice=NullVoiceEngine(),
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth", async_mode=False),
    )


def test_advisor_passes_top_3_events_to_provider() -> None:
    captured: dict[str, str] = {}

    def responder(_sys: str, user: str) -> str:
        captured["last"] = user
        return "Carry through."

    provider = MockProvider(responder=responder)
    advisor = _make_advisor(provider)
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(50)]
    trace = _trace(pkts)
    events = [
        Event(type="throttle.wheelspin", severity=0.4, t_offset=0.0),
        Event(type="throttle.wheelspin", severity=0.9, t_offset=1.0),
        Event(type="steering.oversteer", severity=0.6, t_offset=0.5),
        Event(type="throttle.early_lift", severity=0.8, t_offset=2.0),
        Event(type="braking.late_brake", severity=0.3, t_offset=0.0),
    ]
    advisor.on_corner(trace, events, now=0.0)
    sent = captured["last"]
    # Top 3 distinct types by severity should all appear in the prompt.
    assert "throttle.wheelspin" in sent
    assert "throttle.early_lift" in sent
    assert "steering.oversteer" in sent
    # Lowest-severity types dropped.
    assert "braking.late_brake" not in sent


def test_advisor_skips_llm_when_max_severity_below_threshold() -> None:
    """Tiny events shouldn't waste an LLM call -- and shouldn't speak garbage."""
    provider = MockProvider(responder=lambda _s, _u: "Stay on it.")
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth", min_severity=0.30, async_mode=False),
    )
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(50)]
    trace = _trace(pkts)

    # Below the gate: no LLM call.
    res_low = advisor.on_corner(
        trace, [Event(type="line.late_apex", severity=0.05, t_offset=0.0)], now=0.0
    )
    assert res_low.advice is None
    assert (
        res_low.suppressed_reason is not None and "below-min-severity" in res_low.suppressed_reason
    )
    assert provider.calls == []

    # Above the gate: normal path.
    res_high = advisor.on_corner(
        trace, [Event(type="braking.late_brake", severity=0.8, t_offset=0.0)], now=1.0
    )
    assert res_high.advice is not None
    assert len(provider.calls) == 1


def test_advisor_falls_back_when_provider_returns_one_word() -> None:
    """Single-word responses are treated as failures and replaced with the canned phrase."""
    provider = MockProvider(responder=lambda _s, _u: "Open")  # single word -> rejected
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(min_severity=0.0, async_mode=False),
    )
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(50)]
    trace = _trace(pkts)
    evt = Event(type="line.late_apex", severity=0.5, t_offset=0.0)
    res = advisor.on_corner(trace, [evt], now=0.0)
    assert res.advice == "Hit the apex sooner next lap."  # canned phrase for line.late_apex
    assert res.suppressed_reason is not None and "too-short-response" in res.suppressed_reason


def test_advisor_records_recent_advice_and_feeds_it_back() -> None:
    captured: list[str] = []

    def responder(_sys: str, user: str) -> str:
        captured.append(user)
        return "Carry more."

    provider = MockProvider(responder=responder)
    advisor = _make_advisor(provider)
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(50)]
    trace = _trace(pkts)
    evt = Event(type="throttle.early_lift", severity=0.9, t_offset=0.0)

    advisor.on_corner(trace, [evt], now=0.0)
    advisor.on_corner(trace, [evt], now=1.0)
    advisor.on_corner(trace, [evt], now=2.0)

    # The first call sees no recent advice; the second sees the first; etc.
    assert "Recent advice" not in captured[0]
    assert "Recent advice" in captured[1]
    assert "Carry more." in captured[1]
    assert "Recent advice" in captured[2]
    # Third call sees two prior items.
    assert captured[2].count("Carry more.") == 2
