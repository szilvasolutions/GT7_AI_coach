"""VR voice-HUD alerts.

These alerts exist to replace the unreadable in-VR digital HUD with
spoken callouts. They are intentionally stateful per-frame (unlike the
corner-trace detectors in this package) because the underlying signals
— tire temp, fuel level, coolant temp, RPM, lap time — are continuous,
not per-corner.

Each ``_check_*`` returns 0 or 1 :class:`Event`. The detector applies
its own per-alert hysteresis so a temperature hovering at the threshold
doesn't yell every frame. The downstream :class:`RateLimiter` enforces
the global "max one advice per N seconds" rule on top.

The detector never calls the LLM — like every other module here it just
emits events. The advisor decides what to do with them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gt7coach.detectors.base import Event
from gt7coach.telemetry.packet import Packet

if TYPE_CHECKING:
    from gt7coach.config import VRAlertsConfig

log = logging.getLogger(__name__)


# Deterministic spoken phrases per event type. VR alerts deliberately do
# NOT go through the LLM — they must be reliable, instant, and free.
# Format strings consume ``event.evidence`` directly.
VR_ALERT_PHRASES: dict[str, str] = {
    "vr.tyre_hot": "{tyre} tire hot, {temp_c} degrees.",
    "vr.tyre_cold": "{tyre} tire cold, {temp_c} degrees.",
    "vr.low_fuel": "Low fuel. About {laps_remaining} laps left.",
    "vr.fuel_critical": "Fuel critical. Pit this lap.",
    "vr.oil_hot": "Oil hot, {temp_c} degrees.",
    "vr.water_hot": "Water hot, {temp_c} degrees.",
    "vr.shift_up": "Shift up.",
    "vr.self_delta_down": "Personal best!",
    "vr.self_delta_up": "Down {delta_s} seconds.",
}


def format_vr_phrase(event: Event) -> str | None:
    """Render the spoken text for a VR alert event, or ``None`` if the
    event type is not a VR alert. Evidence-key absence yields a benign
    fallback rather than a KeyError so an off-by-one in a detector can't
    crash the voice path."""
    template = VR_ALERT_PHRASES.get(event.type)
    if template is None:
        return None
    evidence = dict(event.evidence)
    # Pretty-print delta_ms as seconds for the self-delta phrase.
    if "delta_ms" in evidence:
        evidence["delta_s"] = f"{abs(int(evidence['delta_ms'])) / 1000:.1f}"
    try:
        return template.format(**evidence)
    except KeyError as exc:
        log.warning("vr alert %s missing evidence key %s", event.type, exc)
        return None


# Local cooldowns (seconds) — separate from the global rate limiter so we
# avoid spamming the global queue with duplicates that would just get
# dropped anyway. These are deliberately generous.
_LOCAL_COOLDOWN_S: dict[str, float] = {
    "vr.tyre_hot": 30.0,
    "vr.tyre_cold": 180.0,  # a warm-up message, not a running commentary
    "vr.low_fuel": 60.0,
    "vr.fuel_critical": 20.0,
    "vr.oil_hot": 45.0,
    "vr.water_hot": 45.0,
    "vr.shift_up": 0.0,  # gated by rev-light crossing, not cooldown
    "vr.self_delta_down": 0.0,  # gated by lap transitions
    "vr.self_delta_up": 0.0,
}


@dataclass
class VRAlertDetector:
    """Stateful per-packet detector for the VR voice-HUD layer."""

    cfg: VRAlertsConfig
    _last_fired: dict[str, float] = field(default_factory=dict)
    # Hottest tyre seen this session. The fixed 60 C floor is a racing-slick
    # number: a road car on sport tyres runs cooler by design, and one logged
    # Nordschleife race sat below 60 C for 38% of its distance and got nine
    # "tires cold" calls for temperatures that were simply normal for that
    # car. Learn what this car actually runs at and judge against that.
    _peak_tyre_c: float = 0.0

    # Fuel-burn tracking: learn average litres/lap from lap transitions.
    _fuel_burn_per_lap: float | None = None
    _fuel_at_lap_start: float | None = None
    _last_lap_count: int = -1

    # Self-delta tracking: only fires at lap transitions.
    _last_seen_lap_time_ms: int = -1

    # Shift-assist edge detection: fires only on the upward RPM crossing
    # of rev_light_min, never every frame above it.
    _was_below_shift_rpm: bool = True

    def feed(self, p: Packet) -> list[Event]:
        events: list[Event] = []
        if self.cfg.tyre_temp_enabled:
            ev = self._check_tyres(p)
            if ev is not None:
                events.append(ev)
        if self.cfg.fuel_enabled:
            ev = self._check_fuel(p)
            if ev is not None:
                events.append(ev)
        if self.cfg.coolant_enabled:
            for ev in self._check_coolant(p):
                events.append(ev)
        if self.cfg.shift_assist_enabled:
            ev = self._check_shift(p)
            if ev is not None:
                events.append(ev)
        if self.cfg.self_delta_enabled:
            ev = self._check_self_delta(p)
            if ev is not None:
                events.append(ev)
        return events

    # ---- per-alert checks -------------------------------------------------

    def _check_tyres(self, p: Packet) -> Event | None:
        temps = (p.tyre_temp_fl, p.tyre_temp_fr, p.tyre_temp_rl, p.tyre_temp_rr)
        labels = ("front left", "front right", "rear left", "rear right")
        hot = [
            (t, lbl) for t, lbl in zip(temps, labels, strict=True) if t >= self.cfg.tyre_temp_hot_c
        ]
        if hot and self._cooled_down("vr.tyre_hot", p.recv_time):
            t, lbl = max(hot, key=lambda x: x[0])
            self._last_fired["vr.tyre_hot"] = p.recv_time
            return Event(
                type="vr.tyre_hot",
                severity=min(1.0, (t - self.cfg.tyre_temp_hot_c) / 30.0 + 0.5),
                t_offset=0.0,
                evidence={"tyre": lbl, "temp_c": round(t, 1)},
            )
        # Judge "cold" against what this car's tyres actually run at, not
        # against a racing-slick constant. Until the car has shown a real
        # operating temperature, fall back to the configured floor.
        self._peak_tyre_c = max(self._peak_tyre_c, max(temps))
        cold_threshold = self.cfg.tyre_temp_cold_c
        if self._peak_tyre_c > 0.0:
            cold_threshold = min(cold_threshold, self._peak_tyre_c * 0.85)
        cold = [(t, lbl) for t, lbl in zip(temps, labels, strict=True) if t <= cold_threshold]
        if cold and self._cooled_down("vr.tyre_cold", p.recv_time):
            t, lbl = min(cold, key=lambda x: x[0])
            self._last_fired["vr.tyre_cold"] = p.recv_time
            return Event(
                type="vr.tyre_cold",
                severity=0.5,
                t_offset=0.0,
                evidence={"tyre": lbl, "temp_c": round(t, 1)},
            )
        return None

    def _check_fuel(self, p: Packet) -> Event | None:
        # Capacity 0 means the game hasn't populated the field yet (e.g.
        # menus, replays). Skip gracefully.
        if p.fuel_capacity <= 0 or p.fuel_level < 0:
            return None

        # Track burn-rate across lap transitions.
        if p.lap_count != self._last_lap_count:
            if self._fuel_at_lap_start is not None and p.lap_count > self._last_lap_count:
                burnt = self._fuel_at_lap_start - p.fuel_level
                # Reject silly readings (refuel, sensor glitch, < 0.01L burn).
                if 0.01 < burnt < p.fuel_capacity:
                    if self._fuel_burn_per_lap is None:
                        self._fuel_burn_per_lap = burnt
                    else:
                        # Exponential moving average (alpha 0.3) — adapts fast
                        # to a fuel-mixture change but ignores one-off spikes.
                        self._fuel_burn_per_lap = 0.7 * self._fuel_burn_per_lap + 0.3 * burnt
            self._fuel_at_lap_start = p.fuel_level
            self._last_lap_count = p.lap_count

        if self._fuel_burn_per_lap is None or self._fuel_burn_per_lap <= 0:
            return None

        laps_remaining = p.fuel_level / self._fuel_burn_per_lap
        if laps_remaining <= self.cfg.fuel_critical_laps_remaining and self._cooled_down(
            "vr.fuel_critical", p.recv_time
        ):
            self._last_fired["vr.fuel_critical"] = p.recv_time
            return Event(
                type="vr.fuel_critical",
                severity=1.0,
                t_offset=0.0,
                evidence={
                    "laps_remaining": round(laps_remaining, 1),
                    "fuel_l": round(p.fuel_level, 1),
                },
            )
        if laps_remaining <= self.cfg.fuel_low_laps_remaining and self._cooled_down(
            "vr.low_fuel", p.recv_time
        ):
            self._last_fired["vr.low_fuel"] = p.recv_time
            return Event(
                type="vr.low_fuel",
                severity=0.6,
                t_offset=0.0,
                evidence={
                    "laps_remaining": round(laps_remaining, 1),
                    "fuel_l": round(p.fuel_level, 1),
                },
            )
        return None

    def _check_coolant(self, p: Packet) -> list[Event]:
        events: list[Event] = []
        if p.oil_temp >= self.cfg.oil_hot_c and self._cooled_down("vr.oil_hot", p.recv_time):
            self._last_fired["vr.oil_hot"] = p.recv_time
            events.append(
                Event(
                    type="vr.oil_hot",
                    severity=min(1.0, (p.oil_temp - self.cfg.oil_hot_c) / 20.0 + 0.5),
                    t_offset=0.0,
                    evidence={"temp_c": round(p.oil_temp, 1)},
                )
            )
        if p.water_temp >= self.cfg.water_hot_c and self._cooled_down("vr.water_hot", p.recv_time):
            self._last_fired["vr.water_hot"] = p.recv_time
            events.append(
                Event(
                    type="vr.water_hot",
                    severity=min(1.0, (p.water_temp - self.cfg.water_hot_c) / 15.0 + 0.5),
                    t_offset=0.0,
                    evidence={"temp_c": round(p.water_temp, 1)},
                )
            )
        return events

    def _check_shift(self, p: Packet) -> Event | None:
        # No rev light info from the game → can't gate sensibly.
        if p.rev_light_min <= 0:
            return None
        above = p.rpm >= p.rev_light_min
        crossing_up = above and self._was_below_shift_rpm
        self._was_below_shift_rpm = not above
        if not crossing_up:
            return None
        return Event(
            type="vr.shift_up",
            severity=0.4,
            t_offset=0.0,
            evidence={"rpm": int(p.rpm), "threshold": int(p.rev_light_min)},
        )

    def _check_self_delta(self, p: Packet) -> Event | None:
        # Only fires once per lap (when lap_time_ms changes), comparing
        # the just-completed lap to the running best.
        if p.lap_time_ms <= 0 or p.lap_time_ms == self._last_seen_lap_time_ms:
            return None
        self._last_seen_lap_time_ms = p.lap_time_ms
        if p.best_lap_ms <= 0:
            return None
        delta_ms = p.lap_time_ms - p.best_lap_ms
        if abs(delta_ms) < self.cfg.self_delta_threshold_ms:
            return None
        event_type = "vr.self_delta_down" if delta_ms < 0 else "vr.self_delta_up"
        return Event(
            type=event_type,
            severity=min(1.0, abs(delta_ms) / 2000.0),
            t_offset=0.0,
            evidence={
                "last_lap_ms": p.lap_time_ms,
                "best_lap_ms": p.best_lap_ms,
                "delta_ms": delta_ms,
            },
        )

    # ---- internals --------------------------------------------------------

    def _cooled_down(self, event_type: str, now: float) -> bool:
        last = self._last_fired.get(event_type, -1e9)
        return (now - last) >= _LOCAL_COOLDOWN_S.get(event_type, 0.0)
