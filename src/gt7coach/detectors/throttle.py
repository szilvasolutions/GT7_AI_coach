"""Throttle detectors.

Ships ``wheelspin``, ``sawing``, and ``early_lift``.
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


@dataclass(slots=True)
class SawingConfig:
    """Tunable thresholds for the throttle-sawing detector."""

    window_s: float = 1.5
    min_changes: int = 4  # spec: "throttle direction changes >= 4 times in 1.5s"
    min_delta: int = 15  # ignore micro-jitters smaller than this in the 0..255 input
    full_severity_changes: int = 8


def detect_sawing(trace: CornerTrace, *, config: SawingConfig | None = None) -> list[Event]:
    """Driver fanning the throttle pedal mid-corner.

    A "throttle direction change" is when the sign of ``throttle[i] -
    throttle[i-1]`` flips (with a small dead-band to ignore micro-jitter).
    The detector slides a 1.5 s window across the trace and fires once for
    the window with the most changes if it crosses ``min_changes``.
    """
    cfg = config or SawingConfig()
    packets = trace.packets
    if len(packets) < 4:
        return []

    # Precompute sign of each throttle change (or 0 if the delta is below the
    # dead-band so we don't count fan-out noise).
    signs: list[int] = [0]
    for i in range(1, len(packets)):
        delta = packets[i].throttle - packets[i - 1].throttle
        if delta > cfg.min_delta:
            signs.append(1)
        elif delta < -cfg.min_delta:
            signs.append(-1)
        else:
            signs.append(0)

    best_count = 0
    best_window_start: int | None = None
    best_window_end: int | None = None
    left = 0
    for right in range(1, len(packets)):
        while packets[right].recv_time - packets[left].recv_time > cfg.window_s:
            left += 1
        # Count sign flips between non-zero signs inside [left+1, right].
        changes = 0
        last_nonzero = 0
        for i in range(left + 1, right + 1):
            if signs[i] == 0:
                continue
            if last_nonzero != 0 and signs[i] != last_nonzero:
                changes += 1
            last_nonzero = signs[i]
        if changes > best_count:
            best_count = changes
            best_window_start = left
            best_window_end = right

    if best_count < cfg.min_changes or best_window_start is None or best_window_end is None:
        return []

    span = max(cfg.full_severity_changes - cfg.min_changes, 1)
    severity = min(1.0, (best_count - cfg.min_changes) / span)

    return [
        Event(
            type="throttle.sawing",
            severity=severity,
            t_offset=packets[best_window_start].recv_time - trace.start_time,
            evidence={
                "changes": best_count,
                "window_s": round(
                    packets[best_window_end].recv_time - packets[best_window_start].recv_time, 3
                ),
            },
        )
    ]


@dataclass(slots=True)
class EarlyLiftConfig:
    """Tunable thresholds for the early-lift detector."""

    max_throttle: int = 50  # spec: "throttle < 50"
    min_lat_g: float = 1.00  # spec: "G-load still > 1.0 mid-corner"
    min_duration_s: float = 0.25
    full_severity_duration_s: float = 1.00


def detect_early_lift(trace: CornerTrace, *, config: EarlyLiftConfig | None = None) -> list[Event]:
    """Driver lifted off throttle while still under lateral load.

    Common timid-driver mistake: they get to the apex, see the car loaded
    up, panic, and lift. Without the longitudinal traction the car was
    counting on, the rear gets light and the corner gets slower. Fires
    when throttle stays below 50 (out of 255) for >= 0.25 s while lat_g
    is still above 1.0 g.
    """
    cfg = config or EarlyLiftConfig()
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
        peak_lat = max(lat for _, lat in streak)
        span = max(cfg.full_severity_duration_s - cfg.min_duration_s, 1e-6)
        severity = min(1.0, (duration - cfg.min_duration_s) / span)
        events.append(
            Event(
                type="throttle.early_lift",
                severity=severity,
                t_offset=trace.packets[first_idx].recv_time - trace.start_time,
                evidence={
                    "duration_s": round(duration, 3),
                    "peak_lat_g": round(peak_lat, 2),
                    "min_throttle": min(trace.packets[i].throttle for i, _ in streak),
                },
            )
        )
        streak = []

    for i, p in enumerate(trace.packets):
        lat_g = abs(p.accel_lat or 0.0) / 9.80665
        if lat_g < cfg.min_lat_g:
            close_streak()
            continue
        if p.throttle < cfg.max_throttle:
            streak.append((i, lat_g))
        else:
            close_streak()

    close_streak()
    return events
