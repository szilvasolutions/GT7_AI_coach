"""Throttle detectors.

Currently ships only ``wheelspin``. Spec lists ``sawing`` and ``early_lift``
as future work (Phase 4).
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import Event
from gt7coach.detectors.corner import CornerTrace
from gt7coach.telemetry.packet import Packet


@dataclass(slots=True)
class WheelspinConfig:
    """Tunable thresholds for the wheelspin detector."""

    min_throttle: int = 100  # 0..255
    ratio_threshold: float = 1.10  # rear_rps / front_rps
    full_severity_ratio: float = 1.50  # ratio that scores severity = 1.0
    min_duration_s: float = 0.05  # discard single-frame noise


def _wheel_avgs(p: Packet) -> tuple[float, float]:
    front = (abs(p.wheel_speed_fl) + abs(p.wheel_speed_fr)) / 2
    rear = (abs(p.wheel_speed_rl) + abs(p.wheel_speed_rr)) / 2
    return front, rear


def detect_wheelspin(trace: CornerTrace, *, config: WheelspinConfig | None = None) -> list[Event]:
    """Rear axle spinning measurably faster than front axle under throttle.

    Spec wording is "rear wheel speed > car speed x 1.10 with throttle > 100".
    We compare rear_rps to front_rps instead: the front axle of a RWD car
    rolls true under power and so reads ground speed directly, which makes
    this formulation robust to unknown tyre radius (the legacy 0.33 m hack
    is bug §11.11). Mathematically equivalent for RWD cars under power.

    Emits one event per continuous streak of qualifying frames; coalesces
    sub-frame noise by requiring ``min_duration_s`` of sustained signal.
    """
    cfg = config or WheelspinConfig()
    events: list[Event] = []
    streak: list[tuple[int, float]] = []  # (index, ratio)

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
                type="throttle.wheelspin",
                severity=severity,
                t_offset=trace.packets[first_idx].recv_time - trace.start_time,
                evidence={
                    "peak_ratio": round(peak_ratio, 3),
                    "duration_s": round(duration, 3),
                    "peak_throttle": max(trace.packets[i].throttle for i, _ in streak),
                },
            )
        )
        streak = []

    for i, p in enumerate(trace.packets):
        if p.throttle < cfg.min_throttle:
            close_streak()
            continue
        front, rear = _wheel_avgs(p)
        if front < 5.0:  # near-stationary; ratio is meaningless
            continue
        ratio = rear / front
        if ratio > cfg.ratio_threshold:
            streak.append((i, ratio))
        else:
            close_streak()

    close_streak()
    return events
