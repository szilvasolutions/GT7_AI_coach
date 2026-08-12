# Phase 6 — async LLM + thicker prompt context

Phase 5 made the coach smarter on paper, but the live Deep Forest session
that followed surfaced a class of *operational* failures that no prompt
tuning could fix:

* **Latency spikes blocked telemetry.** Per-corner Gemini round-trips
  ranged from 1.9 s (good) to 22 s (catastrophic). Because the receive
  loop called `provider.complete()` synchronously, a 22 s call didn't
  just delay advice — it stalled packet processing for 22 s. When the
  loop unblocked, the segmenter hallucinated a phantom 22.50 s corner
  from the UDP backlog.
* **The coach knew nothing about the session.** It saw corner shape +
  3 events + recent advice. It didn't know the track, the car, the
  gear/RPM mid-corner, how late in the race we were, or how the tyres
  were doing. From a coaching perspective, that's pit-wall with the
  radio on but no monitor.

Both got addressed in this branch.

## Async LLM worker

`Advisor` now runs the LLM call on a dedicated background thread
(`gt7coach-coach-worker`). The receive loop drops a `_PendingJob` into
a single slot, wakes the worker, and returns immediately with
`suppressed_reason="queued"` — never blocked.

**Drop-newest semantics.** If a fresh corner arrives while the worker
is mid-call, the previous queued corner is abandoned. The worker pulls
whatever is in the slot when it next checks. Rationale: 22-s-late
advice for a corner the driver has already left is worse than no
advice. There is intentionally no FIFO queue — backlogs of stale
utterances are the failure mode.

`AdvisorConfig.async_mode=True` is the new default. `flush(timeout)`
+ `stop()` shut the worker down cleanly; `main.py` calls them in the
`finally` block so the last corner's response still gets spoken before
the voice and session logger tear down.

Tests opt into the synchronous path via `async_mode=False` (pre-Phase-6
tests still exercise the same code that production uses, just without
the thread).

## Skip zero-event corners

19 of 44 corners in the test session were exit-acceleration phases
with no detector events. They were burning rate-limit budget for
nothing — the cooldown could lock out a *real* corner that came right
after. Two-line guard: if `events_list` is empty, return immediately
with `suppressed_reason="no events"` BEFORE the rate limiter is asked.

## CornerContext: gear / RPM / coasting / tyres / lap

`CornerTrace` exposes four new physics properties:

* `gear_at_apex` — gear at the packet closest to min-speed.
* `rpm_at_apex` — RPM at the same packet.
* `peak_rpm` — max RPM across the trace.
* `coasting_fraction` — share of the trace where `throttle < 5 AND brake < 5`.

`CornerContext` extends with `tyre_state` (FL/FR/RL/RR quantised to
`cold` / `warm` / `optimal` / `hot` with car-agnostic thresholds:
<60 °C / 60-75 / 75-95 / >100), plus `lap_count`, `last_lap_ms`,
`best_lap_ms`. The Advisor maintains best-lap memory across calls.

Session-level context (`car_class`, `track_shape`) is set once at
startup via `set_car_class()` / `set_track_shape()` and folded into
every prompt.

`build_user_prompt` renders all of these in optional blocks, omitting
each cleanly when its data is empty (so practice / out-laps don't
produce a confused prompt).

## CLI: --car-class + --track + the first track stub

* `--car-class "Gr.3 RWD"` — free-form descriptor max ~30 chars,
  spliced verbatim into the prompt as `Car: <value>.`. No parsing or
  validation — lets the LLM tailor power-on / drivetrain advice
  without us building a 600-car database.
* `--track <id>` — forces a known track id and skips auto-detection.
  Initial DB has one entry, `deep_forest`, seeded from the user's
  session bbox. The actual 84-track DB lands in Phase 7.

Config keys `coach.car_class` and `coach.track` mirror the CLI flags.

## System-prompt update

Adds: "If a track-context line is present, use it for prioritisation
(e.g. fast track → coach the exit, technical track → coach the line),
but DO NOT mention the track or corner number in your reply."
Matches the user's hard requirement that the coach never speaks
track names mid-session.

## Tests

13 new tests in `tests/test_phase6.py`:

* `CornerTrace.gear_at_apex` / `rpm_at_apex` / `peak_rpm` /
  `coasting_fraction` / `tire_temps_c` from synthetic packets.
* `TrackDetector` matches Deep Forest at a known interior position,
  caches the choice, gives up after `max_probes` outside.
* `TrackDetector.force()` bypasses auto-detection.
* Async advisor: `on_corner` returns "queued" immediately and the LLM
  call happens on the worker thread.
* Async advisor: drop-newest replaces the pending corner when a fresh
  one arrives mid-call.
* Zero-event corners return "no events" without touching the rate
  limiter (a subsequent real corner still fires).
* Prompt renders track + car + lap-time + tyre + gear/rpm/coasting +
  recent-events fields when present; omits each cleanly when absent.
* `_tyre_state` thresholds (cold/warm/optimal/hot).
* `_format_lap_time` formatting + edge cases.
* Advisor tracks best-lap across calls (only updates on faster lap).

All pre-Phase-6 Advisor tests were migrated to `async_mode=False` so
the synchronous code path stays exercised. Totals: 104 tests pass,
1 skipped (pyttsx3-on-Linux).

## Real-world impact

The user's first post-Phase-6 live run (44 corners, 76 events,
Deep Forest Gr.3):

| Symptom | Before Phase 6 | After Phase 6 |
|---|---|---|
| Phantom "corner #N: 22.50 s, 29 frames" from UDP backlog | yes, repeated | gone |
| Coach utterance latency in steady state | 2-22 s | 1-3 s |
| Coach pile-up during slow Gemini calls | yes, queued | drop-newest, no pile-up |
| Empty-corner cooldown waste | 19/44 = 43% | 0 |
| Prompt richness | corner shape + 3 events + recent advice | + track context + car + lap + tyres + gear/RPM/coasting + recent fault pattern |

The slow-Gemini path also stopped corrupting the session log — without
async, a stuck call meant tail packets were never written. With async,
the receiver keeps streaming regardless.

## Known limitations carried to Phase 7

* `coach.jsonl` shows `advice_spoken: 0` even when the coach was
  clearly talking. Root cause is the async worker's final result never
  reaches the logger (the receive loop sees only the synchronous
  "queued" stub). Fix is the Phase-7 `on_result` callback.
* The 1-track DB needs to become a real DB.
* Corner #25-style slide-outs (wheels skating without much rotation)
  weren't catching the rotation-only spin trigger. Slide detection
  lands in Phase 7.
* "Clean" corners (19/44 of the session) get nothing said about them —
  the user wanted positive feedback when nothing went wrong.

## How to test

```powershell
git pull
git checkout feat/phase-6-async-and-context
.\.venv\Scripts\python.exe -m pip install -e ".[dev,gemini,voice]"
.\.venv\Scripts\gt7coach-coach.exe --config config.yaml --summary `
    --car-class "Gr.3 RWD"
```

Expected vs the last live session:

* No "corner #N: 22.50 s, 29 frames" phantom corners.
* Coach utterances arrive within 1-3 s of corner exit in the steady state;
  on a transient slow Gemini call the next corner's advice may drop
  instead of stacking up.
* `coach.jsonl` user prompts contain new fields: track-context line,
  `Car:`, `Lap N of M`, `tires: FL warm...`, `gear/RPM @ apex`,
  `coasted X%`, `Recent corners:` block.
* Empty corners (zero events) are listed in `events.jsonl` but not in
  `coach.jsonl` (no LLM call attempted).
* Session summary keeps its 3-5 sentences and is delivered after the
  worker drains.
