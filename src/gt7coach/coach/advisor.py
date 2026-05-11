"""Advisor: glues detected events to provider to voice.

Owns the per-corner policy (highest-severity event wins) and consults the
rate limiter and the voice queue before calling the LLM. Builds the prompt
itself so that the prompt + response can be logged verbatim by the session
logger (which the provider doesn't see).
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

from gt7coach.coach.prompt import SARCASTIC_SYSTEM_PROMPT, SYSTEM_PROMPT, build_user_prompt
from gt7coach.coach.providers import CoachProvider, ProviderError
from gt7coach.coach.rate_limiter import RateLimiter
from gt7coach.detectors import CornerTrace, Event, Incident
from gt7coach.detectors.base import G_MS2
from gt7coach.voice.base import VoiceEngine

log = logging.getLogger(__name__)

_TOP_EVENTS_PER_CORNER = 3  # how many distinct event types we expose to the LLM
_RECENT_ADVICE_DEPTH = 3  # how much advice history the LLM gets to discourage repeats


# Canned coaching phrases, one per event type. Spoken when the LLM provider
# fails or returns an empty response, so the driver always gets *some*
# audible feedback for a real detected event instead of dead silence. Kept
# short and imperative — same constraints as the LLM system prompt.
_FALLBACK_PHRASES: dict[str, str] = {
    "braking.late_brake": "Brake earlier.",
    "braking.lockup": "Lockup. Trail off the brakes.",
    "braking.trail_off_too_fast": "Release the brake more gradually.",
    "throttle.wheelspin": "Less throttle on exit.",
    "throttle.sawing": "Smooth the throttle.",
    "throttle.early_lift": "Stay on the throttle through the corner.",
    "steering.understeer": "Less steering, more patience.",
    "steering.oversteer": "Counter and ease off.",
    "line.late_apex": "Hit the apex sooner.",
}


def fallback_phrase(event_type: str) -> str:
    return _FALLBACK_PHRASES.get(event_type, "")


# Canned sarcastic remarks used when the LLM is unavailable for an incident.
# Multiple per type so a string of incidents doesn't sound robotic.
_INCIDENT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "spin": (
        "Well, that was elegant.",
        "Plenty of practice corners back there.",
        "Just doing some gardening, then.",
        "Right, who taught you to drive?",
        "That'll do, pirouette.",
    ),
    "crash": (
        "That'll buff right out.",
        "Box this lap. Apparently.",
        "Hope you brought the spare.",
        "Cars do not, in fact, bounce.",
        "Bit of a moment there.",
    ),
}


def incident_fallback_phrase(incident_type: str) -> str:
    options = _INCIDENT_FALLBACKS.get(incident_type)
    return random.choice(options) if options else ""


@dataclass(slots=True)
class CornerContext:
    """Summary stats handed to the provider — never raw telemetry rows."""

    peak_lat_g: float
    min_speed_kmh: float
    entry_speed_kmh: float
    exit_speed_kmh: float
    duration_s: float
    corner_type: str = "medium_corner"
    total_yaw_deg: float = 0.0

    @classmethod
    def from_trace(cls, trace: CornerTrace) -> CornerContext:
        return cls(
            peak_lat_g=round(trace.peak_lat_g, 2),
            min_speed_kmh=round(trace.min_speed_kmh, 1),
            entry_speed_kmh=round(trace.entry_speed_kmh, 1),
            exit_speed_kmh=round(trace.exit_speed_kmh, 1),
            duration_s=round(trace.duration_s, 2),
            corner_type=trace.corner_type,
            total_yaw_deg=round(trace.total_yaw_deg, 1),
        )


def _top_events(events: list[Event], n: int = _TOP_EVENTS_PER_CORNER) -> list[Event]:
    """Pick the most informative subset of events for one corner.

    Sort by severity descending, then dedupe by event ``type`` so that 5
    wheelspin events from one corner collapse into a single one for the LLM.
    Returns at most ``n`` items.
    """
    seen: set[str] = set()
    out: list[Event] = []
    for evt in sorted(events, key=lambda e: -e.severity):
        if evt.type in seen:
            continue
        seen.add(evt.type)
        out.append(evt)
        if len(out) >= n:
            break
    return out


@dataclass(slots=True)
class AdvisorConfig:
    driver_style: str = "smooth"
    # Don't waste an LLM call on a corner whose highest-severity event is
    # this low. Low-severity events tend to produce single-word responses
    # ("Open") because the model decides there isn't much to say. Below
    # this threshold the advisor stays silent.
    min_severity: float = 0.30


@dataclass(slots=True)
class AdvisorResult:
    """What happened for a given corner. Useful for logs + tests."""

    advice: str | None
    chosen_event: Event | None
    suppressed_reason: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None


@dataclass(slots=True)
class IncidentResult:
    """What happened for a given incident (spin / crash). Symmetric to AdvisorResult."""

    advice: str | None
    incident: Incident
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
        self.incident_history: list[IncidentResult] = []
        # (event_type, advice_text) of the last few utterances, surfaced to the
        # LLM so it can vary its phrasing across consecutive corners.
        self._recent_advice: deque[tuple[str, str]] = deque(maxlen=_RECENT_ADVICE_DEPTH)

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

        # Per-corner: pick the highest-severity event for rate-limit decisions
        # and as the headline "what to talk about" (section 7). But pass the
        # top N distinct event types to the LLM so it can pick the best angle
        # if e.g. wheelspin + oversteer both fired (they're the same root
        # cause).
        winner = max(events_list, key=lambda e: e.severity)
        top = _top_events(events_list)

        if winner.severity < self.config.min_severity:
            return self._record(None, winner, f"below-min-severity ({winner.severity:.2f})")

        if not self.rate_limiter.allow(winner.type, now=now):
            return self._record(None, winner, "rate-limited")

        # Don't pile up if the previous utterance is still playing.
        if not self.voice.is_idle():
            return self._record(None, winner, "voice-busy")

        ctx = CornerContext.from_trace(trace)
        user_prompt = build_user_prompt(
            top,
            ctx,
            self.config.driver_style,
            recent_advice=list(self._recent_advice),
        )

        try:
            advice = self.provider.complete(SYSTEM_PROMPT, user_prompt)
            failure_reason: str | None = None
        except ProviderError as exc:
            log.warning("provider failed: %s — falling back to canned phrase", exc)
            advice = ""
            failure_reason = f"provider-error: {exc}"

        advice = (advice or "").strip()
        # Reject single-word responses (Gemini's lighter models occasionally
        # emit "Open" / "Carry" / "Brake" alone despite the system prompt).
        # The canned fallback phrases are all 2+ words so they pass this gate.
        if advice and len(advice.split()) < 2:
            log.warning("provider returned single-word response %r; using fallback", advice)
            failure_reason = f"too-short-response: {advice!r}"
            advice = ""
        if not advice:
            # Provider failed or returned nothing. Speak the canned phrase
            # for this event type so the driver still gets feedback.
            advice = fallback_phrase(winner.type)
            if not advice:
                # No canned phrase for this event type either; give up.
                return self._record(
                    None,
                    winner,
                    failure_reason or "empty-response",
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=user_prompt,
                )
            failure_reason = f"{failure_reason or 'empty-response'}; spoke fallback"

        self.rate_limiter.record(winner.type, now=now)
        self.voice.speak(advice)
        self._recent_advice.append((winner.type, advice))
        return self._record(
            advice,
            winner,
            failure_reason,
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    def on_incident(self, incident: Incident) -> IncidentResult:
        """Handle a spin / crash / impact: speak a sarcastic remark.

        Incidents bypass the corner rate-limiter and the voice-busy gate —
        they're more important than whatever coaching tip was about to play.
        The voice is told to ``interrupt`` (clear the pending queue), and the
        IncidentDetector's own 10 s cooldown prevents repeat fire from one
        prolonged event.
        """
        user_prompt = f"The driver just had a {incident.type}. Roast it in one short line."
        try:
            advice = self.provider.complete(SARCASTIC_SYSTEM_PROMPT, user_prompt, max_tokens=64)
            failure_reason: str | None = None
        except ProviderError as exc:
            log.warning("provider failed on incident: %s — falling back", exc)
            advice = ""
            failure_reason = f"provider-error: {exc}"

        advice = (advice or "").strip()
        # Strip surrounding quotes the LLM sometimes wraps the remark in.
        advice = advice.strip("\"'`")
        if advice and len(advice.split()) < 2:
            failure_reason = f"too-short-response: {advice!r}"
            advice = ""
        if not advice:
            advice = incident_fallback_phrase(incident.type)
            failure_reason = f"{failure_reason or 'empty-response'}; spoke fallback"
        if not advice:
            # No canned phrase for this incident type — extremely unlikely but
            # we still record cleanly.
            return self._record_incident(
                None,
                incident,
                failure_reason or "no-fallback",
                system_prompt=SARCASTIC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

        self.voice.interrupt(advice)
        # Also feed the remark into recent_advice so the next corner advice
        # acknowledges that something happened, instead of pretending it
        # didn't.
        self._recent_advice.append((f"incident.{incident.type}", advice))
        return self._record_incident(
            advice,
            incident,
            failure_reason,
            system_prompt=SARCASTIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    def _record_incident(
        self,
        advice: str | None,
        incident: Incident,
        reason: str | None,
        *,
        system_prompt: str | None = None,
        user_prompt: str | None = None,
    ) -> IncidentResult:
        result = IncidentResult(
            advice=advice,
            incident=incident,
            suppressed_reason=reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self.incident_history.append(result)
        if advice is not None:
            log.info("coach -> %r (incident=%s sev=%.2f)", advice, incident.type, incident.severity)
        elif reason:
            log.warning("incident not voiced (%s): %s", reason, incident.type)
        return result

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


__all__ = [
    "G_MS2",
    "Advisor",
    "AdvisorConfig",
    "AdvisorResult",
    "CornerContext",
    "IncidentResult",
]
