"""Steering detectors.

Ships ``understeer`` and ``oversteer``.
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import G_MS2, Event
from gt7coach.detectors.corner import CornerTrace
from gt7coach.telemetry.packet import Packet


def _wheel_avgs(p: Packet) -> tuple[float, float]:
    front = (abs(p.wheel_speed_fl) + abs(p.wheel_speed_fr)) / 2
    rear = (abs(p.wheel_speed_rl) + abs(p.wheel_speed_rr)) / 2
    return front, rear


@dataclass(slots=True)
class UndersteerConfig:
    """Tunable thresholds for the understeer detector."""

    min_lat_g: float = 0.70  # corner must actually be loaded
    ratio_threshold: float = 1.15  # front_rps / rear_rps  (spec: front_slip - rear_slip > 0.15)
    full_severity_ratio: float = 1.40
    min_duration_s: float = 0.30


def detect_understeer(trace: CornerTrace, *, config: UndersteerConfig | None = None) -> list[Event]:
    """Front wheels rotating noticeably faster than rears while loaded laterally.

    Front_rps > rear_rps x 1.15 means the fronts are slipping outwards across
    the contact patch — they're not gripping the corner. The mid-corner gate
    (|lat_g| > 0.7) prevents braking/exit-acceleration phases from
    contaminating the result.

    Tyre radius cancels out of the ratio, so this works without the per-car
    radius table that bug §11.11 says we shouldn't hardcode.
    """
    cfg = config or UndersteerConfig()
    events: list[Event] = []
    streak: list[tuple[int, float]] = []

    def close_streak() -> None:
        nonlocal streak
        if not streak:
            return
        first_idx, _ = streak[0]
        last_idx, _ = streak[-1]
        duration = trace.packets[last_idx].recv_time - trace.packets[first_idx].recv_time
        if duration < cfg.min_duration_s:
            streak = []
            return
        peak_ratio = max(r for _, r in streak)
        span = max(cfg.full_severity_ratio - cfg.ratio_threshold, 1e-6)
        severity = min(1.0, (peak_ratio - cfg.ratio_threshold) / span)
        events.append(
            Event(
                type="steering.understeer",
                severity=severity,
                t_offset=trace.packets[first_idx].recv_time - trace.start_time,
                evidence={
                    "peak_ratio": round(peak_ratio, 3),
                    "duration_s": round(duration, 3),
                    "peak_lat_g": round(
                        max(abs(trace.packets[i].accel_lat or 0.0) / G_MS2 for i, _ in streak),
                        2,
                    ),
                },
            )
        )
        streak = []

    for i, p in enumerate(trace.packets):
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        if lat_g < cfg.min_lat_g:
            close_streak()
            continue
        front, rear = _wheel_avgs(p)
        if rear < 5.0:
            continue
        ratio = front / rear
        if ratio > cfg.ratio_threshold:
            streak.append((i, ratio))
        else:
            close_streak()

    close_streak()
    return events


@dataclass(slots=True)
class OversteerConfig:
    """Tunable thresholds for the oversteer detector."""

    min_lat_g: float = 0.70
    ratio_threshold: float = 1.15  # rear_rps / front_rps  (spec: rear_slip - front_slip > 0.15)
    full_severity_ratio: float = 1.40
    min_duration_s: float = 0.30


def detect_oversteer(trace: CornerTrace, *, config: OversteerConfig | None = None) -> list[Event]:
    """Rear wheels rotating noticeably faster than fronts while loaded laterally.

    Mirror of :func:`detect_understeer`. When the rear axle outpaces the front
    under cornering load, the back end is sliding outwards (or being driven
    out by throttle on a RWD car) — that's oversteer. The wheelspin detector
    fires on the same telemetry pattern but gates on throttle; the oversteer
    detector gates on lateral load and is throttle-agnostic.
    """
    cfg = config or OversteerConfig()
    events: list[Event] = []
    streak: list[tuple[int, float]] = []

    def close_streak() -> None:
        nonlocal streak
        if not streak:
            return
        first_idx, _ = streak[0]
        last_idx, _ = streak[-1]
        duration = trace.packets[last_idx].recv_time - trace.packets[first_idx].recv_time
        if duration < cfg.min_duration_s:
            streak = []
            return
        peak_ratio = max(r for _, r in streak)
        span = max(cfg.full_severity_ratio - cfg.ratio_threshold, 1e-6)
        severity = min(1.0, (peak_ratio - cfg.ratio_threshold) / span)
        events.append(
            Event(
                type="steering.oversteer",
                severity=severity,
                t_offset=trace.packets[first_idx].recv_time - trace.start_time,
                evidence={
                    "peak_ratio": round(peak_ratio, 3),
                    "duration_s": round(duration, 3),
                    "peak_lat_g": round(
                        max(abs(trace.packets[i].accel_lat or 0.0) / G_MS2 for i, _ in streak),
                        2,
                    ),
                },
            )
        )
        streak = []

    for i, p in enumerate(trace.packets):
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        if lat_g < cfg.min_lat_g:
            close_streak()
            continue
        front, rear = _wheel_avgs(p)
        if front < 5.0:
            continue
        ratio = rear / front
        if ratio > cfg.ratio_threshold:
            streak.append((i, ratio))
        else:
            close_streak()

    close_streak()
    return events
