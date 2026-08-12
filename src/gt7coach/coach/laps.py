"""Lap tracking + end-of-lap summary.

Detects lap_count transitions, aggregates events since the last boundary,
and asks the LLM (or falls back to canned phrases) for a 1-2 sentence
between-laps debrief. The summary is spoken via ``voice.interrupt`` so it
preempts whatever stale corner advice was queued mid-corner.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, field

from gt7coach.coach.prompt import LAP_SUMMARY_SYSTEM_PROMPT
from gt7coach.coach.providers import CoachProvider, ProviderError
from gt7coach.detectors import Event
from gt7coach.telemetry.packet import Packet
from gt7coach.voice.base import VoiceEngine

log = logging.getLogger(__name__)


def _format_lap_time(ms: int) -> str:
    if ms <= 0:
        return ""
    total_s = ms / 1000.0
    m = int(total_s // 60)
    s = total_s - m * 60
    return f"{m}:{s:06.3f}"


def _format_best_lap_callout(
    last_lap_ms: int,
    best_lap_ms: int,
    delta_ms: int | None,
    is_pb: bool,
) -> str:
    """Short PB / delta callout used by the 'best_lap' and 'both' modes."""
    time_str = _format_lap_time(last_lap_ms)
    if is_pb:
        return f"New personal best, {time_str}."
    if delta_ms is None or best_lap_ms <= 0:
        return time_str + "."
    delta_s = delta_ms / 1000.0
    return f"{time_str}, plus {delta_s:.1f}."


def _canned_summary(
    last_lap_ms: int,
    best_lap_ms: int,
    delta_ms: int | None,
    counts: Counter[str],
) -> str:
    """Pick a fallback line when the LLM isn't reachable."""
    time_str = _format_lap_time(last_lap_ms)

    # Personal best?
    if delta_ms is not None and last_lap_ms == best_lap_ms:
        return f"New personal best — {time_str}."

    # Within 0.3 s of best?
    if delta_ms is not None and 0 <= delta_ms <= 300:
        return f"{time_str}, right on the money."

    # Has dominant mistake?
    if counts:
        dominant = counts.most_common(1)[0][0]
        action = {
            "throttle.early_lift": "Stay on the gas next lap.",
            "throttle.wheelspin": "Less throttle on exit.",
            "throttle.sawing": "Smooth the throttle.",
            "braking.late_brake": "Brake earlier.",
            "braking.lockup": "Trail off the brake.",
            "braking.trail_off_too_fast": "Ease off the brake more gradually.",
            "steering.understeer": "Less steering, more patience.",
            "steering.oversteer": "Counter and ease off.",
            "line.late_apex": "Hit the apex sooner.",
        }.get(dominant, "Tighten up next lap.")
        return f"{time_str}. {action}"

    # No mistakes? Compliment.
    return random.choice(
        (
            f"Clean lap, {time_str}.",
            f"Tidy, {time_str}.",
            f"Solid lap, {time_str}.",
        )
    )


@dataclass
class LapTracker:
    """Watches lap-count transitions and produces between-lap summaries.

    Subscribers feed every packet + every event. When the lap number
    increments, the tracker:
      1. Updates the running personal-best.
      2. Builds a short summary via the LLM (or a canned fallback).
      3. Speaks it through ``voice.interrupt`` so it preempts mid-corner
         advice and lands at the start/finish line.

    Call ``feed_packet`` and ``feed_events`` from the receive loop; the
    tracker is otherwise stateless and very cheap.
    """

    provider: CoachProvider
    voice: VoiceEngine
    driver_style: str = "smooth"
    summary_max_tokens: int = 96
    # End-of-lap voice mode: "recommendation" (LLM summary, current
    # behaviour), "best_lap" (short PB callout, no LLM call), or "both".
    announce_mode: str = "recommendation"

    _last_lap_count: int = field(default=-1, init=False)
    _best_lap_ms: int = field(default=-1, init=False)
    _current_lap_event_counts: Counter[str] = field(default_factory=Counter, init=False)
    _seen_first_packet: bool = field(default=False, init=False)

    history: list[dict] = field(default_factory=list, init=False)

    def feed_events(self, events: list[Event]) -> None:
        for e in events:
            self._current_lap_event_counts[e.type] += 1

    def feed_packet(self, packet: Packet) -> str | None:
        """Watch for lap transitions; return the spoken summary if one was just delivered."""
        if not self._seen_first_packet:
            self._seen_first_packet = True
            self._last_lap_count = packet.lap_count
            return None

        # The 0 -> 1 transition is the race start — no lap time yet, but it
        # is the wall-clock anchor scripts/build_demo_video.py fits a screen
        # recording against, so log it. Keep the format.
        if packet.lap_count == 1 and self._last_lap_count == 0:
            log.info("race start (lap 1 began)")

        # Only act on a real increment (not the initial 0 -> 1 lap-1 start;
        # we want the lap-1 -> lap-2 transition because lap_time_ms only
        # becomes valid then).
        if packet.lap_count > self._last_lap_count and packet.lap_count > 1:
            spoken = self._on_lap_complete(packet.lap_time_ms)
            self._last_lap_count = packet.lap_count
            self._current_lap_event_counts.clear()
            return spoken

        # Track straight-up best lap regardless of transition order.
        if packet.lap_time_ms > 0 and (
            self._best_lap_ms < 0 or packet.lap_time_ms < self._best_lap_ms
        ):
            self._best_lap_ms = packet.lap_time_ms

        self._last_lap_count = max(self._last_lap_count, packet.lap_count)
        return None

    @property
    def best_lap_ms(self) -> int:
        return self._best_lap_ms

    # ---- internals -------------------------------------------------------

    def _llm_or_canned_summary(
        self,
        last_lap_ms: int,
        delta_ms: int | None,
        counts: Counter[str],
        is_pb: bool,
    ) -> str:
        user_prompt = self._build_prompt(last_lap_ms, delta_ms, counts, is_pb)
        try:
            text = self.provider.complete(
                LAP_SUMMARY_SYSTEM_PROMPT,
                user_prompt,
                max_tokens=self.summary_max_tokens,
            )
        except ProviderError as exc:
            log.warning("provider failed on lap summary: %s — using fallback", exc)
            text = ""
        text = (text or "").strip().strip("\"'`")
        if not text or len(text.split()) < 3:
            text = _canned_summary(last_lap_ms, self._best_lap_ms, delta_ms, counts)
        return text

    def _on_lap_complete(self, last_lap_ms: int) -> str:
        is_pb = last_lap_ms > 0 and (self._best_lap_ms < 0 or last_lap_ms <= self._best_lap_ms)
        if last_lap_ms > 0 and (self._best_lap_ms < 0 or last_lap_ms < self._best_lap_ms):
            self._best_lap_ms = last_lap_ms

        delta_ms = (
            (last_lap_ms - self._best_lap_ms) if last_lap_ms > 0 and self._best_lap_ms > 0 else None
        )
        counts = Counter(self._current_lap_event_counts)

        mode = self.announce_mode
        best_lap_line = _format_best_lap_callout(last_lap_ms, self._best_lap_ms, delta_ms, is_pb)

        if mode == "best_lap":
            # No LLM call. Just speak the short callout — free-tier-friendly.
            text = best_lap_line
            self.voice.interrupt(text)
        elif mode == "both":
            # Speak the short callout immediately, then chain the LLM summary
            # behind it (the LLM call happens after; voice queue handles the
            # ordering).
            self.voice.speak(best_lap_line)
            text = self._llm_or_canned_summary(last_lap_ms, delta_ms, counts, is_pb)
            self.voice.interrupt(text)
            text = f"{best_lap_line} {text}"
        else:  # "recommendation" — current behaviour
            text = self._llm_or_canned_summary(last_lap_ms, delta_ms, counts, is_pb)
            self.voice.interrupt(text)

        log.info("lap %d done (%s): %s", self._last_lap_count, mode, text)
        try:
            from gt7coach import status as _status

            _status.emit(
                "lap",
                lap=self._last_lap_count,
                last_lap_ms=last_lap_ms,
                best_lap_ms=self._best_lap_ms,
                delta_ms=delta_ms,
                summary=text,
            )
        except Exception:  # pragma: no cover
            pass
        self.history.append(
            {
                "lap": self._last_lap_count,
                "last_lap_ms": last_lap_ms,
                "best_lap_ms": self._best_lap_ms,
                "delta_ms": delta_ms,
                "summary": text,
                "event_counts": dict(counts),
            }
        )
        return text

    def _build_prompt(
        self,
        last_lap_ms: int,
        delta_ms: int | None,
        counts: Counter[str],
        is_pb: bool,
    ) -> str:
        lines = [f"Driver style: {self.driver_style}."]
        lines.append(f"Just finished lap. Lap time: {_format_lap_time(last_lap_ms)}.")
        if is_pb:
            lines.append("This is a personal best.")
        elif delta_ms is not None:
            sign = "+" if delta_ms >= 0 else ""
            lines.append(f"Delta to personal best: {sign}{delta_ms / 1000.0:.2f} s.")
        if counts:
            sorted_counts = ", ".join(
                f"{t} x{n}" for t, n in counts.most_common() if t and not t.startswith("quality.")
            )
            clean_count = counts.get("quality.clean_corner", 0)
            if sorted_counts:
                lines.append(f"Mistakes this lap: {sorted_counts}.")
            if clean_count:
                lines.append(f"Clean corners: {clean_count}.")
            if not sorted_counts and not clean_count:
                lines.append("No mistakes detected this lap.")
        else:
            lines.append("No mistakes detected this lap.")
        lines.append("Summarise in 1-2 short sentences, max 20 words.")
        return "\n".join(lines)
