"""Braking detectors.

Ships ``late_brake`` and ``lockup``. ``trail_off_too_fast`` remains Phase 4.
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import Event
from gt7coach.detectors.corner import CornerTrace


@dataclass(slots=True)
class LateBrakeConfig:
    """Tunable thresholds for the late-brake detector."""

    min_peak_brake: int = 100  # ignore corners that weren't really braked into
    min_steer_rad: float = 0.10  # what counts as "turn-in has started"
    threshold_offset_s: float = 0.30  # how late is "late" — spec-derived
    full_severity_offset_s: float = 1.00  # offset that scores severity = 1.0


def detect_late_brake(trace: CornerTrace, *, config: LateBrakeConfig | None = None) -> list[Event]:
    """Peak brake force occurs *after* turn-in begins.

    Implementation: find the moment the driver started actually steering
    (|steer_angle| > min_steer_rad), then find the moment of peak brake
    pressure. If the brake peak is more than ``threshold_offset_s`` after
    turn-in, fire one event. Severity scales linearly from 0 at the
    threshold to 1 at ``full_severity_offset_s``.

    Returns at most one event per corner.
    """
    cfg = config or LateBrakeConfig()
    packets = trace.packets
    if len(packets) < 2:
        return []

    brakes = [p.brake for p in packets]
    peak_brake = max(brakes)
    if peak_brake < cfg.min_peak_brake:
        return []
    peak_idx = brakes.index(peak_brake)

    first_steer_idx = next(
        (
            i
            for i, p in enumerate(packets)
            if p.steer_angle is not None and abs(p.steer_angle) > cfg.min_steer_rad
        ),
        None,
    )
    if first_steer_idx is None:
        return []  # no measurable steering input — can't decide

    offset = packets[peak_idx].recv_time - packets[first_steer_idx].recv_time
    if offset <= cfg.threshold_offset_s:
        return []

    span = max(cfg.full_severity_offset_s - cfg.threshold_offset_s, 1e-6)
    severity = min(1.0, (offset - cfg.threshold_offset_s) / span)

    return [
        Event(
            type="braking.late_brake",
            severity=severity,
            t_offset=packets[peak_idx].recv_time - trace.start_time,
            evidence={
                "peak_brake": int(peak_brake),
                "offset_after_turn_in_s": round(offset, 3),
                "first_steer_t_s": round(packets[first_steer_idx].recv_time - trace.start_time, 3),
            },
        )
    ]


@dataclass(slots=True)
class LockupConfig:
    """Tunable thresholds for the lockup detector.

    Per spec section 6: "wheel speed -> 0 while car speed > 30 km/h".
    """

    min_brake: int = 150  # only count frames where the driver is actually braking
    min_speed_kmh: float = 30.0  # below this, locking is normal (you're stopping)
    max_wheel_rps: float = 10.0  # absolute rad/s; the slowest wheel is "near zero"
    min_duration_s: float = 0.10  # avoid one-frame noise
    full_severity_duration_s: float = 0.50


def detect_lockup(trace: CornerTrace, *, config: LockupConfig | None = None) -> list[Event]:
    """At least one wheel rotates near zero while the car is still moving fast.

    Catches "stomp on the brakes and skate" lockups. The detector looks at the
    *slowest* wheel each frame so a single front-axle lock counts (which is
    typically what happens — front wheels see more weight transfer and lock
    first). Multiple events per corner are possible if the lockup is
    intermittent (ABS-style pulsing).
    """
    cfg = config or LockupConfig()
    events: list[Event] = []
    streak: list[tuple[int, float, float]] = []  # (index, slowest_rps, speed_kmh)

    def close_streak() -> None:
        nonlocal streak
        if not streak:
            return
        first_idx = streak[0][0]
        last_idx = streak[-1][0]
        duration = trace.packets[last_idx].recv_time - trace.packets[first_idx].recv_time
        if duration < cfg.min_duration_s:
            streak = []
            return
        peak_speed = max(s for _, _, s in streak)
        min_rps = min(r for _, r, _ in streak)
        span = max(cfg.full_severity_duration_s - cfg.min_duration_s, 1e-6)
        severity = min(1.0, (duration - cfg.min_duration_s) / span)
        events.append(
            Event(
                type="braking.lockup",
                severity=severity,
                t_offset=trace.packets[first_idx].recv_time - trace.start_time,
                evidence={
                    "duration_s": round(duration, 3),
                    "min_wheel_rps": round(min_rps, 2),
                    "peak_speed_kmh": round(peak_speed, 1),
                },
            )
        )
        streak = []

    for i, p in enumerate(trace.packets):
        if p.brake < cfg.min_brake or p.speed_kmh < cfg.min_speed_kmh:
            close_streak()
            continue
        slowest = min(
            abs(p.wheel_speed_fl),
            abs(p.wheel_speed_fr),
            abs(p.wheel_speed_rl),
            abs(p.wheel_speed_rr),
        )
        if slowest < cfg.max_wheel_rps:
            streak.append((i, slowest, p.speed_kmh))
        else:
            close_streak()

    close_streak()
    return events
