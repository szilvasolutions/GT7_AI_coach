"""Replay a recorded session CSV as a stream of :class:`Packet` objects.

The CSV schema is one column per :class:`Packet` field. ``recv_time`` is the
authoritative timing axis; replay sleeps to recreate inter-arrival gaps unless
``realtime=False``.

A fixture CSV must be parsable by this module to be useful for tests, so the
header column names must match the :class:`Packet` field names exactly.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator
from pathlib import Path
from time import monotonic, sleep

from gt7coach.telemetry.packet import Packet

log = logging.getLogger(__name__)

_INT_FIELDS = frozenset(
    {
        "packet_id",
        "throttle",
        "brake",
        "gear",
        "lap_count",
        "lap_time_ms",
        "flags",
        "best_lap_ms",
        "rev_light_min",
        "rev_light_max",
        "time_of_day_ms",
    }
)
_OPTIONAL_FLOAT_FIELDS = frozenset({"steer_angle", "accel_long", "accel_lat"})

# Fields added after v0.x — replay fixtures recorded before they existed are
# allowed to omit them and we fall back to a benign default. Detectors that
# rely on these fields are off by default in old replays.
_FIELD_DEFAULTS: dict[str, int | float] = {
    "best_lap_ms": -1,
    "fuel_level": 0.0,
    "fuel_capacity": 0.0,
    "oil_temp": 0.0,
    "water_temp": 0.0,
    "rev_light_min": 0,
    "rev_light_max": 0,
    "time_of_day_ms": 0,
    # Added after the first real captures were recorded; a CSV without these
    # columns must still replay.
    "tyre_radius_fl": 0.0,
    "tyre_radius_fr": 0.0,
    "tyre_radius_rl": 0.0,
    "tyre_radius_rr": 0.0,
    "ride_height": 0.0,
    "roll_rate": 0.0,
    "pitch_rate": 0.0,
    "steer_rate": 0.0,
}


def _coerce(field_name: str, raw: str) -> int | float | None:
    if raw == "" and field_name in _OPTIONAL_FLOAT_FIELDS:
        return None
    if field_name in _INT_FIELDS:
        return int(raw)
    return float(raw)


def replay_csv(path: str | Path, *, realtime: bool = False) -> Iterator[Packet]:
    """Yield :class:`Packet` objects from a recorded CSV.

    Args:
        path: CSV path. The header row must contain one column per
            :class:`Packet` field.
        realtime: if True, sleep between yields to recreate the original
            inter-arrival timing (using ``recv_time``). Defaults to False so
            tests run instantly.
    """
    path = Path(path)
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return
        present = set(reader.fieldnames)
        missing = _required_fields() - present - set(_FIELD_DEFAULTS)
        if missing:
            raise ValueError(f"fixture {path} missing columns: {sorted(missing)}")

        last_recv: float | None = None
        wall_start = monotonic()
        first_recv: float | None = None

        for row in reader:
            values: dict[str, int | float | None] = {
                name: _coerce(name, row[name]) for name in reader.fieldnames
            }
            for field_name, default in _FIELD_DEFAULTS.items():
                if field_name not in values:
                    values[field_name] = default
            pkt = Packet(**values)  # type: ignore[arg-type]

            if realtime:
                if first_recv is None:
                    first_recv = pkt.recv_time
                target = wall_start + (pkt.recv_time - first_recv)
                delay = target - monotonic()
                if delay > 0:
                    sleep(delay)
            last_recv = pkt.recv_time
            yield pkt
        log.debug("replay finished: last recv_time=%s", last_recv)


def _required_fields() -> set[str]:
    return set(Packet.__dataclass_fields__.keys())
