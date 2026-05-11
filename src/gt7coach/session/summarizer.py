"""End-of-session summary.

Reads a finished session directory and produces a short LLM-written
reflection on the run. Per spec §9, this is where LLMs actually shine —
they don't need real-time latency and they get aggregated context.

The summarizer runs *after* the session closes so it doesn't compete with
the live coaching loop for tokens or latency.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gt7coach.coach.providers import CoachProvider

log = logging.getLogger(__name__)


SUMMARY_SYSTEM_PROMPT = """\
You are an expert racing-driver coach reviewing a finished GT7 session
with the driver. Read the per-event statistics and produce a SHORT
post-session debrief (3-5 sentences). Be specific about what went well
and what to work on next session, but stay conversational — this is
spoken to the driver, not a written report. No filler, no praise without
specifics, no bullet points.
"""


@dataclass(slots=True)
class SessionStats:
    """Lightweight summary of one session, fed to the LLM."""

    total_corners: int
    total_events: int
    event_counts: dict[str, int]
    event_avg_severity: dict[str, float]
    fastest_corner_speed_kmh: float | None
    slowest_corner_speed_kmh: float | None
    peak_lat_g: float

    def as_prompt_block(self) -> str:
        if not self.event_counts:
            return (
                f"Corners driven: {self.total_corners}. "
                f"Peak cornering load: {self.peak_lat_g:.2f} g. "
                f"No specific driving events detected."
            )
        lines = [
            f"Corners driven: {self.total_corners}.",
            f"Peak cornering load: {self.peak_lat_g:.2f} g.",
        ]
        if self.fastest_corner_speed_kmh is not None and self.slowest_corner_speed_kmh is not None:
            lines.append(
                f"Speed range across corners: {self.slowest_corner_speed_kmh:.0f}"
                f"-{self.fastest_corner_speed_kmh:.0f} km/h."
            )
        lines.append("Detected events by type:")
        for kind, count in sorted(self.event_counts.items(), key=lambda kv: -kv[1]):
            avg = self.event_avg_severity.get(kind, 0.0)
            lines.append(f"  - {kind}: {count}x (avg severity {avg:.2f})")
        return "\n".join(lines)


def aggregate(session_dir: Path) -> SessionStats:
    """Read ``events.jsonl`` from a finished session and build stats."""
    events_path = session_dir / "events.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(f"no events.jsonl in {session_dir}")

    counts: Counter[str] = Counter()
    severities: dict[str, list[float]] = defaultdict(list)
    corner_speeds: list[float] = []
    peak_lat_g = 0.0
    total_events = 0
    total_corners = 0

    with events_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total_corners += 1
            trace = rec.get("trace", {})
            min_speed = trace.get("min_speed_kmh")
            if isinstance(min_speed, int | float):
                corner_speeds.append(float(min_speed))
            if isinstance(trace.get("peak_lat_g"), int | float):
                peak_lat_g = max(peak_lat_g, float(trace["peak_lat_g"]))
            for evt in rec.get("events", []):
                total_events += 1
                t = evt.get("type", "unknown")
                counts[t] += 1
                sev = evt.get("severity")
                if isinstance(sev, int | float):
                    severities[t].append(float(sev))

    avg_severity = {t: (sum(v) / len(v) if v else 0.0) for t, v in severities.items()}
    return SessionStats(
        total_corners=total_corners,
        total_events=total_events,
        event_counts=dict(counts),
        event_avg_severity=avg_severity,
        fastest_corner_speed_kmh=max(corner_speeds) if corner_speeds else None,
        slowest_corner_speed_kmh=min(corner_speeds) if corner_speeds else None,
        peak_lat_g=round(peak_lat_g, 2),
    )


def summarise(
    session_dir: Path,
    *,
    provider: CoachProvider,
    driver_style: str = "smooth",
) -> str:
    """Produce a 3-5 sentence post-session reflection using the given provider."""
    stats = aggregate(session_dir)
    user = (
        f"Driver style: {driver_style}.\n"
        f"{stats.as_prompt_block()}\n"
        "Write 3-5 sentences of post-session feedback."
    )
    response = provider.complete(SUMMARY_SYSTEM_PROMPT, user)
    summary = (response or "").strip()
    (session_dir / "summary.txt").write_text(summary, encoding="utf-8")
    (session_dir / "summary_prompt.txt").write_text(
        f"--- SYSTEM ---\n{SUMMARY_SYSTEM_PROMPT}\n--- USER ---\n{user}\n",
        encoding="utf-8",
    )
    return summary


__all__ = ["SUMMARY_SYSTEM_PROMPT", "SessionStats", "aggregate", "summarise"]
