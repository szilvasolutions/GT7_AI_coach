"""Shutdown ordering: SIGINT during a pending corner must not lose advice.

Regression test for the bug where the SIGINT handler called voice.stop()
before the finally block drained the advisor worker, killing the TTS
thread so the worker's voice.speak(advice) silently no-op'd.
"""

from __future__ import annotations

import threading
import time

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    MockProvider,
    RateLimiter,
    RateLimiterConfig,
)
from gt7coach.detectors import CornerTrace, Event
from gt7coach.voice import NullVoiceEngine
from tests._synth import make_packet


def _trace(packets):
    return CornerTrace(packets=packets)


class _CountingVoice(NullVoiceEngine):
    """NullVoiceEngine that auto-becomes-idle a configurable delay after speak()."""

    def __init__(self, *, drain_delay_s: float = 0.0) -> None:
        super().__init__()
        self._drain_delay_s = drain_delay_s
        self._busy_until = 0.0
        self.stop_calls = 0

    def speak(self, text: str) -> None:
        super().speak(text)
        self._busy_until = time.monotonic() + self._drain_delay_s

    def is_idle(self) -> bool:
        return time.monotonic() >= self._busy_until

    def stop(self) -> None:
        self.stop_calls += 1


def test_shutdown_drains_pending_corner_and_speaks_advice() -> None:
    """Simulate SIGINT mid-LLM: voice still speaks the advice after drain."""

    started = threading.Event()

    def slow_responder(_s, _u):
        started.set()
        time.sleep(0.10)
        return "Brake earlier next time."

    provider = MockProvider(responder=slow_responder)
    voice = _CountingVoice(drain_delay_s=0.05)
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth", min_severity=0.0, async_mode=True),
    )

    pkts = [make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=100) for i in range(20)]
    evt = Event(type="braking.late_brake", severity=0.8, t_offset=0.0)

    # Enqueue a corner. on_corner returns immediately; worker is now busy.
    advisor.on_corner(_trace(pkts), [evt], now=0.0)
    assert started.wait(timeout=1.0), "worker should pick up the corner"

    # SIGINT-like: stop the source of new work, but do NOT touch voice.
    # (No rx in this test — the receive-loop equivalent is simply done.)

    # finally-block-equivalent drain sequence (mirrors src/gt7coach/main.py).
    advisor.flush(timeout=2.0)
    advisor.stop()
    deadline = time.monotonic() + 5.0
    while not voice.is_idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    voice.stop()

    assert voice.spoken == ["Brake earlier next time."], (
        f"worker advice should have reached the voice engine; got {voice.spoken!r}"
    )
    assert voice.is_idle(), "drain loop should have waited until voice was idle"
    assert voice.stop_calls == 1


def test_shutdown_drain_loop_respects_timeout() -> None:
    """If the voice never drains, the drain loop bounds itself and exits."""

    voice = _CountingVoice(drain_delay_s=10.0)  # would-be-busy long after we give up
    voice.speak("stuck advice")

    deadline = time.monotonic() + 0.25
    start = time.monotonic()
    while not voice.is_idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    elapsed = time.monotonic() - start

    assert elapsed < 0.4, "drain loop must exit by its own deadline"
    assert not voice.is_idle(), "the stuck voice never drained, by construction"
