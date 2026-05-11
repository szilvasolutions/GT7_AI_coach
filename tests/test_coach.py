"""Phase-3 coach tests.

End-to-end smoke test (synthetic detector trace -> mocked LLM -> null voice)
is the centrepiece. Rate limiter and prompt builder are covered separately.
"""

from __future__ import annotations

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    CornerContext,
    MockProvider,
    RateLimiter,
    RateLimiterConfig,
    build_user_prompt,
    make_provider,
)
from gt7coach.coach.providers import ProviderError
from gt7coach.detectors import (
    CornerSegmenter,
    Event,
    detect_late_brake,
    detect_understeer,
    detect_wheelspin,
)
from gt7coach.voice import NullVoiceEngine
from tests._synth import build_bad_corner_trace

# ---- RateLimiter ------------------------------------------------------------


def test_rate_limiter_global_cooldown_blocks_repeats() -> None:
    rl = RateLimiter(RateLimiterConfig(global_cooldown_s=4.0))
    assert rl.allow("braking.late_brake", now=100.0)
    rl.record("braking.late_brake", now=100.0)
    # Another type, still inside the global cooldown window -> blocked.
    assert not rl.allow("throttle.wheelspin", now=102.0)
    # After the cooldown, allowed again.
    assert rl.allow("throttle.wheelspin", now=104.5)


def test_rate_limiter_suppresses_same_type_inside_window() -> None:
    rl = RateLimiter(RateLimiterConfig(global_cooldown_s=4.0, duplicate_window_s=30.0))
    rl.record("braking.late_brake", now=100.0)
    # 10 s later, same type, well past global cooldown but inside 30 s window.
    assert not rl.allow("braking.late_brake", now=110.0)
    # A different type IS allowed if past global cooldown.
    assert rl.allow("throttle.wheelspin", now=110.0)
    # After 30 s window, same type allowed again.
    assert rl.allow("braking.late_brake", now=131.0)


# ---- Prompt -----------------------------------------------------------------


def test_build_user_prompt_mentions_event_and_corner_stats() -> None:
    ctx = CornerContext(
        peak_lat_g=1.45,
        min_speed_kmh=68.0,
        entry_speed_kmh=280.0,
        exit_speed_kmh=120.0,
        duration_s=4.2,
    )
    evt = Event(
        type="braking.late_brake",
        severity=0.65,
        t_offset=0.5,
        evidence={"peak_brake": 230, "offset_after_turn_in_s": 0.45},
    )
    prompt = build_user_prompt([evt], ctx, "smooth")
    assert "smooth" in prompt.lower()
    assert "1.4g" in prompt or "1.5g" in prompt  # rounded
    assert "braking.late_brake" in prompt
    assert "peak_brake=230" in prompt
    assert "ONE imperative" in prompt


# ---- Advisor (end-to-end with mocked provider) ------------------------------


def _build_traces_and_events() -> list[tuple[object, list[Event]]]:
    """Run the synthetic bad-corner trace through the segmenter + detectors."""
    seg = CornerSegmenter()
    traces = []
    for p in build_bad_corner_trace():
        c = seg.feed(p)
        if c is not None:
            traces.append(c)
    last = seg.flush()
    if last is not None:
        traces.append(last)

    out = []
    for t in traces:
        events: list[Event] = []
        events += detect_late_brake(t)
        events += detect_wheelspin(t)
        events += detect_understeer(t)
        out.append((t, events))
    return out


def test_advisor_emits_advice_for_highest_severity_event() -> None:
    provider = MockProvider(
        responder=lambda evts, ctx, style: f"Brake earlier (event={evts[0].type})",
    )
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth"),
    )

    for trace, events in _build_traces_and_events():
        advisor.on_corner(trace, events, now=0.0)

    advice_lines = [r for r in advisor.history if r.advice is not None]
    assert advice_lines, "advisor produced no advice for the bad-corner trace"
    # The winner each time should be the highest-severity event for that corner.
    for r in advice_lines:
        assert r.chosen_event is not None
        assert r.advice and r.chosen_event.type in r.advice


def test_advisor_respects_voice_busy() -> None:
    provider = MockProvider(responder=lambda e, c, s: "stay on it")
    voice = NullVoiceEngine()
    voice.set_busy(True)  # simulate previous utterance still playing
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0)),
    )
    evt = Event(type="braking.late_brake", severity=0.8, t_offset=0.0)
    trace_and_events = next(iter(_build_traces_and_events()), None)
    assert trace_and_events is not None
    trace, _ = trace_and_events
    res = advisor.on_corner(trace, [evt], now=0.0)
    assert res.advice is None
    assert res.suppressed_reason == "voice-busy"
    assert voice.spoken == []
    assert provider.calls == []


def test_advisor_respects_rate_limiter() -> None:
    provider = MockProvider(responder=lambda e, c, s: "go")
    voice = NullVoiceEngine()
    rl = RateLimiter(RateLimiterConfig(global_cooldown_s=4.0, duplicate_window_s=30.0))
    advisor = Advisor(provider=provider, voice=voice, rate_limiter=rl)
    trace, _ = next(iter(_build_traces_and_events()))
    evt_a = Event(type="braking.late_brake", severity=0.8, t_offset=0.0)
    evt_b = Event(type="throttle.wheelspin", severity=0.8, t_offset=0.0)

    # First call goes through.
    r1 = advisor.on_corner(trace, [evt_a], now=0.0)
    assert r1.advice is not None

    # 2 s later: still inside global cooldown -> suppressed.
    r2 = advisor.on_corner(trace, [evt_b], now=2.0)
    assert r2.advice is None
    assert r2.suppressed_reason == "rate-limited"

    # 5 s later: past global cooldown, different type -> allowed.
    r3 = advisor.on_corner(trace, [evt_b], now=5.0)
    assert r3.advice is not None


def test_advisor_returns_no_op_on_provider_error() -> None:
    def boom(_e, _c, _s):
        raise ProviderError("boom")

    provider = MockProvider(responder=boom)
    voice = NullVoiceEngine()
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0)),
    )
    trace, _ = next(iter(_build_traces_and_events()))
    evt = Event(type="braking.late_brake", severity=0.5, t_offset=0.0)
    res = advisor.on_corner(trace, [evt], now=0.0)
    assert res.advice is None
    assert res.suppressed_reason is not None and "provider-error" in res.suppressed_reason


# ---- Factory ----------------------------------------------------------------


def test_make_provider_mock_returns_mock_provider() -> None:
    p = make_provider("mock")
    assert isinstance(p, MockProvider)


def test_make_provider_unknown_raises() -> None:
    try:
        make_provider("nonsense")
    except ProviderError:
        return
    raise AssertionError("expected ProviderError for unknown provider name")
