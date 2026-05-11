"""TrackDetector: matches packets to a known :class:`Track`.

Disambiguation is **sequence-based** because zetetos's polylines are
quantised to a 16 m grid, which means single-point matches are noisy —
e.g. a dozen unrelated circuits share the point (-48, -256) because each
has a straight section riding the world's z-axis. A short buffer of
recent positions is the cheapest way to disambiguate: the right track's
polyline tracks the buffer; wrong tracks diverge after a couple of
moves.

Algorithm:

1. **Bbox prefilter** — drop tracks whose bbox doesn't contain the latest
   ``(pos_x, pos_z)``. Usually 5-20 candidates remain.
2. **Sequence cost** — for each candidate, compute the *mean* nearest-
   polyline distance across the buffer of recent positions. Pick the
   candidate with the lowest mean, gated by:
     * the mean must be below ``max_distance_m``
     * it must beat the second-best by at least ``margin_m``
3. **Sticky lock** — once a track is chosen, keep it until the car
   leaves the bbox by > ``sticky_release_m`` for > ``sticky_release_s``
   seconds (covers garage / pit returns).
"""

from __future__ import annotations

import logging
from collections import deque

from gt7coach.telemetry.packet import Packet
from gt7coach.tracks.database import Track, load_default_tracks

log = logging.getLogger(__name__)

_BUFFER_LEN = 30
_MIN_BUFFER_FOR_MATCH = 10


class TrackDetector:
    """Returns the current :class:`Track` (or None) for each fed packet."""

    def __init__(
        self,
        tracks: dict[str, Track] | None = None,
        *,
        max_probes: int = 600,
        max_distance_m: float = 40.0,
        margin_m: float = 8.0,
        sticky_release_m: float = 150.0,
        sticky_release_s: float = 3.0,
    ) -> None:
        self._tracks = tracks if tracks is not None else load_default_tracks()
        self._max_probes = max_probes
        self._max_distance_m = max_distance_m
        self._margin_m = margin_m
        self._sticky_release_m = sticky_release_m
        self._sticky_release_s = sticky_release_s

        self._chosen: Track | None = None
        self._probes_seen: int = 0
        self._gave_up: bool = False
        self._outside_since: float | None = None
        self._positions: deque[tuple[float, float]] = deque(maxlen=_BUFFER_LEN)

    # ---- public ----------------------------------------------------------

    @property
    def track(self) -> Track | None:
        return self._chosen

    def feed(self, packet: Packet) -> Track | None:
        x = packet.pos_x
        z = packet.pos_z
        self._positions.append((x, z))

        if self._chosen is not None:
            if self._in_release_bbox(self._chosen, x, z):
                self._outside_since = None
                return self._chosen
            if self._outside_since is None:
                self._outside_since = packet.recv_time
            elif (packet.recv_time - self._outside_since) >= self._sticky_release_s:
                log.info(
                    "track %s released (>%.0f m outside bbox for >%.1f s)",
                    self._chosen.id,
                    self._sticky_release_m,
                    self._sticky_release_s,
                )
                self._chosen = None
                self._outside_since = None
                self._gave_up = False
                self._probes_seen = 0
                self._positions.clear()
                self._positions.append((x, z))
            return self._chosen

        if self._gave_up:
            return None
        self._probes_seen += 1

        if len(self._positions) < _MIN_BUFFER_FOR_MATCH:
            return None  # not enough motion data yet

        # Bbox prefilter against the *latest* position.
        candidates: list[Track] = [tr for tr in self._tracks.values() if tr.contains(x, z)]
        if not candidates:
            if self._probes_seen >= self._max_probes:
                log.warning(
                    "no track matched after %d probes (pos last=(%.0f, %.0f)); "
                    "running without track context",
                    self._probes_seen,
                    x,
                    z,
                )
                self._gave_up = True
            return None

        # Sequence cost: mean nearest-polyline distance across the buffer.
        ranked: list[tuple[float, Track]] = []
        for tr in candidates:
            total = 0.0
            for px, pz in self._positions:
                _, dist = tr.nearest_polyline_index(px, pz)
                total += dist
            mean = total / len(self._positions)
            ranked.append((mean, tr))
        ranked.sort(key=lambda item: item[0])
        best_mean, best_track = ranked[0]
        if best_mean > self._max_distance_m:
            if self._probes_seen >= self._max_probes:
                log.warning(
                    "no track polyline within %.0f m after %d probes; giving up",
                    self._max_distance_m,
                    self._probes_seen,
                )
                self._gave_up = True
            return None
        if len(ranked) > 1 and (ranked[1][0] - best_mean) < self._margin_m:
            return None  # still ambiguous; keep buffering

        self._chosen = best_track
        log.info(
            "track detected: %s (id=%s) at pos=(%.0f, %.0f), mean %.1f m off polyline",
            best_track.display_name,
            best_track.id,
            x,
            z,
            best_mean,
        )
        return self._chosen

    def force(self, track_id: str) -> Track:
        if track_id not in self._tracks:
            available = sorted(self._tracks)[:10]
            raise KeyError(
                f"unknown track id {track_id!r}; known: {available}... ({len(self._tracks)} total)"
            )
        self._chosen = self._tracks[track_id]
        log.info("track forced: %s (id=%s)", self._chosen.display_name, self._chosen.id)
        return self._chosen

    def current_turn(self, packet: Packet) -> tuple[int, int] | None:
        if self._chosen is None or not self._chosen.turns:
            return None
        poly_idx, _ = self._chosen.nearest_polyline_index(packet.pos_x, packet.pos_z)
        turn_idx = self._chosen.nearest_turn_index(poly_idx)
        if turn_idx is None:
            return None
        return (turn_idx + 1, len(self._chosen.turns))

    # ---- internals -------------------------------------------------------

    def _in_release_bbox(self, track: Track, x: float, z: float) -> bool:
        x_min, x_max, z_min, z_max = track.bbox
        m = self._sticky_release_m
        return (x_min - m) <= x <= (x_max + m) and (z_min - m) <= z <= (z_max + m)
