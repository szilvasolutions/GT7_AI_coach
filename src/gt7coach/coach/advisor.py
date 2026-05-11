"""Advisor: glues detected events to provider to voice.

This module hosts:

* :class:`CornerContext` — the structured summary handed to the LLM. Includes
  corner shape (peak g, min speed, type, total yaw, gear/RPM at apex,
  coasting fraction, tyre temps) and session context (lap number, last-lap
  time + delta to best, car class, track shape).
* :class:`Advisor` — orchestrates rate-limit + voice-busy gates and the
  per-corner LLM call. **The LLM call runs on a background worker thread**
  so a slow provider response (Gemini occasionally takes 5-20 s) never
  blocks the telemetry receive loop. The worker uses drop-newest semantics:
  if a fresh corner arrives while it's still working on a previous one,
  the old work is abandoned and the new corner takes its place — stale
  advice for a corner the driver has already left is worse than no advice.
* :meth:`Advisor.on_incident` — the synchronous spin/crash path. Incidents
  are short, interrupt-class messages and don't share the corner worker.
"""

from __future__ import annotations

import logging
import random
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from gt7coach.coach.prompt import (
    COMPLIMENT_SYSTEM_PROMPT,
    SARCASTIC_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from gt7coach.coach.providers import CoachProvider, ProviderError
from gt7coach.coach.rate_limiter import RateLimiter
from gt7coach.detectors import CornerTrace, Event, Incident
from gt7coach.detectors.base import G_MS2
from gt7coach.voice.base import VoiceEngine

log = logging.getLogger(__name__)

_TOP_EVENTS_PER_CORNER = 3
_RECENT_ADVICE_DEPTH = 3
_RECENT_EVENTS_DEPTH = 3
_WORKER_JOIN_TIMEOUT_S = 15.0

# Callback signature for AdvisorResult / IncidentResult delivery. Invoked
# AFTER the result is finalised (which may be on the worker thread for
# corners), so external observers — like the SessionLogger — see the
# actual advice / prompt / failure_reason instead of the synchronous
# "queued" stub the caller of on_corner() received.
ResultCallback = Callable[[int, "CornerTrace | None", "AdvisorResult | IncidentResult"], None]


# ---- Canned fallbacks -------------------------------------------------------


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
    # Positive-feedback fallbacks. Multiple per type so a string of clean
    # corners doesn't sound robotic.
    "quality.clean_corner": "Tidy through that one.",
}


_COMPLIMENT_FALLBACKS: tuple[str, ...] = (
    "Hooked up nicely through that one.",
    "Clean line, plenty of speed.",
    "Solid commitment there.",
    "Tidy through that corner.",
    "Strong, that's the way.",
)


def fallback_phrase(event_type: str) -> str:
    return _FALLBACK_PHRASES.get(event_type, "")


_INCIDENT_FALLBACKS: dict[str, tuple[str, ...]] = {
    "spin": (
        "Well, that was elegant.",
        "Plenty of practice corners back there.",
        "Just doing some gardening, then.",
        "Right, who taught you to drive?",
        "That'll do, pirouette.",
    ),
    "slide": (
        "Skating, are we?",
        "Off-line. Tidy that up.",
        "Wheels need the road.",
        "Found the grass.",
        "Saved it. Sort of.",
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


# ---- Tyre-temperature quantisation ------------------------------------------


def _tyre_state(temp_c: float) -> str:
    if temp_c < 60.0:
        return "cold"
    if temp_c < 75.0:
        return "warm"
    if temp_c <= 95.0:
        return "optimal"
    return "hot"


def _tyre_summary(temps_c: tuple[float, float, float, float]) -> str:
    labels = ("FL", "FR", "RL", "RR")
    return ", ".join(f"{lbl} {_tyre_state(t)}" for lbl, t in zip(labels, temps_c, strict=True))


# ---- CornerContext ----------------------------------------------------------


@dataclass(slots=True)
class CornerContext:
    """Structured summary handed to the provider. Never raw telemetry rows."""

    peak_lat_g: float
    min_speed_kmh: float
    entry_speed_kmh: float
    exit_speed_kmh: float
    duration_s: float
    corner_type: str = "medium_corner"
    total_yaw_deg: float = 0.0
    # Phase-6 additions:
    gear_at_apex: int = 0
    rpm_at_apex: float = 0.0
    peak_rpm: float = 0.0
    coasting_fraction: float = 0.0
    tyre_state: str = ""
    lap_count: int = 0
    last_lap_ms: int = -1
    best_lap_ms: int = -1
    # Session context (set per session, threaded through here for prompt assembly):
    car_class: str = ""
    track_shape: str = ""

    @classmethod
    def from_trace(
        cls,
        trace: CornerTrace,
        *,
        best_lap_ms: int = -1,
        car_class: str = "",
        track_shape: str = "",
    ) -> CornerContext:
        return cls(
            peak_lat_g=round(trace.peak_lat_g, 2),
            min_speed_kmh=round(trace.min_speed_kmh, 1),
            entry_speed_kmh=round(trace.entry_speed_kmh, 1),
            exit_speed_kmh=round(trace.exit_speed_kmh, 1),
            duration_s=round(trace.duration_s, 2),
            corner_type=trace.corner_type,
            total_yaw_deg=round(trace.total_yaw_deg, 1),
            gear_at_apex=trace.gear_at_apex,
            rpm_at_apex=round(trace.rpm_at_apex, 0),
            peak_rpm=round(trace.peak_rpm, 0),
            coasting_fraction=round(trace.coasting_fraction, 2),
            tyre_state=_tyre_summary(trace.tire_temps_c),
            lap_count=trace.lap_count,
            last_lap_ms=trace.last_lap_ms,
            best_lap_ms=best_lap_ms,
            car_class=car_class,
            track_shape=track_shape,
        )


def _top_events(events: list[Event], n: int = _TOP_EVENTS_PER_CORNER) -> list[Event]:
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


# ---- Advisor config / results ----------------------------------------------


@dataclass(slots=True)
class AdvisorConfig:
    driver_style: str = "smooth"
    min_severity: float = 0.30
    car_class: str = ""
    track_shape: str = ""
    async_mode: bool = True


@dataclass(slots=True)
class AdvisorResult:
    advice: str | None
    chosen_event: Event | None
    suppressed_reason: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None


@dataclass(slots=True)
class IncidentResult:
    advice: str | None
    incident: Incident
    suppressed_reason: str | None = None
    system_prompt: str | None = None
    user_prompt: str | None = None


@dataclass(slots=True)
class _PendingJob:
    """One queued corner waiting on the LLM worker.

    ``corner_idx`` is preserved so the eventual ``AdvisorResult`` can be
    correlated with the corresponding entry in the session log even after
    the receive loop has moved on to later corners.
    """

    corner_idx: int
    trace: CornerTrace
    events: list[Event]
    winner: Event
    top: list[Event]
    now: float | None
    enqueued_at: float = field(default_factory=lambda: 0.0)


# ---- Advisor ----------------------------------------------------------------


class Advisor:
    """Owns the per-corner coaching policy + the LLM worker thread."""

    def __init__(
        self,
        provider: CoachProvider,
        voice: VoiceEngine,
        rate_limiter: RateLimiter,
        config: AdvisorConfig | None = None,
        on_result: ResultCallback | None = None,
    ) -> None:
        self.provider = provider
        self.voice = voice
        self.rate_limiter = rate_limiter
        self.config = config or AdvisorConfig()
        self.history: list[AdvisorResult] = []
        self.incident_history: list[IncidentResult] = []
        # Callback invoked AFTER an AdvisorResult is finalised — including
        # asynchronously from the worker thread. SessionLogger uses this to
        # write the actual LLM advice/prompt/response to coach.jsonl instead
        # of the synchronous "queued" stub.
        self._on_result = on_result
        # Session-level state surfaced to the prompt:
        self._best_lap_ms: int = -1
        self._recent_advice: deque[tuple[str, str]] = deque(maxlen=_RECENT_ADVICE_DEPTH)
        self._recent_events: deque[tuple[str, str]] = deque(maxlen=_RECENT_EVENTS_DEPTH)
        # Async worker bookkeeping:
        self._pending: _PendingJob | None = None
        self._pending_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._worker_idle = threading.Event()
        self._worker_idle.set()
        self._worker_thread: threading.Thread | None = None
        if self.config.async_mode:
            self._worker_thread = threading.Thread(
                target=self._worker_loop, name="gt7-coach-worker", daemon=True
            )
            self._worker_thread.start()

    # ---- session context updates ---------------------------------------

    def set_track_shape(self, shape: str) -> None:
        self.config.track_shape = shape

    def set_car_class(self, car_class: str) -> None:
        self.config.car_class = car_class

    # ---- public entrypoints --------------------------------------------

    def on_corner(
        self,
        trace: CornerTrace,
        events: Iterable[Event],
        *,
        now: float | None = None,
        corner_idx: int = 0,
    ) -> AdvisorResult:
        """Schedule (or run) coaching for one corner.

        ``corner_idx`` is threaded through to the on_result callback so the
        SessionLogger can correlate the async result with the right entry.
        """
        events_list = list(events)
        if not events_list:
            return self._record(None, None, "no events", corner_idx=corner_idx, trace=trace)

        winner = max(events_list, key=lambda e: e.severity)
        top = _top_events(events_list)

        # Quality (positive-feedback) events bypass the min_severity gate —
        # they intentionally use a fixed-low severity (~0.15) so they don't
        # outrank real corrective events, but we still want to compliment
        # the driver when they nail a corner.
        is_compliment = winner.type.startswith("quality.")
        if not is_compliment and winner.severity < self.config.min_severity:
            return self._record(
                None,
                winner,
                f"below-min-severity ({winner.severity:.2f})",
                corner_idx=corner_idx,
                trace=trace,
            )

        last_lap = trace.last_lap_ms
        if last_lap > 0 and (self._best_lap_ms < 0 or last_lap < self._best_lap_ms):
            self._best_lap_ms = last_lap

        if not self.rate_limiter.allow(winner.type, now=now):
            return self._record(None, winner, "rate-limited", corner_idx=corner_idx, trace=trace)

        if not self.voice.is_idle():
            return self._record(None, winner, "voice-busy", corner_idx=corner_idx, trace=trace)

        job = _PendingJob(
            corner_idx=corner_idx,
            trace=trace,
            events=events_list,
            winner=winner,
            top=top,
            now=now,
        )
        if self._worker_thread is None:
            return self._process_job(job)
        with self._pending_lock:
            replaced = self._pending is not None
            self._pending = job
        if replaced:
            log.info("worker still busy; replacing pending corner with newer one")
        self._worker_idle.clear()
        self._wake.set()
        # Synchronous stub for the caller. The worker will fire the on_result
        # callback when it actually finishes; that's the result that gets
        # logged with the prompt + response.
        stub = AdvisorResult(advice=None, chosen_event=winner, suppressed_reason="queued")
        self.history.append(stub)
        return stub

    def on_incident(self, incident: Incident) -> IncidentResult:
        """Speak a sarcastic remark for a spin / crash. Synchronous on purpose."""
        user_prompt = f"The driver just had a {incident.type}. Roast it in one short line."
        try:
            advice = self.provider.complete(SARCASTIC_SYSTEM_PROMPT, user_prompt, max_tokens=64)
            failure_reason: str | None = None
        except ProviderError as exc:
            log.warning("provider failed on incident: %s — falling back", exc)
            advice = ""
            failure_reason = f"provider-error: {exc}"

        advice = (advice or "").strip().strip("\"'`")
        if advice and len(advice.split()) < 2:
            failure_reason = f"too-short-response: {advice!r}"
            advice = ""
        if not advice:
            advice = incident_fallback_phrase(incident.type)
            failure_reason = f"{failure_reason or 'empty-response'}; spoke fallback"
        if not advice:
            return self._record_incident(
                None,
                incident,
                failure_reason or "no-fallback",
                system_prompt=SARCASTIC_SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )

        self.voice.interrupt(advice)
        self._recent_advice.append((f"incident.{incident.type}", advice))
        return self._record_incident(
            advice,
            incident,
            failure_reason,
            system_prompt=SARCASTIC_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

    # ---- shutdown ------------------------------------------------------

    def flush(self, *, timeout: float = _WORKER_JOIN_TIMEOUT_S) -> None:
        """Block until the worker has finished any queued corner."""
        if self._worker_thread is None:
            return
        self._worker_idle.wait(timeout=timeout)

    def stop(self) -> None:
        """Signal the worker to exit; join with a hard timeout."""
        self._stop_evt.set()
        self._wake.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=_WORKER_JOIN_TIMEOUT_S)
            self._worker_thread = None

    # ---- worker --------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop_evt.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            while not self._stop_evt.is_set():
                with self._pending_lock:
                    job = self._pending
                    self._pending = None
                if job is None:
                    break
                self._worker_idle.clear()
                try:
                    self._process_job(job)
                except Exception as exc:  # pragma: no cover — defensive
                    log.exception("worker exception: %s", exc)
            self._worker_idle.set()

    def _process_job(self, job: _PendingJob) -> AdvisorResult:
        """Synchronously run one corner's coaching pipeline."""
        ctx = CornerContext.from_trace(
            job.trace,
            best_lap_ms=self._best_lap_ms,
            car_class=self.config.car_class,
            track_shape=self.config.track_shape,
        )
        user_prompt = build_user_prompt(
            job.top,
            ctx,
            self.config.driver_style,
            recent_advice=list(self._recent_advice),
            recent_events=list(self._recent_events),
        )
        # Route to the compliment system prompt for positive-feedback events.
        is_compliment = job.winner.type.startswith("quality.")
        system_prompt = COMPLIMENT_SYSTEM_PROMPT if is_compliment else SYSTEM_PROMPT

        try:
            advice = self.provider.complete(system_prompt, user_prompt)
            failure_reason: str | None = None
        except ProviderError as exc:
            log.warning("provider failed: %s — falling back to canned phrase", exc)
            advice = ""
            failure_reason = f"provider-error: {exc}"

        advice = (advice or "").strip()
        if advice and len(advice.split()) < 2:
            log.warning("provider returned single-word response %r; using fallback", advice)
            failure_reason = f"too-short-response: {advice!r}"
            advice = ""
        if not advice:
            if is_compliment:
                advice = random.choice(_COMPLIMENT_FALLBACKS)
            else:
                advice = fallback_phrase(job.winner.type)
            if not advice:
                return self._record(
                    None,
                    job.winner,
                    failure_reason or "empty-response",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    corner_idx=job.corner_idx,
                    trace=job.trace,
                )
            failure_reason = f"{failure_reason or 'empty-response'}; spoke fallback"

        self.rate_limiter.record(job.winner.type, now=job.now)
        self.voice.speak(advice)
        self._recent_advice.append((job.winner.type, advice))
        self._recent_events.append((job.trace.corner_type, job.winner.type))
        return self._record(
            advice,
            job.winner,
            failure_reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            corner_idx=job.corner_idx,
            trace=job.trace,
        )

    # ---- result bookkeeping --------------------------------------------

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
        if self._on_result is not None:
            try:
                # Incidents aren't tied to a corner_idx; use 0 as a sentinel
                # and pass None for the trace.
                self._on_result(0, None, result)
            except Exception as exc:  # pragma: no cover
                log.exception("on_result callback raised: %s", exc)
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
        corner_idx: int = 0,
        trace: CornerTrace | None = None,
    ) -> AdvisorResult:
        result = AdvisorResult(
            advice=advice,
            chosen_event=chosen_event,
            suppressed_reason=reason,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        self.history.append(result)
        if self._on_result is not None:
            try:
                self._on_result(corner_idx, trace, result)
            except Exception as exc:  # pragma: no cover — observer shouldn't break the worker
                log.exception("on_result callback raised: %s", exc)
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
