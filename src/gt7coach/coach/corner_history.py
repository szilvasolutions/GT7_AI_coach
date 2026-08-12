"""Per-corner history — "you have driven this corner better before".

This is the *consistency* half of the coach's reference. Grip utilisation says
whether a corner was near the car's limit; this says whether it was near the
driver's own best through that same corner. For a beginner the personal best is
a ceiling rather than a target, so it never sets the goal — but "you carried
8 km/h more through here two laps ago" is concrete, is obviously achievable
because they have already done it, and needs no reference driver.

Deliberately keyed on the track's turn index rather than on lap distance.
Distance accumulation is fragile in exactly the situations that matter: a
respawn teleports the car (a 400 m sector "took" 0.01 s in one logged session)
and the packet rate collapses in menus. Minimum and exit speed through an
identified turn need no alignment at all.

**NOT WIRED INTO COACHING YET, and the reason is not this module.** Corner
identity is currently unreliable: the segmenter emits 19 corners per two laps
against a track with 11 turns, so several distinct corners collapse onto one
turn index. On the reference session turn 5 collected four passes — two of them
from the same lap, a medium corner at 138 km/h and a slow corner at 86 km/h —
and comparing those produced a confident, meaningless "52 km/h slower through
here". Three turn indices had same-lap collisions.

The blocker was expected to be teleport rejection; that is solved and tested
here. The real blocker is one-segment-per-turn segmentation. Until the
segmenter stops merging a hairpin with its approach and splitting single
corners in two, this compares different corners with each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

from gt7coach.detectors import CornerTrace

# Corners are identified by where they sit on the track's polyline, quantised
# into buckets, rather than by the nearest entry in the track database's turn
# list. That list is incomplete: Deep Forest declares 18 corners and lists 11,
# leaving a 122-point gap across a third of the lap, so every corner driven in
# that stretch mapped onto the same turn index. One index collected ten passes
# and comparisons became meaningless. A bucket is stable per location whether
# or not the database happens to name that corner.
_LOCATION_BUCKET_POINTS = 12

# A corner whose trace contains a teleport or a stall is not a real sample.
# Position cannot jump this far between two 60 Hz frames under any legitimate
# driving — 30 m at 60 Hz implies 6480 km/h.
_MAX_FRAME_JUMP_M = 30.0


@dataclass
class CornerBest:
    """Best pass through one identified turn."""

    min_speed_kmh: float
    exit_speed_kmh: float
    lap: int


@dataclass
class CornerDelta:
    """How this pass compares with the driver's best through the same turn."""

    turn_index: int
    min_speed_delta_kmh: float  # positive = faster than their best
    exit_speed_delta_kmh: float
    best_lap: int

    @property
    def is_meaningful(self) -> bool:
        """Ignore noise: corner boundaries wander by a metre or two."""
        return abs(self.min_speed_delta_kmh) >= 3.0 or abs(self.exit_speed_delta_kmh) >= 3.0

    def describe(self) -> str | None:
        if not self.is_meaningful:
            return None
        if self.min_speed_delta_kmh <= -3.0:
            return (
                f"{abs(self.min_speed_delta_kmh):.0f} km/h slower through here "
                f"than your own best on lap {self.best_lap}"
            )
        if self.min_speed_delta_kmh >= 3.0:
            return f"{self.min_speed_delta_kmh:.0f} km/h quicker through here than before"
        if self.exit_speed_delta_kmh <= -3.0:
            return f"exit {abs(self.exit_speed_delta_kmh):.0f} km/h down on your best here"
        return f"exit {self.exit_speed_delta_kmh:.0f} km/h up on your best here"


def trace_is_clean(trace: CornerTrace) -> bool:
    """False if the car teleported or the stream stalled inside this corner.

    A respawn puts a discontinuity in the position trace, and a corner
    containing one is not a lap of driving — recording it as a personal best
    would poison the reference permanently.
    """
    packets = trace.packets
    for a, b in pairwise(packets):
        dx, dz = b.pos_x - a.pos_x, b.pos_z - a.pos_z
        if (dx * dx + dz * dz) ** 0.5 > _MAX_FRAME_JUMP_M:
            return False
        # Stopped dead mid-corner: an impact, not a lap. The bar has to sit
        # low — the logged wall hit went 32.1 -> 0.5 km/h, so a threshold of
        # 40 km/h sailed straight past the one crash in the session.
        if b.speed_kmh < 2.0 and a.speed_kmh > 15.0:
            return False
    return True


def location_key(track: object, trace: CornerTrace) -> int | None:
    """Stable identity for *where* this corner is, independent of the turn list.

    Uses the polyline index of the corner's slowest point — the apex is far
    more stable between laps than the segment boundaries, which move with
    entry speed and line.
    """
    if track is None:
        return None
    packets = trace.packets
    if not packets:
        return None
    apex = min(packets, key=lambda p: p.speed_kmh)
    try:
        idx, dist = track.nearest_polyline_index(apex.pos_x, apex.pos_z)  # type: ignore[attr-defined]
    except AttributeError:
        return None
    if dist > 60.0:  # nowhere near this track's line
        return None
    return idx // _LOCATION_BUCKET_POINTS


@dataclass
class CornerHistory:
    """Remembers the best pass through each identified turn this session."""

    _best: dict[int, CornerBest] = field(default_factory=dict)
    _last_key: int | None = None

    def compare_and_record(self, turn_index: int | None, trace: CornerTrace) -> CornerDelta | None:
        """Compare this pass with the stored best, then update it.

        Returns None the first time a turn is seen, when the turn could not be
        identified, or when the trace is not a clean sample.
        """
        if turn_index is None or not trace_is_clean(trace):
            self._last_key = None
            return None

        # Two segments arriving back-to-back at the same place are one corner
        # the segmenter split, not two passes: the reference session produced
        # pairs at an identical 86 and 93 km/h. Fold the second into the first
        # instead of comparing a corner against itself.
        continuation = turn_index == self._last_key
        self._last_key = turn_index

        current = CornerBest(
            min_speed_kmh=trace.min_speed_kmh,
            exit_speed_kmh=trace.exit_speed_kmh,
            lap=trace.lap_count,
        )
        previous = self._best.get(turn_index)
        if previous is None:
            self._best[turn_index] = current
            return None

        if continuation:
            if current.min_speed_kmh > previous.min_speed_kmh:
                self._best[turn_index] = current
            return None

        delta = CornerDelta(
            turn_index=turn_index,
            min_speed_delta_kmh=round(current.min_speed_kmh - previous.min_speed_kmh, 1),
            exit_speed_delta_kmh=round(current.exit_speed_kmh - previous.exit_speed_kmh, 1),
            best_lap=previous.lap,
        )
        # Keep whichever pass carried more speed through the corner.
        if current.min_speed_kmh > previous.min_speed_kmh:
            self._best[turn_index] = current
        return delta

    def known_turns(self) -> int:
        return len(self._best)
