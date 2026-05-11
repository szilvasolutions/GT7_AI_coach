"""Racing-line detectors.

Ships ``late_apex``. The legacy V62 had no line detector at all; this is new.
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import Event
from gt7coach.detectors.corner import CornerTrace


@dataclass(slots=True)
class LateApexConfig:
    """Tunable thresholds for the late-apex detector."""

    min_steer_rad: float = 0.30  # the corner must actually be steered
    min_offset_s: float = 0.30  # how much later than "ideal" before we complain
    full_severity_offset_s: float = 1.50


def detect_late_apex(trace: CornerTrace, *, config: LateApexConfig | None = None) -> list[Event]:
    """Min speed occurs measurably after the geometric apex.

    A textbook racing line peaks the steering input and hits minimum speed
    at roughly the same moment (the apex). When the min-speed frame lands
    well after max steering, the driver carried too much speed past the apex
    and is dragging brakes / scrubbing speed on the wrong side of the corner.

    We use ``argmax(|steer_angle|)`` as a proxy for the geometric apex
    because we don't have track-edge geometry to compute a true apex.
    """
    cfg = config or LateApexConfig()
    packets = trace.packets
    if len(packets) < 4:
        return []

    steerings = [abs(p.steer_angle or 0.0) for p in packets]
    peak_steer = max(steerings)
    if peak_steer < cfg.min_steer_rad:
        return []
    apex_idx = steerings.index(peak_steer)

    min_speed = min(p.speed_kmh for p in packets)
    min_speed_idx = next(i for i, p in enumerate(packets) if p.speed_kmh == min_speed)

    offset = packets[min_speed_idx].recv_time - packets[apex_idx].recv_time
    if offset <= cfg.min_offset_s:
        return []

    span = max(cfg.full_severity_offset_s - cfg.min_offset_s, 1e-6)
    severity = min(1.0, (offset - cfg.min_offset_s) / span)

    return [
        Event(
            type="line.late_apex",
            severity=severity,
            t_offset=packets[min_speed_idx].recv_time - trace.start_time,
            evidence={
                "offset_after_apex_s": round(offset, 3),
                "min_speed_kmh": round(min_speed, 1),
                "peak_steer_rad": round(peak_steer, 3),
            },
        )
    ]
