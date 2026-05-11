"""Built-in track records used by :class:`TrackDetector`.

Each :class:`Track` carries:

* ``id``                — stable machine identifier (snake_case).
* ``display_name``      — human-readable label. **Never spoken** to the
  driver; only used in logs and the AI's internal context.
* ``x_range`` / ``z_range`` — GT7 world-coordinate bounding box (metres).
  Bboxes are extracted from real captures and padded ~10 % on each side
  so cars on the verge / off-track still match.
* ``shape_description`` — one-line summary glued into the LLM prompt as
  context. Should be evocative but never name the track.

Phase-6 ships exactly one track. Phase-7+ extends the dict; adding a
track is a single entry, no schema changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Track:
    id: str
    display_name: str
    x_range: tuple[float, float]
    z_range: tuple[float, float]
    shape_description: str

    def contains(self, x: float, z: float) -> bool:
        return self.x_range[0] <= x <= self.x_range[1] and self.z_range[0] <= z <= self.z_range[1]


# Bounding box derived from the user's first live Deep Forest session
# (pos_x in [-491, 877], pos_z in [-263, 211]), padded ~10 % each side.
_DEEP_FOREST = Track(
    id="deep_forest",
    display_name="Deep Forest Raceway",
    x_range=(-630.0, 1015.0),
    z_range=(-340.0, 290.0),
    shape_description=(
        "mountain forest circuit with long high-speed esses, "
        "fast undulating corners, and a slow uphill hairpin"
    ),
)


DEFAULT_TRACKS: dict[str, Track] = {
    _DEEP_FOREST.id: _DEEP_FOREST,
}
