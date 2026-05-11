"""Positive-feedback "detectors" — fire when the driver did something right.

These are the inverse of the corrective detectors. They run on the SAME
:class:`CornerTrace` objects but tag corners where nothing went wrong as
``quality.clean_corner`` events. The :class:`Advisor` routes
``quality.*`` events down a separate prompt path that asks the LLM for
a brief compliment instead of a correction.

The point: a driver who nails a fast sweeper deserves to hear about it,
not silence. Spec section 12 has this in the "future" bucket; here it is.
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import G_MS2, Event
from gt7coach.detectors.corner import CornerTrace


@dataclass(slots=True)
class CleanCornerConfig:
    """Tunable thresholds for the clean-corner detector."""

    # Only flag a corner as "clean" if it represented a real effort.
    min_peak_lat_g: float = 1.2  # below this it's probably just a kink, not coaching-worthy

    # Throttle hold: a clean corner means the driver kept the throttle on
    # (didn't lift in panic). We check that throttle never dropped below
    # this for more than a brief blip during the *cornering* portion.
    min_mid_corner_throttle: int = 50
    max_throttle_dip_fraction: float = 0.10  # at most 10% of frames below the throttle floor


def detect_clean_corner(
    trace: CornerTrace,
    *,
    config: CleanCornerConfig | None = None,
    other_events: list[Event] | None = None,
) -> list[Event]:
    """Fire ``quality.clean_corner`` when the driver nailed it.

    Args:
        trace: the corner trace.
        config: thresholds.
        other_events: events already fired by other detectors for this corner.
            If any are present, we stay silent — the corner isn't clean.
    """
    cfg = config or CleanCornerConfig()
    if other_events:
        return []
    if trace.peak_lat_g < cfg.min_peak_lat_g:
        return []

    # Throttle-hold check: the driver should have stayed on the gas through
    # the loaded section. We measure across the WHOLE corner; brief dips for
    # gear changes are fine, sustained lifts are not.
    g_threshold = 0.7 * G_MS2  # m/s² — only check frames that were properly loaded
    loaded_packets = [p for p in trace.packets if abs(p.accel_lat or 0.0) >= g_threshold]
    if not loaded_packets:
        return []
    dipped = sum(1 for p in loaded_packets if p.throttle < cfg.min_mid_corner_throttle)
    dip_fraction = dipped / len(loaded_packets)
    if dip_fraction > cfg.max_throttle_dip_fraction:
        return []

    # Severity is fixed-low so the advisor knows this is a compliment, not
    # a correction. Real (corrective) events use 0.3+ as their min-severity
    # gate; clean_corner explicitly bypasses that gate in the Advisor.
    return [
        Event(
            type="quality.clean_corner",
            severity=0.15,
            t_offset=0.0,
            evidence={
                "peak_lat_g": round(trace.peak_lat_g, 2),
                "min_speed_kmh": round(trace.min_speed_kmh, 1),
                "corner_type": trace.corner_type,
                "duration_s": round(trace.duration_s, 2),
            },
        )
    ]
