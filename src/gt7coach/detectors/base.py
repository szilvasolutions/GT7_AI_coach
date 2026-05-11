"""Common types used across detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

G_MS2 = 9.80665  # 1 g in m/s² — used to convert measured accelerations to "g"


@dataclass(slots=True, frozen=True)
class Event:
    """A driving event detected by physics — never by an LLM.

    Attributes:
        type: dotted identifier, e.g. ``"braking.late_brake"``.
        severity: 0.0 (mild) .. 1.0 (severe). Caps the coach's tone.
        t_offset: seconds since the start of the corner this event belongs to.
        evidence: per-detector metrics (kept compact: ≤6 scalar keys).
    """

    type: str
    severity: float
    t_offset: float
    evidence: dict[str, Any] = field(default_factory=dict)
