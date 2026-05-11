"""Built-in track records used by :class:`TrackDetector`.

This module reads ``data/tracks.json`` — the joined output of zetetos's
``gt-telemetry`` polyline inventory and ddm999's ``gt7info`` metadata.
Build the JSON with ``scripts/build_track_db.py``.

The polylines are integer-metre world coordinates in the same frame as the
``pos_x`` / ``pos_z`` fields of the GT7 telemetry packet, so detection is
just spatial matching — no in-packet track ID exists (verified against
both zetetos's kaitai schema and Nenkai's PDTools).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path


@dataclass(slots=True, frozen=True)
class Turn:
    """One detected apex on the track's racing line."""

    index: int  # index into the track's polyline
    x: float
    z: float
    turning_radius_m: float


@dataclass(slots=True)
class Track:
    """One GT7 track layout."""

    id: str
    display_name: str
    country: str | None
    length_m: int
    num_corners: int
    is_oval: bool
    bbox: tuple[float, float, float, float]  # (x_min, x_max, z_min, z_max)
    polyline_x: list[float]
    polyline_z: list[float]
    turns: list[Turn]
    shape_description: str
    pd_course_id: int | None = None

    def contains(self, x: float, z: float) -> bool:
        x_min, x_max, z_min, z_max = self.bbox
        return x_min <= x <= x_max and z_min <= z <= z_max

    def nearest_polyline_index(self, x: float, z: float) -> tuple[int, float]:
        """Return ``(idx, distance_m)`` of the polyline point nearest to (x, z)."""
        # Brute force: 84 tracks x ~400 points after the bbox prefilter wipes
        # all but 1-3 candidates, so this is ~1500 ops per packet, easily
        # under 1 ms in Python.
        best_idx = 0
        best_d2 = float("inf")
        for i, (px, pz) in enumerate(zip(self.polyline_x, self.polyline_z, strict=True)):
            dx = px - x
            dz = pz - z
            d2 = dx * dx + dz * dz
            if d2 < best_d2:
                best_d2 = d2
                best_idx = i
        return best_idx, best_d2**0.5

    def nearest_turn_index(self, polyline_idx: int) -> int | None:
        """Map a polyline index to the turn it's closest to."""
        if not self.turns:
            return None
        best = 0
        best_d = abs(self.turns[0].index - polyline_idx)
        for j in range(1, len(self.turns)):
            d = abs(self.turns[j].index - polyline_idx)
            if d < best_d:
                best_d = d
                best = j
        return best


@cache
def _load_database_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "tracks.json"


def _track_from_record(rec: dict) -> Track:
    bbox = rec["bbox"]
    polyline = rec["polyline"]
    turns = [
        Turn(
            index=t["index"],
            x=t["x"],
            z=t["z"],
            turning_radius_m=t["turning_radius_m"],
        )
        for t in rec.get("turns", [])
    ]
    return Track(
        id=rec["id"],
        display_name=rec["display_name"],
        country=rec.get("country"),
        length_m=int(rec.get("length_m") or 0),
        num_corners=int(rec.get("num_corners") or 0),
        is_oval=bool(rec.get("is_oval")),
        bbox=(bbox["x_min"], bbox["x_max"], bbox["z_min"], bbox["z_max"]),
        polyline_x=[p["x"] for p in polyline],
        polyline_z=[p["z"] for p in polyline],
        turns=turns,
        shape_description=rec.get("shape_description", "circuit"),
        pd_course_id=rec.get("pd_course_id"),
    )


@cache
def load_default_tracks() -> dict[str, Track]:
    """Lazy-load the built-in track database."""
    path = _load_database_path()
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Track] = {}
    for rec in raw.get("tracks", []):
        try:
            out[rec["id"]] = _track_from_record(rec)
        except (KeyError, TypeError, ValueError):
            continue
    return out


# Compatibility shim: previous code referenced DEFAULT_TRACKS as a module-level
# dict literal. Keep the same name pointing at the lazy loader's result.
DEFAULT_TRACKS = load_default_tracks()
