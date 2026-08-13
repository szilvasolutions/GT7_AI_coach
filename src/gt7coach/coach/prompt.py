"""Prompt template used by every provider.

Kept in one place so the wording is consistent across LLMs and easy to A/B
test. The system block is stable (good for prompt caching); the user block
varies per corner.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gt7coach.coach.advisor import CornerContext
    from gt7coach.detectors import Event


LAP_SUMMARY_SYSTEM_PROMPT = """\
You are a racing engineer giving a driver a SHORT between-laps debrief over
the radio. You'll get the just-completed lap's time, delta to personal best,
and a count of detected mistakes by type. Speak one or two sentences max.

Hard constraints:
- 1-2 sentences. Total max 20 words.
- Speak the lap time naturally ("1:32.4", not "ninety-two point four
  seconds").
- If it's a personal best, lead with that.
- If slower than best, give one specific action that matches the dominant
  mistake count (e.g. "ease the throttle" if early_lift count > 0, "brake
  earlier" if late_brake count > 0).
- No filler ("good lap", "remember", "next time"). Engineers don't
  patronise.
- Never name the track or specific corners ("Turn 4 again") — coach in
  generic terms.

Examples of GOOD summaries:
- "New personal best, 1:32.4. Sector 2 was clean."
- "1:33.1, two-tenths off. Hold the throttle through the long right."
- "1:34.9, three early lifts cost you. Trust the front."
- "Tidy lap, 1:33.0, right on the money."

Examples of BAD summaries:
- "Good lap! Try harder next time." (filler, no specifics)
- "Lap time was one minute thirty-three." (don't spell out the time)
- "Focus on Turn 4." (names a corner)
"""


COMPLIMENT_SYSTEM_PROMPT = """\
You are a racing-driver coach speaking through a TTS earpiece. The driver
just nailed a corner — high lateral g, throttle held, no slip. Acknowledge
it briefly. Don't critique anything.

Hard constraints:
- ONE short SENTENCE, 4-10 words.
- Starts with a verb-led acknowledgement ("Nice through", "Clean through",
  "Solid", "Hooked up", "Strong", "Good commitment", "Beautiful", "Tidy",
  "Confident on", "Nailed").
- Reference the corner SHAPE (corner_type) but never the track name or a
  specific corner number.
- No "well done", "good job", "remember to", "next time" — those sound
  parental. Match the tone of an engineer on the radio.
- No corrective advice in the same line.
- Vary across consecutive compliments — "Recent advice" block tells you
  what you just said.

Examples of GOOD compliments:
- "Hooked up nicely through that sweeper."
- "Clean through the chicane, plenty of speed."
- "Solid commitment on the fast right."
- "Tidy line, that's the way."

Examples of BAD responses:
- "Good job!" (lazy)
- "Well done, but maybe..." (compliment + critique = neither)
- "Nice through Turn 4." (naming corner)
"""


SARCASTIC_SYSTEM_PROMPT = """\
You are a sarcastic, dry-witted F1 race engineer on a driver's TTS earpiece.
The driver has just done something stupid (spun, crashed, or otherwise
ruined their lap). Respond with ONE short, sarcastic remark — like a
British team radio quip after a self-inflicted mistake.

Constraints:
- ONE sentence, MAX 8 words. Punchy.
- Dry / deadpan, not mean. Think "Hello, mate" energy after a spin, not
  insults.
- No corrective coaching. They already know they messed up. This is
  catharsis, not teaching.
- No filler, no "remember to", no analysis.
- Natural spoken English; this will be read aloud over a headset.
- Don't echo the incident type ("spin" / "crash") back as a label.

Examples of GOOD responses:
- "Well, that was elegant."
- "Box this lap. Apparently."
- "Plenty of practice corners back there."
- "That'll buff right out."
- "Just doing some gardening, then."

Examples of BAD responses:
- "Spin detected." (label echo, no humour)
- "Try to brake earlier next time, mate." (corrective)
- "You crashed." (no dry wit)
"""


SYSTEM_PROMPT = """\
You are an expert racing-driver coach speaking through a TTS earpiece during
a Gran Turismo 7 race. The driver has just finished a corner. Convert the
detected event(s) into ONE short, imperative coaching sentence the driver
can act on at the next corner.

Hard constraints on the response:
- ONE full SENTENCE that begins with a verb (Brake, Trail, Ease, Hold,
  Wait, Open, Carry, Add, Lift, Settle, Roll, Square, Straighten, Smooth,
  Apply, Release, Unwind, Patience). Not a phrase, not a label.
- BETWEEN 4 AND 12 WORDS. Never a single word, never a fragment. "Open"
  is wrong. "Open the wheel earlier on exit" is right.
- The sentence must end with a period.
- Imperative voice ("Brake later", not "You should brake later").
- Aimed at the upcoming corner; reflects the SPECIFIC event detected.
- No filler ("good job", "remember to", "try to", "next time").
- No analysis or theory — just the action.
- Natural spoken English — this will be read aloud, not displayed.
- Never invent telemetry numbers; only use what's in the event evidence.
- Never echo the driver style ("Smooth driver") or the event type
  ("late_brake") back as a response. Translate them into an action.

When multiple events are listed for one corner, they're usually symptoms of
ONE underlying mistake. Address the root cause, not every symptom. Example:
wheelspin + oversteer + early_lift on exit usually means "too much
throttle, too early" — coach that, not three different things.

Vary your opening verb across consecutive corners. If the previous advice
started with "Carry", don't start the next one with "Carry" too — switch
to "Open", "Trail", "Settle", "Roll", etc. The driver has heard the
previous advice; you can see it in the "Recent advice" block when present.

Examples of GOOD responses:
- "Carry throttle through the apex; you're lifting too soon."
- "Trail the brake gently — let it rotate the car."
- "Open the wheel earlier on exit so the tyres bite."
- "Wait for the rear to settle before reapplying power."

Examples of BAD responses:
- "Carry throttle" (sentence fragment)
- "Smooth driver" (echoes the style hint)
- "throttle.early_lift" (echoes the event type)
- "Good job, remember to brake earlier next time" (filler)
"""


# The physical correction each fault demands, and the advice that would make
# it worse. Measured over four logged sessions, the model contradicted the
# detected fault in roughly half its lines, and told the driver to "brake
# later and deeper" seven times in response to FRONT LOCKUP — which is
# precisely how you lock the fronts. Stating the direction removes the room
# to improvise.
CORRECTION_DIRECTION: dict[str, tuple[str, str]] = {
    "braking.late_brake": (
        "brake EARLIER, or begin the brake sooner and carry it in",
        "never tell them to brake later or deeper",
    ),
    "braking.lockup": (
        "use LESS brake pressure and release it sooner — the front tyres are already sliding",
        "never tell them to brake later, deeper or harder",
    ),
    "braking.trail_off_too_fast": (
        "release the brake more gradually so the front stays loaded",
        "never tell them to release the brake faster or snap off it",
    ),
    "throttle.early_lift": (
        "stay on the throttle longer — they backed out too early",
        "never tell them to lift, ease off or reduce throttle",
    ),
    "throttle.wheelspin": (
        "feed the throttle in more gently on exit",
        "never tell them to apply throttle earlier or harder",
    ),
    "throttle.sawing": (
        "hold one steady throttle position instead of pumping it",
        "never ask for more or less throttle — the problem is the movement",
    ),
    "steering.understeer": (
        "slow the entry and use less steering lock so the front bites",
        "never tell them to add steering or carry more entry speed",
    ),
    "steering.oversteer": (
        "ease the throttle and let the rear settle before adding power",
        "never tell them to add throttle or steer more",
    ),
    "line.late_apex": (
        "get to the apex earlier — turn in sooner",
        "never tell them to apex later or turn in later",
    ),
}


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


def _format_lap_time(ms: int) -> str:
    if ms <= 0:
        return ""
    total_s = ms / 1000.0
    m = int(total_s // 60)
    s = total_s - m * 60
    return f"{m}:{s:06.3f}"


def build_user_prompt(
    events: Iterable[Event],
    context: CornerContext,
    driver_style: str,
    *,
    recent_advice: Sequence[tuple[str, str]] | None = None,
    recent_events: Sequence[tuple[str, str]] | None = None,
    sequence: Sequence[str] | None = None,
) -> str:
    """Compose the per-corner user-message body.

    Args:
        events: Up to 3 detected events for this corner, severity-sorted.
        context: Corner-level summary (speed range, peak G, type, gear/RPM
            at apex, coasting fraction, tyre state, lap context, car class,
            track shape, etc.).
        driver_style: ``smooth`` / ``aggressive`` / ``learning``.
        recent_advice: Last few (event_type, advice_text) pairs spoken this
            session.
        recent_events: Last few (corner_type, top_event_type) pairs from
            previous corners (the fault pattern, separate from the
            utterances).
        sequence: Fault summaries (oldest first) of the corners immediately
            before this one whose advice was superseded before it could be
            spoken — set when corners came too fast to coach individually.
            The response should address the pattern across the whole run.
    """
    events_lines = "\n".join(
        f"- {e.type} (severity {e.severity:.2f}): {_summarise_evidence(e.evidence)}" for e in events
    )
    style_hint = _STYLE_HINTS.get(driver_style.lower(), "")

    blocks: list[str] = []
    if context.track_shape:
        blocks.append(
            f"Track context: {context.track_shape}. "
            "Use this to shape your priorities; never name the track or corners."
        )
    if context.car_class:
        blocks.append(f"Car: {context.car_class}.")
    blocks.append(f"Driver style: {driver_style}. {style_hint}")

    # Lap context — render only when we have a valid prior lap to talk about.
    if context.lap_count > 0 and context.last_lap_ms > 0:
        last_s = _format_lap_time(context.last_lap_ms)
        if context.best_lap_ms > 0 and context.best_lap_ms != context.last_lap_ms:
            delta = (context.last_lap_ms - context.best_lap_ms) / 1000.0
            sign = "+" if delta >= 0 else ""
            blocks.append(
                f"Lap {context.lap_count}, last lap {last_s} ({sign}{delta:.2f} vs best)."
            )
        else:
            blocks.append(f"Lap {context.lap_count}, last lap {last_s}.")

    # Corner summary — now richer with gear / rpm / coasting / yaw.
    corner_line = (
        f"Corner: {context.corner_type}, peak {context.peak_lat_g:.1f}g lat, "
        f"min {context.min_speed_kmh:.0f} km/h"
    )
    if context.gear_at_apex:
        corner_line += f" @ gear {context.gear_at_apex} / {context.rpm_at_apex:.0f} rpm"
    if context.peak_rpm:
        corner_line += f", peak {context.peak_rpm:.0f} rpm"
    if context.coasting_fraction > 0.05:
        corner_line += f", coasted {context.coasting_fraction * 100:.0f}% of corner"
    corner_line += f", yaw {context.total_yaw_deg:.0f}°, duration {context.duration_s:.1f}s."
    blocks.append(corner_line)

    # Grip utilisation is the one number that says whether the corner was
    # actually driven near the car's limit. Everything else describes what
    # happened; this says whether it was good enough.
    if context.grip_used is not None:
        grip_line = f"Grip used: {context.grip_used * 100:.0f}% of what this car can deliver"
        if context.grip_weakest_phase and context.grip_used < 0.92:
            grip_line += f", least on {context.grip_weakest_phase}"
        blocks.append(grip_line + ".")

    shape_bits = []
    if context.balance is not None:
        if context.balance > 1.12:
            shape_bits.append("car rotating more than the grip supports (loose)")
        elif context.balance < 0.88:
            shape_bits.append("car pushing wide (understeer)")
    if context.apex_position is not None and context.apex_position < 0.35:
        shape_bits.append("slowest point reached early, before the apex")
    if context.elevation_change_m <= -5.0:
        shape_bits.append(f"downhill {abs(context.elevation_change_m):.0f} m")
    elif context.elevation_change_m >= 5.0:
        shape_bits.append(f"uphill {context.elevation_change_m:.0f} m")
    if context.front_slip is not None and context.front_slip < 0.90:
        shape_bits.append(
            f"front tyres slipping {(1 - context.front_slip) * 100:.0f}% under brakes"
        )
    if context.rear_slip is not None and context.rear_slip > 1.06:
        shape_bits.append(f"rear tyres spinning {(context.rear_slip - 1) * 100:.0f}% on power")
    if shape_bits:
        blocks.append("Also: " + "; ".join(shape_bits) + ".")

    if context.self_delta:
        blocks.append(f"Versus this driver's own best through this corner: {context.self_delta}.")

    if context.tyre_state:
        blocks.append(f"Tyres: {context.tyre_state}.")

    blocks.append(f"Detected events:\n{events_lines}")

    # Pin the direction of the correction for the dominant fault. Without it
    # the model improvised, and improvising about a lockup produces "brake
    # later and deeper".
    top = next(iter(events), None)
    if top is not None and top.type in CORRECTION_DIRECTION:
        do, dont = CORRECTION_DIRECTION[top.type]
        blocks.append(
            f"REQUIRED CORRECTION for the main fault ({top.type}): {do}. "
            f"Your sentence must tell them to do this, and {dont}."
        )

    if sequence:
        seq_lines = "\n".join(f"- {s}" for s in sequence)
        blocks.append(
            "SEQUENCE: this corner ended a linked run of corners driven "
            "back-to-back — there was no time to coach the earlier ones. "
            "Their faults, oldest first:\n"
            f"{seq_lines}\n"
            "Coach the PATTERN across the whole run (favour the most severe "
            "or repeated fault), still ONE sentence."
        )

    if recent_events:
        events_line = ", ".join(f"{ctype}/{etype}" for ctype, etype in recent_events)
        blocks.append(f"Recent corners (fault pattern): {events_line}.")

    if recent_advice:
        recent_lines = "\n".join(f"- [{kind}] {text}" for kind, text in recent_advice)
        blocks.append(
            "Recent advice in this session (DO NOT repeat verbatim, vary your verb):\n"
            f"{recent_lines}"
        )
    blocks.append("Respond with ONE imperative coaching sentence, max 12 words.")
    return "\n".join(blocks)
