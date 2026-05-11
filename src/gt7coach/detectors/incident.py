"""Incident detectors: spinouts, crashes, big impacts.

Incidents are different from corner events:

* They fire on a **single packet** (no corner segmentation required).
* They **interrupt** whatever the coach was about to say (a spin matters
  more than the late-brake observation from the previous turn).
* They are handed to a **separate prompt** that produces a one-line
  sarcastic remark, not a corrective coaching imperative.

The legacy V23 script had ``trigger_spin_roast()`` for this. We keep the
same vibe but also detect crashes (heavy impact G).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from gt7coach.telemetry.packet import Packet

# 1 g in m/s² (mirrors detectors.base.G_MS2 without the import-cycle risk).
_G = 9.80665


@dataclass(slots=True, frozen=True)
class Incident:
    """A driving incident detected outside the corner-event pipeline."""

    type: str  # "spin" | "crash"
    severity: float  # 0..1
    recv_time: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IncidentDetectorConfig:
    # Spin: car rotating much faster than any normal cornering would explain.
    spin_yaw_rate_rad_s: float = 2.5  # ~143°/s; well above any clean cornering
    spin_min_speed_kmh: float = 20.0  # below this you might be parking
    spin_min_duration_s: float = 0.15  # avoid catching one-frame noise

    # Crash: massive instantaneous G spike (impact).
    crash_g_threshold: float = 4.0  # ~40 m/s²; clean racing rarely exceeds 2.5 g
    crash_min_speed_kmh: float = 15.0

    # Cooldown so one event doesn't fire 20 times.
    cooldown_s: float = 10.0


class IncidentDetector:
    """Watches the live packet stream and emits :class:`Incident` objects.

    Usage::

        det = IncidentDetector()
        for pkt in stream:
            inc = det.feed(pkt)
            if inc is not None:
                # interrupt the coach, speak the sarcastic remark
                ...
    """

    def __init__(self, config: IncidentDetectorConfig | None = None) -> None:
        self.config = config or IncidentDetectorConfig()
        self._last_emit_t: float = -1e9
        self._spin_streak_started: float | None = None

    def feed(self, packet: Packet) -> Incident | None:
        # Respect cooldown so we don't roast the driver six times for one spin.
        if (packet.recv_time - self._last_emit_t) < self.config.cooldown_s:
            self._spin_streak_started = None
            return None

        # ---- crash check (single-frame G spike) -------------------------
        if packet.speed_kmh > self.config.crash_min_speed_kmh:
            lon = abs(packet.accel_long or 0.0) / _G
            lat = abs(packet.accel_lat or 0.0) / _G
            total_g = math.sqrt(lon * lon + lat * lat)
            if total_g >= self.config.crash_g_threshold:
                return self._emit(
                    Incident(
                        type="crash",
                        severity=min(1.0, total_g / 8.0),
                        recv_time=packet.recv_time,
                        evidence={
                            "peak_g": round(total_g, 2),
                            "speed_kmh": round(packet.speed_kmh, 1),
                        },
                    )
                )

        # ---- spin check (sustained high yaw rate at speed) --------------
        if (
            abs(packet.yaw_rate) >= self.config.spin_yaw_rate_rad_s
            and packet.speed_kmh >= self.config.spin_min_speed_kmh
        ):
            if self._spin_streak_started is None:
                self._spin_streak_started = packet.recv_time
            elif (packet.recv_time - self._spin_streak_started) >= self.config.spin_min_duration_s:
                inc = Incident(
                    type="spin",
                    severity=min(1.0, abs(packet.yaw_rate) / 5.0),
                    recv_time=packet.recv_time,
                    evidence={
                        "peak_yaw_rate_rad_s": round(abs(packet.yaw_rate), 2),
                        "speed_kmh": round(packet.speed_kmh, 1),
                    },
                )
                self._spin_streak_started = None
                return self._emit(inc)
        else:
            self._spin_streak_started = None

        return None

    # ---- internals ------------------------------------------------------

    def _emit(self, incident: Incident) -> Incident:
        self._last_emit_t = incident.recv_time
        self._spin_streak_started = None
        return incident
