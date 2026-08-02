"""Duration-aware cue timing: don't start a coaching line the driver can't
finish hearing comfortably before the next corner.

The Advisor speaks 2-3 s after corner exit (LLM + TTS latency). On short
straights that puts the tail of the utterance inside the next braking zone —
exactly when the driver has no spare attention. This module answers one
question: *if I start speaking N seconds of audio right now, will it finish
at least ``finish_margin_s`` before the next apex?*

Position comes from the same track polyline the :class:`TrackDetector`
matched; the next apex is the nearest :class:`~gt7coach.tracks.database.Turn`
ahead along the racing line, and arrival time is path distance divided by
current speed. Without a detected track (or at very low speed) there is no
constraint and callers speak immediately — pre-Phase-F behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gt7coach.telemetry import Packet
from gt7coach.tracks.database import Track

log = logging.getLogger(__name__)


@dataclass(slots=True)
class CueTimingConfig:
    enabled: bool = True
    # The cue must FINISH this many seconds before the upcoming apex. Apex is
    # our proxy for "the corner" — the braking zone starts a little earlier,
    # which this margin is sized to absorb.
    finish_margin_s: float = 2.5
    # If no speaking window opens within this long, speak anyway — on tight
    # esses the windows may simply never fit and silence helps nobody.
    max_hold_s: float = 6.0
    # Worker poll interval while holding a cue.
    poll_s: float = 0.2
    # Below this speed there is no meaningful "time to next corner" (grid,
    # pits, spin recovery) — treat as unconstrained.
    min_speed_kmh: float = 30.0
    # If the car is further than this from the matched polyline the position
    # estimate is junk (off-track excursion) — treat as unconstrained.
    max_offline_m: float = 60.0


class CueScheduler:
    """Tracks the car's progress along the polyline and grants speech windows.

    ``feed()`` is called from the telemetry receive loop (O(1): it just
    stores the newest packet). ``seconds_to_next_turn()`` / ``clearance()``
    are called from the Advisor's worker thread; a single ``Packet`` reference
    swap is atomic under the GIL, so no lock is needed.
    """

    def __init__(self, config: CueTimingConfig | None = None) -> None:
        self.config = config or CueTimingConfig()
        self._track: Track | None = None
        self._packet: Packet | None = None
        # Cumulative arc length up to each polyline point, metres. Built once
        # per track so a window query is two lookups, not a polyline walk.
        self._cum_m: list[float] = []
        self._total_m: float = 0.0

    # ---- inputs ----------------------------------------------------------

    def set_track(self, track: Track) -> None:
        self._track = track
        cum = [0.0]
        xs, zs = track.polyline_x, track.polyline_z
        for i in range(1, len(xs)):
            dx = xs[i] - xs[i - 1]
            dz = zs[i] - zs[i - 1]
            cum.append(cum[-1] + (dx * dx + dz * dz) ** 0.5)
        # Closing segment (polyline is a loop).
        if len(xs) > 1:
            dx = xs[0] - xs[-1]
            dz = zs[0] - zs[-1]
            self._total_m = cum[-1] + (dx * dx + dz * dz) ** 0.5
        else:
            self._total_m = 0.0
        self._cum_m = cum
        log.info(
            "cue timing armed: %s (%d turns, %.0f m lap)",
            track.display_name,
            len(track.turns),
            self._total_m,
        )

    def feed(self, packet: Packet) -> None:
        self._packet = packet

    # ---- queries ---------------------------------------------------------

    def seconds_to_next_turn(self) -> float | None:
        """Estimated seconds until the next apex, or ``None`` if unknown.

        ``None`` means "no constraint": no track, no telemetry yet, crawling
        speed, or the car is nowhere near the racing line.
        """
        track, packet = self._track, self._packet
        if track is None or packet is None or not track.turns or not self._cum_m:
            return None
        if packet.speed_kmh < self.config.min_speed_kmh:
            return None
        poly_idx, offline_m = track.nearest_polyline_index(packet.pos_x, packet.pos_z)
        if offline_m > self.config.max_offline_m:
            return None
        dist_m = self._distance_to_next_turn_m(track, poly_idx)
        if dist_m is None:
            return None
        return dist_m / packet.speed_mps

    def clearance(self, duration_s: float) -> bool:
        """True if ``duration_s`` of audio can finish ``finish_margin_s``
        before the next apex — or if there is no constraint at all."""
        if not self.config.enabled:
            return True
        window = self.seconds_to_next_turn()
        if window is None:
            return True
        return window >= duration_s + self.config.finish_margin_s

    # ---- internals -------------------------------------------------------

    def _distance_to_next_turn_m(self, track: Track, poly_idx: int) -> float | None:
        if poly_idx >= len(self._cum_m):
            return None  # stale cumulative table (track swapped mid-query)
        here = self._cum_m[poly_idx]
        best: float | None = None
        for turn in track.turns:
            if turn.index >= len(self._cum_m):
                continue
            ahead = self._cum_m[turn.index] - here
            if ahead <= 0:
                ahead += self._total_m  # behind us -> next lap's instance
            if best is None or ahead < best:
                best = ahead
        return best


__all__ = ["CueScheduler", "CueTimingConfig"]
