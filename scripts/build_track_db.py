"""Build ``src/gt7coach/tracks/data/tracks.json`` from upstream sources.

Sources:
  * zetetos/gt-telemetry — per-track polylines (MIT licensed; vendored)
  * ddm999/gt7info        — official PD course IDs + metadata (community)

Outputs:
  * src/gt7coach/tracks/data/tracks.json  — runtime database

Run once after pulling fresh data; the result is committed to the repo so
end-users don't need network access. Re-run when zetetos / ddm999 publish
new tracks.

Usage:
    python scripts/build_track_db.py
        [--zetetos PATH]   (default: clone fresh)
        [--course-csv PATH] (default: fetch fresh)
        [--out PATH]       (default: src/gt7coach/tracks/data/tracks.json)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("build_track_db")

ZETETOS_URL = "https://github.com/zetetos/gt-telemetry.git"
COURSE_CSV_URL = "https://raw.githubusercontent.com/ddm999/gt7info/web-new/_data/db/course.csv"

# Map ddm999 country IDs (int) to ISO country codes. ddm999 publishes these
# as integers; only the common ones matter for prompt text.
_COUNTRY_LOOKUP: dict[int, str] = {
    1: "us",
    2: "jp",
    3: "us",
    4: "de",
    5: "fr",
    6: "it",
    7: "gb",
    8: "be",
    9: "es",
    10: "pt",
    11: "br",
    12: "au",
    13: "nl",
    14: "us",
    15: "ch",
    16: "at",
    17: "fi",
}


# ---------- I/O helpers ---------------------------------------------------


def fetch_zetetos(target: Path) -> Path:
    """Shallow-clone zetetos/gt-telemetry into ``target``; return inventory dir."""
    if not target.exists():
        log.info("cloning %s ...", ZETETOS_URL)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", ZETETOS_URL, str(target)],
            check=True,
        )
    inv = target / "pkg" / "circuits" / "inventory"
    if not inv.is_dir():
        raise FileNotFoundError(f"no inventory dir at {inv}")
    return inv


def fetch_course_csv(target: Path) -> Path:
    if not target.exists():
        log.info("downloading %s ...", COURSE_CSV_URL)
        with urllib.request.urlopen(COURSE_CSV_URL) as resp:
            target.write_bytes(resp.read())
    return target


def load_zetetos_tracks(inventory_dir: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(inventory_dir.glob("*.json")):
        with p.open(encoding="utf-8") as fh:
            out.append(json.load(fh))
    log.info("loaded %d zetetos tracks", len(out))
    return out


def load_course_csv(csv_path: Path) -> list[dict]:
    out: list[dict] = []
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                row["ID"] = int(row["ID"])
                row["Length"] = int(row["Length"]) if row["Length"] else 0
                row["LongestStraight"] = (
                    int(row["LongestStraight"]) if row["LongestStraight"] else 0
                )
                row["NumCorners"] = int(row["NumCorners"]) if row["NumCorners"] else 0
                row["IsOval"] = bool(int(row["IsOval"])) if row["IsOval"] else False
                row["IsReverse"] = bool(int(row["IsReverse"])) if row["IsReverse"] else False
                row["Country"] = int(row["Country"]) if row["Country"] else 0
            except (ValueError, KeyError):
                continue
            out.append(row)
    log.info("loaded %d ddm999 entries", len(out))
    return out


# ---------- joining + derivation -----------------------------------------


def normalise(name: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace for fuzzy match."""
    n = name.lower()
    n = re.sub(r"[^\w\s]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    # common substitutions to align zetetos / ddm999 spellings
    n = n.replace("autodromo", "")
    n = n.replace("autódromo", "")
    n = n.replace("circuit", "")
    n = n.replace("raceway", "")
    n = n.replace("speedway", "")
    n = n.replace("internazionale", "")
    n = n.replace("international", "")
    n = re.sub(r"\s+", " ", n).strip()
    return n


def best_ddm_match(zetetos_name: str, ddm_rows: list[dict]) -> dict | None:
    target = normalise(zetetos_name)
    best: tuple[int, dict | None] = (0, None)
    for row in ddm_rows:
        score = _similarity(target, normalise(row["Name"]))
        if score > best[0]:
            best = (score, row)
    return best[1] if best[0] >= 60 else None  # 60 % is conservative


def _similarity(a: str, b: str) -> int:
    """Cheap stand-in for rapidfuzz: ratio of longest-common-substring + token overlap."""
    if not a or not b:
        return 0
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return 0
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
    # Also reward when one is a prefix/suffix of the other
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    contains_bonus = 0.3 if short and short in long_ else 0.0
    return int((overlap + contains_bonus) * 100)


def derive_bbox(coords: list[dict]) -> dict:
    xs = [c["x"] for c in coords]
    zs = [c["z"] for c in coords]
    pad_x = (max(xs) - min(xs)) * 0.05
    pad_z = (max(zs) - min(zs)) * 0.05
    return {
        "x_min": min(xs) - pad_x,
        "x_max": max(xs) + pad_x,
        "z_min": min(zs) - pad_z,
        "z_max": max(zs) + pad_z,
    }


@dataclass
class TurnPoint:
    index: int  # index into polyline
    x: float
    z: float
    turning_radius_m: float


def detect_turns(coords: list[dict], min_radius_m: float = 8.0) -> list[TurnPoint]:
    """Crude turn detection: find local maxima of curvature along the polyline.

    Curvature ≈ inverse of the radius of the circle through three consecutive
    points. The smaller the radius, the tighter the turn. We pick local peaks
    of the |curvature| signal and treat each as one apex.
    """
    if len(coords) < 5:
        return []
    curvatures: list[float] = [0.0] * len(coords)
    for i in range(1, len(coords) - 1):
        p0 = coords[i - 1]
        p1 = coords[i]
        p2 = coords[i + 1]
        # Side lengths.
        a = math.hypot(p1["x"] - p0["x"], p1["z"] - p0["z"])
        b = math.hypot(p2["x"] - p1["x"], p2["z"] - p1["z"])
        c = math.hypot(p2["x"] - p0["x"], p2["z"] - p0["z"])
        # Heron's area, then circumradius R = abc / (4 * area).
        s = (a + b + c) / 2
        area_sq = max(0.0, s * (s - a) * (s - b) * (s - c))
        area = math.sqrt(area_sq)
        if area < 1e-3:
            curvatures[i] = 0.0
            continue
        radius = (a * b * c) / (4 * area)
        curvatures[i] = 1.0 / max(radius, 0.5)

    # Find local maxima with a minimum spacing.
    turns: list[TurnPoint] = []
    min_spacing = max(3, len(coords) // 50)  # avoid clustered apexes
    last_pick = -10_000
    # Threshold: only keep turns with radius < ~80 m on the polyline.
    threshold_curvature = 1.0 / 80.0
    for i in range(2, len(coords) - 2):
        if curvatures[i] < threshold_curvature:
            continue
        # Local-max check
        if curvatures[i] <= curvatures[i - 1] or curvatures[i] <= curvatures[i + 1]:
            continue
        if i - last_pick < min_spacing:
            continue
        last_pick = i
        radius = 1.0 / curvatures[i]
        if radius < min_radius_m:
            continue
        turns.append(
            TurnPoint(
                index=i,
                x=coords[i]["x"],
                z=coords[i]["z"],
                turning_radius_m=round(radius, 1),
            )
        )
    return turns


def shape_description(
    zetetos: dict,
    ddm: dict | None,
    turns: list[TurnPoint],
    bbox: dict,
) -> str:
    """Build a 1-line description for the LLM prompt."""
    parts: list[str] = []

    length_km = zetetos.get("length", 0) / 1000.0
    if length_km > 0:
        if length_km < 2.5:
            parts.append("short technical circuit")
        elif length_km < 5.0:
            parts.append("medium-length circuit")
        elif length_km < 10.0:
            parts.append("long circuit")
        else:
            parts.append("very long circuit")

    if ddm:
        if ddm.get("IsOval"):
            parts.append("oval layout, banked turns")
        if ddm.get("LongestStraight", 0) > 1200:
            parts.append("with significant straights")
        n_corners = ddm.get("NumCorners", 0)
        if n_corners > 0:
            parts.append(f"{n_corners} corners")
        elev = ddm.get("ElevationDiff")
        try:
            if elev and float(elev) > 30:
                parts.append("with notable elevation changes")
        except (TypeError, ValueError):
            pass

    # If we have a fair number of detected apexes, infer corner density.
    if turns:
        density = len(turns) / max(length_km, 0.5)
        if density > 5:
            parts.append("twisting layout")

    if not parts:
        parts.append("circuit")

    return ", ".join(parts) + "."


# ---------- driver -------------------------------------------------------


def build(zetetos_path: Path, course_csv: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    zetetos_tracks = load_zetetos_tracks(zetetos_path)
    ddm_rows = load_course_csv(course_csv)

    db: list[dict] = []
    matched = 0
    for t in zetetos_tracks:
        coords = t.get("coordinates", [])
        if len(coords) < 50:
            log.warning("skipping %s — only %d coords", t.get("id"), len(coords))
            continue
        bbox = derive_bbox(coords)
        turns = detect_turns(coords)
        ddm = best_ddm_match(t.get("name") or t.get("id", ""), ddm_rows)
        if ddm:
            matched += 1
        record = {
            "id": t["id"],
            "display_name": t.get("name", t["id"]),
            "variation": t.get("variation"),
            "country": t.get("country") or _COUNTRY_LOOKUP.get(ddm.get("Country", 0) if ddm else 0),
            "default_layout": t.get("default", False),
            "length_m": t.get("length", 0),
            "pd_course_id": ddm.get("ID") if ddm else None,
            "num_corners": ddm.get("NumCorners") if ddm else len(turns),
            "longest_straight_m": ddm.get("LongestStraight") if ddm else None,
            "is_oval": ddm.get("IsOval") if ddm else False,
            "is_reverse": ddm.get("IsReverse") if ddm else False,
            "elevation_diff_m": (
                float(ddm.get("ElevationDiff"))
                if ddm and ddm.get("ElevationDiff") not in (None, "")
                else None
            ),
            "bbox": bbox,
            "start_line": t.get("startLine"),
            "polyline": [{"x": c["x"], "z": c["z"]} for c in coords],
            "turns": [
                {
                    "index": t.index,
                    "x": t.x,
                    "z": t.z,
                    "turning_radius_m": t.turning_radius_m,
                }
                for t in turns
            ],
            "shape_description": shape_description(t, ddm, turns, bbox),
        }
        db.append(record)

    log.info("built %d tracks (%d matched to ddm999)", len(db), matched)
    out_path.write_text(json.dumps({"tracks": db}, indent=1), encoding="utf-8")
    log.info("wrote %s (%d KiB)", out_path, out_path.stat().st_size // 1024)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zetetos", type=Path, default=None)
    p.add_argument("--course-csv", type=Path, default=None)
    p.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "src"
        / "gt7coach"
        / "tracks"
        / "data"
        / "tracks.json",
    )
    args = p.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="gt7coach-tracks-"))
    zetetos = args.zetetos or workdir / "gt-telemetry"
    course = args.course_csv or workdir / "course.csv"
    inv_dir = fetch_zetetos(zetetos)
    fetch_course_csv(course)
    build(inv_dir, course, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
