"""Braking detectors.

Currently ships only ``late_brake``. Spec section 6 lists ``lockup`` and
``trail_off_too_fast`` as future work (Phase 4).
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
