"""SessionLogger: writes telemetry, events, and coach turns to disk per run.

Produces a per-run directory ``sessions/run_<timestamp>/`` containing:

* ``telemetry.csv`` — every packet, schema-compatible with
  :func:`gt7coach.telemetry.replay_csv`. Lets the user replay the live
  session offline.
* ``events.jsonl`` — one JSON object per detected event, with the corner
  index and the parent trace summary attached.
* ``coach.jsonl`` — one JSON object per advisor turn: the chosen event,
  the verbatim system + user prompts the LLM saw, the response (or the
  suppression reason). This is the AI audit log.
* ``meta.json`` — host info, CLI args, start / end times. Written once at
  the end of the run.

Writes are synchronous + flushed every line, so a hard kill keeps the data.
That's fine at 60 Hz with ~25 fields per row; the receive loop has been
profiled to spend well under 10 % of its tick on logging in practice.
"""

from __future__ import annotations

import csv
import json
import logging
import platform
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from gt7coach.coach.advisor import AdvisorResult, CornerContext, IncidentResult
from gt7coach.detectors import CornerTrace, Event
from gt7coach.telemetry.packet import Packet

log = logging.getLogger(__name__)


def _packet_row(pkt: Packet) -> dict[str, str]:
    row: dict[str, str] = {}
    for name, value in asdict(pkt).items():
        if value is None:
            row[name] = ""
        elif isinstance(value, float):
            row[name] = repr(value)
        else:
            row[name] = str(value)
    return row


def _trace_summary(trace: CornerTrace) -> dict[str, Any]:
    ctx = CornerContext.from_trace(trace)
    return {
        "duration_s": ctx.duration_s,
        "entry_speed_kmh": ctx.entry_speed_kmh,
        "min_speed_kmh": ctx.min_speed_kmh,
        "exit_speed_kmh": ctx.exit_speed_kmh,
        "peak_lat_g": ctx.peak_lat_g,
        "start_packet_id": trace.packets[0].packet_id,
        "end_packet_id": trace.packets[-1].packet_id,
        "frame_count": len(trace),
    }


def _event_payload(event: Event) -> dict[str, Any]:
    return {
        "type": event.type,
        "severity": event.severity,
        "t_offset": event.t_offset,
        "evidence": event.evidence,
    }


class SessionLogger:
    """Owns three append-only files for one run of ``gt7coach-coach``."""

    def __init__(
        self,
        root_dir: Path,
        *,
        cli_args: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        ts = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.dir = root_dir / f"run_{ts}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cli_args = cli_args or {}
        self._started_at = datetime.now()

        self._telemetry_path = self.dir / "telemetry.csv"
        self._events_path = self.dir / "events.jsonl"
        self._coach_path = self.dir / "coach.jsonl"
        self._meta_path = self.dir / "meta.json"

        self._telemetry_fh: IO[str] = self._telemetry_path.open("w", newline="", encoding="utf-8")
        fieldnames = list(Packet.__dataclass_fields__.keys())
        self._telemetry_writer = csv.DictWriter(self._telemetry_fh, fieldnames=fieldnames)
        self._telemetry_writer.writeheader()

        self._events_fh: IO[str] = self._events_path.open("w", encoding="utf-8")
        self._coach_fh: IO[str] = self._coach_path.open("w", encoding="utf-8")

        self._packet_count = 0
        self._corner_count = 0
        self._event_count = 0
        self._advice_count = 0
        self._incident_count = 0
        # corner_idx -> trace, kept so async results from the worker can be
        # logged with the right corner context after the receive loop has
        # already moved on.
        self._traces_by_idx: dict[int, CornerTrace] = {}

        log.info("session log: %s", self.dir)

    # ---- writes ----------------------------------------------------------

    def log_packet(self, pkt: Packet) -> None:
        self._telemetry_writer.writerow(_packet_row(pkt))
        self._packet_count += 1

    def log_corner(self, corner_idx: int, trace: CornerTrace, events: list[Event]) -> None:
        self._corner_count = max(self._corner_count, corner_idx)
        # Keep the trace around so the async advisor result can be logged
        # with the right corner context later. Cleared in on_advisor_result.
        self._traces_by_idx[corner_idx] = trace
        record = {
            "corner_idx": corner_idx,
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "trace": _trace_summary(trace),
            "events": [_event_payload(e) for e in events],
        }
        self._events_fh.write(json.dumps(record) + "\n")
        self._events_fh.flush()
        self._event_count += len(events)

    def on_advisor_result(
        self,
        corner_idx: int,
        trace: CornerTrace | None,
        result: AdvisorResult | IncidentResult,
    ) -> None:
        """Callback registered on the Advisor — fired from the worker thread.

        Logs the FINAL result (with prompt + response) instead of the
        synchronous "queued" stub that the receive loop saw.
        """
        if isinstance(result, IncidentResult):
            self._log_incident_result(result)
            return
        # AdvisorResult — falls back to the cached trace if the worker
        # already removed it from our map (shouldn't happen but be safe).
        resolved_trace = trace or self._traces_by_idx.pop(corner_idx, None)
        if resolved_trace is None:
            log.debug("on_advisor_result for corner %d with no trace cached", corner_idx)
            return
        self.log_advisor(corner_idx, resolved_trace, result)
        self._traces_by_idx.pop(corner_idx, None)

    def _log_incident_result(self, result: IncidentResult) -> None:
        record = {
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "incident": {
                "type": result.incident.type,
                "severity": result.incident.severity,
                "recv_time": result.incident.recv_time,
                "evidence": result.incident.evidence,
            },
            "advice": result.advice,
            "suppressed_reason": result.suppressed_reason,
            "prompt": {
                "system": result.system_prompt,
                "user": result.user_prompt,
            }
            if result.user_prompt is not None
            else None,
        }
        self._coach_fh.write(json.dumps(record) + "\n")
        self._coach_fh.flush()
        self._incident_count += 1
        if result.advice is not None:
            self._advice_count += 1

    def log_advisor(
        self,
        corner_idx: int,
        trace: CornerTrace,
        result: AdvisorResult,
    ) -> None:
        record = {
            "corner_idx": corner_idx,
            "wall_time": datetime.now().isoformat(timespec="milliseconds"),
            "trace": _trace_summary(trace),
            "advice": result.advice,
            "suppressed_reason": result.suppressed_reason,
            "chosen_event": (
                _event_payload(result.chosen_event) if result.chosen_event is not None else None
            ),
            "prompt": {
                "system": result.system_prompt,
                "user": result.user_prompt,
            }
            if result.user_prompt is not None
            else None,
        }
        self._coach_fh.write(json.dumps(record) + "\n")
        self._coach_fh.flush()
        if result.advice is not None:
            self._advice_count += 1

    # ---- lifecycle -------------------------------------------------------

    def close(self) -> None:
        ended_at = datetime.now()
        meta = {
            "started_at": self._started_at.isoformat(timespec="seconds"),
            "ended_at": ended_at.isoformat(timespec="seconds"),
            "duration_s": (ended_at - self._started_at).total_seconds(),
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "node": platform.node(),
            },
            "cli_args": self._cli_args,
            "totals": {
                "packets": self._packet_count,
                "corners": self._corner_count,
                "events": self._event_count,
                "advice_spoken": self._advice_count,
                "incidents": self._incident_count,
            },
            "files": {
                "telemetry_csv": self._telemetry_path.name,
                "events_jsonl": self._events_path.name,
                "coach_jsonl": self._coach_path.name,
            },
        }
        self._meta_path.write_text(json.dumps(meta, indent=2))
        for fh in (self._telemetry_fh, self._events_fh, self._coach_fh):
            try:
                fh.flush()
                fh.close()
            except Exception:  # pragma: no cover
                pass
        log.info(
            "session done: %d packets / %d corners / %d events / %d spoken",
            self._packet_count,
            self._corner_count,
            self._event_count,
            self._advice_count,
        )

    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
