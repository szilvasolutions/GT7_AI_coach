"""Duration-aware cue timing: scheduler geometry + advisor hold behaviour."""

from __future__ import annotations

import threading
import time

from gt7coach.coach import Advisor, AdvisorConfig, MockProvider, RateLimiter, RateLimiterConfig
from gt7coach.coach.cue_timing import CueScheduler, CueTimingConfig
from gt7coach.detectors import CornerTrace, Event
from gt7coach.tracks.database import Track, Turn
from gt7coach.voice.base import estimate_speech_seconds
from gt7coach.voice.null_engine import NullVoiceEngine
from tests._synth import make_packet


def _square_track() -> Track:
    """400 m x 400 m square, one polyline point every 10 m, apex at each corner.

    Index layout: 0-39 along z=0 (x rising), 40-79 along x=400 (z rising),
    80-119 along z=400 (x falling), 120-159 along x=0 (z falling).
    """
    xs: list[float] = []
    zs: list[float] = []
    for i in range(40):
        xs.append(i * 10.0)
        zs.append(0.0)
    for i in range(40):
        xs.append(400.0)
        zs.append(i * 10.0)
    for i in range(40):
        xs.append(400.0 - i * 10.0)
        zs.append(400.0)
    for i in range(40):
        xs.append(0.0)
        zs.append(400.0 - i * 10.0)
    turns = [
        Turn(index=40, x=400.0, z=0.0, turning_radius_m=30.0),
        Turn(index=80, x=400.0, z=400.0, turning_radius_m=30.0),
        Turn(index=120, x=0.0, z=400.0, turning_radius_m=30.0),
        Turn(index=0, x=0.0, z=0.0, turning_radius_m=30.0),
    ]
    return Track(
        id="square",
        display_name="Square Test Circuit",
        country=None,
        length_m=1600,
        num_corners=4,
        is_oval=False,
        bbox=(0.0, 400.0, 0.0, 400.0),
        polyline_x=xs,
        polyline_z=zs,
        turns=turns,
        shape_description="square test circuit",
    )


def _packet(x: float, z: float, speed_kmh: float):
    return make_packet(packet_id=1, recv_time=0.0, speed_kmh=speed_kmh, pos=(x, 0.0, z))


# ---- CueScheduler geometry ---------------------------------------------------


def test_seconds_to_next_turn_straight_run() -> None:
    sched = CueScheduler()
    sched.set_track(_square_track())
    # Mid first straight at (200, 0): 200 m to the (400, 0) apex, at 40 m/s.
    sched.feed(_packet(200.0, 0.0, speed_kmh=144.0))
    eta = sched.seconds_to_next_turn()
    assert eta is not None
    assert abs(eta - 5.0) < 0.15


def test_seconds_to_next_turn_wraps_to_first_turn() -> None:
    sched = CueScheduler()
    sched.set_track(_square_track())
    # On the last side at (0, 100), heading down to the (0, 0) apex: 100 m.
    sched.feed(_packet(0.0, 100.0, speed_kmh=72.0))  # 20 m/s
    eta = sched.seconds_to_next_turn()
    assert eta is not None
    assert abs(eta - 5.0) < 0.15


def test_no_constraint_without_track_or_at_low_speed_or_off_line() -> None:
    sched = CueScheduler()
    sched.feed(_packet(200.0, 0.0, speed_kmh=144.0))
    assert sched.seconds_to_next_turn() is None  # no track yet
    assert sched.clearance(10.0)

    sched.set_track(_square_track())
    sched.feed(_packet(200.0, 0.0, speed_kmh=5.0))
    assert sched.seconds_to_next_turn() is None  # crawling: pits / grid / spin
    sched.feed(_packet(5000.0, 5000.0, speed_kmh=144.0))
    assert sched.seconds_to_next_turn() is None  # nowhere near the racing line


def test_clearance_threshold_and_disable() -> None:
    sched = CueScheduler(CueTimingConfig(finish_margin_s=2.5))
    sched.set_track(_square_track())
    sched.feed(_packet(200.0, 0.0, speed_kmh=144.0))  # ~5 s window
    assert sched.clearance(2.0)  # 2.0 + 2.5 <= 5.0
    assert not sched.clearance(3.0)  # 3.0 + 2.5 > 5.0

    off = CueScheduler(CueTimingConfig(enabled=False))
    off.set_track(_square_track())
    off.feed(_packet(395.0, 0.0, speed_kmh=144.0))  # right on top of an apex
    assert off.clearance(10.0)


# ---- speech duration estimate --------------------------------------------------


def test_estimate_speech_seconds_scales_with_words_and_rate() -> None:
    # 4 words at 120 wpm = 2 words/s -> 2.0 s + 0.4 s overhead.
    assert abs(estimate_speech_seconds("one two three four", wpm=120.0) - 2.4) < 1e-6
    # Faster rate -> shorter estimate; never zero even for empty text.
    assert estimate_speech_seconds("one two three four", wpm=240.0) < 2.4
    assert estimate_speech_seconds("", wpm=200.0) > 0.0


def test_null_engine_estimates_nonzero_duration() -> None:
    assert NullVoiceEngine().estimate_duration("Brake earlier next lap.") > 0.5


# ---- Advisor hold behaviour ----------------------------------------------------


class _StubScheduler(CueScheduler):
    """Scheduler with a directly controllable gate (no geometry)."""

    def __init__(self, *, allow: bool, max_hold_s: float = 6.0, poll_s: float = 0.02) -> None:
        super().__init__(CueTimingConfig(max_hold_s=max_hold_s, poll_s=poll_s))
        self.allow = allow

    def seconds_to_next_turn(self) -> float | None:
        return 1.0

    def clearance(self, duration_s: float) -> bool:
        return self.allow


def _advisor(provider, voice, sched, *, async_mode: bool) -> Advisor:
    return Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(min_severity=0.0, async_mode=async_mode),
        cue_scheduler=sched,
    )


def _corner(idx: int = 1):
    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    evt = Event(type="braking.late_brake", severity=0.8, t_offset=0.0)
    return CornerTrace(packets=pkts), [evt], idx


def test_hold_opens_then_speaks() -> None:
    sched = _StubScheduler(allow=False, max_hold_s=5.0)
    voice = NullVoiceEngine()
    advisor = _advisor(
        MockProvider(responder=lambda s, u: "Brake earlier."), voice, sched, async_mode=True
    )
    trace, events, idx = _corner()
    advisor.on_corner(trace, events, corner_idx=idx)
    # Open the window shortly after the worker starts holding.
    threading.Timer(0.15, lambda: setattr(sched, "allow", True)).start()
    advisor.flush(timeout=3.0)
    advisor.stop()
    assert voice.spoken == ["Brake earlier."]


def test_hold_times_out_and_speaks_anyway() -> None:
    sched = _StubScheduler(allow=False, max_hold_s=0.2)
    voice = NullVoiceEngine()
    advisor = _advisor(
        MockProvider(responder=lambda s, u: "Brake earlier."), voice, sched, async_mode=True
    )
    trace, events, idx = _corner()
    t0 = time.monotonic()
    advisor.on_corner(trace, events, corner_idx=idx)
    advisor.flush(timeout=3.0)
    advisor.stop()
    assert voice.spoken == ["Brake earlier."]
    assert time.monotonic() - t0 >= 0.2  # it really did hold before giving up


def test_hold_dropped_when_newer_corner_arrives() -> None:
    sched = _StubScheduler(allow=False, max_hold_s=3.0)
    voice = NullVoiceEngine()
    calls: list[int] = []

    def responder(_s, _u):
        calls.append(1)
        return f"Advice number {len(calls)}."

    advisor = _advisor(MockProvider(responder=responder), voice, sched, async_mode=True)
    trace, events, idx = _corner(1)
    advisor.on_corner(trace, events, corner_idx=idx)
    time.sleep(0.15)  # let the worker enter the hold loop for corner 1
    trace2, events2, idx2 = _corner(2)
    sched.allow = True  # corner 2 gets a clear window immediately
    advisor.on_corner(trace2, events2, corner_idx=idx2)
    advisor.flush(timeout=3.0)
    advisor.stop()
    # Corner 1's advice was superseded mid-hold and never spoken.
    assert voice.spoken == ["Advice number 2."]
    reasons = [r.suppressed_reason for r in advisor.history]
    assert "superseded-while-holding-cue" in reasons


def test_sync_mode_never_blocks_on_hold() -> None:
    sched = _StubScheduler(allow=False, max_hold_s=5.0)
    voice = NullVoiceEngine()
    advisor = _advisor(
        MockProvider(responder=lambda s, u: "Brake earlier."), voice, sched, async_mode=False
    )
    trace, events, idx = _corner()
    t0 = time.monotonic()
    advisor.on_corner(trace, events, corner_idx=idx)
    # Sync mode speaks immediately — a held cue would stall the receive loop.
    assert time.monotonic() - t0 < 1.0
    assert voice.spoken == ["Brake earlier."]
