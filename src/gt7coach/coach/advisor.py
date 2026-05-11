"""Advisor: glues detected events to provider to voice.

Owns the per-corner policy (highest-severity event wins) and consults the
rate limiter and the voice queue before calling the LLM. Builds the prompt
itself so that the prompt + response can be logged verbatim by the session
logger (which the provider doesn't see).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from gt7coach.coach.prompt import SYSTEM_PROMPT, build_user_prompt
from gt7coach.coach.providers import CoachProvider, ProviderError
from gt7coach.coach.rate_limiter import RateLimiter
from gt7coach.detectors import CornerTrace, Event
from gt7coach.detectors.base import G_MS2
from gt7coach.voice.base import VoiceEngine

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CornerContext:
    """Summary stats handed to the provider — never raw telemetry rows."""

    peak_lat_g: float
    min_speed_kmh: float
    entry_speed_kmh: float
    exit_speed_kmh: float
    duration_s: float

    @classmethod
    def from_trace(cls, trace: CornerTrace) -> CornerContext:
        return cls(
            peak_lat_g=round(trace.peak_lat_g, 2),
            min_speed_kmh=round(trace.min_speed_kmh, 1),
            entry_speed_kmh=round(trace.entry_speed_kmh, 1),
            exit_speed_kmh=round(trace.exit_speed_kmh, 1),
            duration_s=round(trace.duration_s, 2),
        )


@dataclass(slots=True)
class AdvisorConfig:
    driver_style: str = "smooth"


@dataclass(slots=True)
class AdvisorResult:
    """What happened for a given corner. Useful for logs + tests."""

    advice: str | None
    chosen_event: Event | None
    suppressed_reason: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None


class Advisor:
    """Per ARCHITECTURE.md section 7."""

    def __init__(
        self,
        provider: CoachProvider,
        voice: VoiceEngine,
        rate_limiter: RateLimiter,
        config: AdvisorConfig | None = None,
    ) -> None:
        self.provider = provider
        self.voice = voice
        self.rate_limiter = rate_limiter
        self.config = config or AdvisorConfig()
        self.history: list[AdvisorResult] = []

    def on_corner(
        self,
        trace: CornerTrace,
        events: Iterable[Event],
        *,
        now: float | None = None,
    ) -> AdvisorResult:
        events_list = list(events)
        if not events_list:
            return self._record(None, None, "no events")

        # Per-corner: pick the highest-severity event (section 7).
        winner = max(events_list, key=lambda e: e.severity)

        if not self.rate_limiter.allow(winner.type, now=now):
            return self._record(None, winner, "rate-limited")

        # Don't pile up if the previous utterance is still playing.
        if not self.voice.is_idle():
            return self._record(None, winner, "voice-busy")

        ctx = CornerContext.from_trace(trace)
        user_prompt = build_user_prompt([winner], ctx, self.config.driver_style)

        try:
            advice = self.provider.complete(SYSTEM_PROMPT, user_prompt)
        except ProviderError as exc:
            log.warning("provider failed: %s", exc)
            return self._record(
                None,
                winner,
                f"provider-error: {exc}",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

        advice = (advice or "").strip()
        if not advice:
            return self._record(
                None,
                winner,
                "empty-response",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

        self.rate_limiter.record(winner.type, now=now)
        self.voice.speak(advice)
        return self._record(
            advice,
            winner,
            None,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    def _record(
        self,
        advice: str | None,
        chosen_event: Event | None,
        reason: str | None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> AdvisorResult:
        result = AdvisorResult(
            advice=advice,
            chosen_event=chosen_event,
            suppressed_reason=reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self.history.append(result)
        if advice is not None:
            log.info(
                "coach -> %r (event=%s sev=%.2f)",
                advice,
                chosen_event.type if chosen_event else "?",
                chosen_event.severity if chosen_event else 0.0,
            )
        elif chosen_event is not None and reason:
            log.debug(
                "coach suppressed (%s): %s sev=%.2f",
                reason,
                chosen_event.type,
                chosen_event.severity,
            )
        return result


__all__ = ["G_MS2", "Advisor", "AdvisorConfig", "AdvisorResult", "CornerContext"]
