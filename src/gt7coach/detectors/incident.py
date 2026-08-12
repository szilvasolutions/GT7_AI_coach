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
    # Spin (rotation): car rotating much faster than any normal cornering
    # would explain.
    spin_yaw_rate_rad_s: float = 2.5  # ~143°/s; well above any clean cornering
    spin_min_speed_kmh: float = 20.0  # below this you might be parking
    spin_min_duration_s: float = 0.08  # 0.15 missed brief 180° spins

    # Slide-out: the car is sliding sideways without much rotation. The
    # wheels spin (rear/front speed ratio shoots up) and the car decelerates
    # hard but the yaw rate doesn't break the rotational threshold. Catches
    # the "off-track, all four wheels skating" scenario.
    slide_ratio_threshold: float = 1.6  # rear_wheel_rps / front_wheel_rps
    slide_min_speed_kmh: float = 25.0  # ignore at parking speeds
    slide_max_speed_kmh: float = 90.0  # high-speed wheelspin during exit is NOT a slide
    slide_min_speed_drop_kmh: float = 30.0  # speed must have dropped this much in the last window
    slide_drop_window_s: float = 1.0

    # Crash: massive instantaneous G spike (impact).
    crash_g_threshold: float = 4.0
    crash_min_speed_kmh: float = 15.0
    # ...but GT7's accel_long channel does NOT spike on contact. In a logged
    # wall hit the car went 32.1 -> 0.5 km/h in a single 60 Hz tick (~45 g of
    # real deceleration) while accel_long still read -1.13 g, so the check
    # above never fired and the driver got no acknowledgement at all. The
    # speed channel is the one that shows an impact: derive the deceleration
    # from consecutive packets instead.
    impact_decel_g: float = 8.0  # hard braking peaks near 1.5 g, so this is ~5x clear
    impact_max_dt_s: float = 0.12  # ignore gaps in the stream; only trust adjacent frames
    impact_min_dt_s: float = 0.005  # sub-ms gaps turn a 1 km/h wobble into 100 g
    # An impact sheds real speed. The logged wall hit lost 31.6 km/h; the
    # hardest legitimate braking sheds ~6 km/h across impact_max_dt_s. Two
    # false positives in the same session were 2.1 and 0.9 km/h drops that
    # only looked violent because the packets were microseconds apart.
    impact_min_drop_kmh: float = 15.0

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
        # Small ring buffer of (recv_time, speed_kmh) for the slide detector.
        # Sized to cover slide_drop_window_s at 60 Hz with some slack.
        from collections import deque

        self._speed_history: deque[tuple[float, float]] = deque(maxlen=80)

    def feed(self, packet: Packet) -> Incident | None:
        # Maintain speed history regardless of cooldown — needed for the
        # slide trigger and cheap to keep current.
        prev = self._speed_history[-1] if self._speed_history else None
        self._speed_history.append((packet.recv_time, packet.speed_kmh))

        # Respect cooldown so we don't roast the driver six times for one spin.
        if (packet.recv_time - self._last_emit_t) < self.config.cooldown_s:
            self._spin_streak_started = None
            return None

        # ---- crash check (speed collapse between two frames) ------------
        # Runs first: an impact that bleeds all the speed leaves the car
        # below crash_min_speed_kmh, so the G-spike branch below would skip
        # it on the very frame it happened.
        if prev is not None:
            dt = packet.recv_time - prev[0]
            lost_kmh = prev[1] - packet.speed_kmh
            if (
                self.config.impact_min_dt_s <= dt <= self.config.impact_max_dt_s
                and prev[1] > self.config.crash_min_speed_kmh
                and lost_kmh >= self.config.impact_min_drop_kmh
                and (lost_kmh / 3.6) / dt / _G >= self.config.impact_decel_g
            ):
                return self._emit(
                    Incident(
                        type="crash",
                        severity=min(1.0, ((lost_kmh / 3.6) / dt / _G) / 8.0),
                        recv_time=packet.recv_time,
                        evidence={
                            "decel_g": round((lost_kmh / 3.6) / dt / _G, 1),
                            "speed_lost_kmh": round(lost_kmh, 1),
                            "speed_kmh": round(prev[1], 1),
                        },
                    )
                )

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

        # ---- slide check (wheels skating, speed dumping) ----------------
        # Trigger criteria (must all be true):
        #   - rear-axle wheel speed >> front-axle (the "wheels are spinning
        #     not gripping" signature -- ratio > 1.6, well above wheelspin)
        #   - current speed is low-to-mid (high-speed wheelspin under
        #     full-throttle exit is NOT a slide-out)
        #   - speed dropped a lot recently (driver was going much faster
        #     than they are now -- the slide cost them)
        cfg = self.config
        if (
            cfg.slide_min_speed_kmh <= packet.speed_kmh <= cfg.slide_max_speed_kmh
            and self._speed_history
        ):
            front_avg = (abs(packet.wheel_speed_fl) + abs(packet.wheel_speed_fr)) / 2.0
            rear_avg = (abs(packet.wheel_speed_rl) + abs(packet.wheel_speed_rr)) / 2.0
            if front_avg > 5.0 and rear_avg / front_avg >= cfg.slide_ratio_threshold:
                speed_drop = self._speed_drop_in_window(packet.recv_time, cfg.slide_drop_window_s)
                if speed_drop >= cfg.slide_min_speed_drop_kmh:
                    ratio = rear_avg / front_avg
                    return self._emit(
                        Incident(
                            type="slide",
                            severity=min(1.0, (ratio - 1.0) / 1.5),
                            recv_time=packet.recv_time,
                            evidence={
                                "peak_ratio": round(ratio, 2),
                                "speed_kmh": round(packet.speed_kmh, 1),
                                "speed_drop_kmh": round(speed_drop, 1),
                            },
                        )
                    )

        return None

    def _speed_drop_in_window(self, now: float, window_s: float) -> float:
        """Return max(speed) - current_speed within the last ``window_s``."""
        if not self._speed_history:
            return 0.0
        cutoff = now - window_s
        max_speed = 0.0
        current = self._speed_history[-1][1]
        for t, s in self._speed_history:
            if t < cutoff:
                continue
            if s > max_speed:
                max_speed = s
        return max(0.0, max_speed - current)

    # ---- internals ------------------------------------------------------

    def _emit(self, incident: Incident) -> Incident:
        self._last_emit_t = incident.recv_time
        self._spin_streak_started = None
        return incident
