"""TrackDetector: matches incoming packets to a known :class:`Track`.

Behaviour:

* The first packet whose ``(pos_x, pos_z)`` lands inside a known track's
  bounding box wins the session — once chosen, the result is cached and
  every subsequent ``feed()`` call returns the same Track.
* If no match after ``max_probes`` packets (default 60), gives up and
  returns ``None`` forever after. Coaching continues with no track
  context.
* ``force(track_id)`` lets the CLI bypass auto-detection entirely.
"""

from __future__ import annotations

import logging

from gt7coach.telemetry.packet import Packet
from gt7coach.tracks.database import DEFAULT_TRACKS, Track

log = logging.getLogger(__name__)


class TrackDetector:
    def __init__(
        self,
        tracks: dict[str, Track] | None = None,
        *,
        max_probes: int = 60,
    ) -> None:
        self._tracks = tracks if tracks is not None else DEFAULT_TRACKS
        self._max_probes = max_probes
        self._chosen: Track | None = None
        self._probes_seen: int = 0
        self._gave_up: bool = False

    # ---- public ----------------------------------------------------------

    @property
    def track(self) -> Track | None:
        return self._chosen

    def feed(self, packet: Packet) -> Track | None:
        """Try to match this packet to a known track. Returns the choice (or None)."""
        if self._chosen is not None or self._gave_up:
            return self._chosen
        self._probes_seen += 1
        for tr in self._tracks.values():
            if tr.contains(packet.pos_x, packet.pos_z):
                self._chosen = tr
                log.info(
                    "track detected: %s (id=%s) at pos=(%.0f, %.0f)",
                    tr.display_name,
                    tr.id,
                    packet.pos_x,
                    packet.pos_z,
                )
                return self._chosen
        if self._probes_seen >= self._max_probes:
            log.warning(
                "no track matched after %d probes (pos last=(%.0f, %.0f)); "
                "running without track context",
                self._probes_seen,
                packet.pos_x,
                packet.pos_z,
            )
            self._gave_up = True
        return None

    def force(self, track_id: str) -> Track:
        """CLI override: skip auto-detection and use the named track."""
        if track_id not in self._tracks:
            raise KeyError(f"unknown track id {track_id!r}; known: {list(self._tracks)}")
        self._chosen = self._tracks[track_id]
        log.info("track forced: %s (id=%s)", self._chosen.display_name, self._chosen.id)
        return self._chosen
