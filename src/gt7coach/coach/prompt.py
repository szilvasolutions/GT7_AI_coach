"""Prompt template used by every provider.

Kept in one place so the wording is consistent across LLMs and easy to A/B
test. The system block is stable (good for prompt caching); the user block
varies per corner.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gt7coach.coach.advisor import CornerContext
    from gt7coach.detectors import Event


SYSTEM_PROMPT = """\
You are an expert racing-driver coach speaking through a TTS earpiece during
a Gran Turismo 7 race. The driver has just finished a corner. Convert the
detected event(s) into ONE short, imperative coaching sentence the driver
can act on at the next corner.

Hard constraints on the response:
- ONE full SENTENCE that begins with a verb (Brake, Trail, Ease, Hold,
  Wait, Open, Carry, Add, Lift, Settle, Roll, Square, Straighten, Smooth,
  Apply, Release, Unwind, Patience). Not a phrase, not a label.
- Maximum 12 words. Aim for 4-8.
- Imperative voice ("Brake later", not "You should brake later").
- Aimed at the upcoming corner; reflects the SPECIFIC event detected.
- No filler ("good job", "remember to", "try to", "next time").
- No analysis or theory — just the action.
- Natural spoken English — this will be read aloud, not displayed.
- Never invent telemetry numbers; only use what's in the event evidence.
- Never echo the driver style ("Smooth driver") or the event type
  ("late_brake") back as a response. Translate them into an action.
"""


_STYLE_HINTS: dict[str, str] = {
    "smooth": "The driver is smooth and progressive. Don't critique slow brake build-up.",
    "aggressive": "The driver is aggressive. Lean towards stability and exit-speed advice.",
    "learning": "The driver is learning. Prefer the single most important fix.",
}


def _summarise_evidence(evidence: dict) -> str:
    """Render an Event's evidence dict as a compact comma-separated string."""
    if not evidence:
        return ""
    parts = []
    for k, v in evidence.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:.2f}")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def build_user_prompt(events: Iterable[Event], context: CornerContext, driver_style: str) -> str:
    """Compose the per-corner user-message body."""
    events_lines = "\n".join(
        f"- {e.type} (severity {e.severity:.2f}): {_summarise_evidence(e.evidence)}" for e in events
    )
    style_hint = _STYLE_HINTS.get(driver_style.lower(), "")
    return (
        f"Driver style: {driver_style}. {style_hint}\n"
        f"Corner: peak {context.peak_lat_g:.1f}g lat, "
        f"min speed {context.min_speed_kmh:.0f} km/h, "
        f"duration {context.duration_s:.1f}s.\n"
        f"Detected events:\n{events_lines}\n"
        f"Respond with ONE imperative coaching sentence, max 12 words."
    )
