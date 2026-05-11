"""``gt7coach-list-tracks`` — print every track in the built-in database.

Use the printed ``id`` with ``gt7coach-coach --track <id>`` or
``coach.track`` in ``config.yaml`` to force a particular layout.
"""

from __future__ import annotations

import argparse
import sys

from gt7coach.tracks.database import load_default_tracks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gt7coach-list-tracks",
        description="List all GT7 tracks recognised by gt7coach.",
    )
    parser.add_argument(
        "--filter",
        default=None,
        help="Case-insensitive substring filter (matches id or display name).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Also print length, corner count, country, oval flag.",
    )
    args = parser.parse_args(argv)

    tracks = load_default_tracks()
    if not tracks:
        print("no track database found — re-run scripts/build_track_db.py", file=sys.stderr)
        return 1

    items = sorted(tracks.items(), key=lambda kv: kv[1].display_name)
    if args.filter:
        needle = args.filter.lower()
        items = [
            (tid, t)
            for tid, t in items
            if needle in tid.lower() or needle in t.display_name.lower()
        ]

    print(f"{'id':<48s} {'display name':<48s}")
    print("-" * 100)
    for tid, t in items:
        name = t.display_name
        if args.verbose:
            length_km = t.length_m / 1000.0 if t.length_m else 0.0
            extra = f"  ({length_km:.2f} km, {t.num_corners} corners, {t.country or '??'}"
            if t.is_oval:
                extra += ", oval"
            extra += ")"
            name += extra
        print(f"{tid:<48s} {name}")
    print(f"\n{len(items)} tracks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
