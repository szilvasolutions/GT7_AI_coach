# GT7 AI Coach — live architecture

This is the current shape of the codebase, as of Phase 7. For the original
v1 brief see [ARCHITECTURE.md](./ARCHITECTURE.md); for the chronology of
how we got here see the per-phase notes.

## What it does, end to end

```
┌───────────────────┐    ┌─────────────────┐    ┌──────────────────┐
│ PS5 (GT7)         │───▶│ telemetry       │───▶│ CornerSegmenter  │
│ Salsa20 UDP @60Hz │    │ (Receiver/CSV)  │    │ (state machine)  │
└───────────────────┘    └─────────────────┘    └──────────────────┘
                              │                       │
                              ▼                       ▼ (per corner)
                         ┌─────────────────────────────────────────┐
                         │ Per-packet detectors                    │
                         │ - TrackDetector (sequence matching)     │
                         │ - IncidentDetector (spin/slide/crash)   │
                         │ - LapTracker (transition watcher)       │
                         └─────────────────────────────────────────┘
                                                  │
                                                  ▼
                         ┌─────────────────────────────────────────┐
                         │ Per-corner detectors (9 + clean_corner) │
                         │ braking.* | throttle.* | steering.* |   │
                         │ line.* | quality.clean_corner           │
                         └─────────────────────────────────────────┘
                                                  │
                                                  ▼ (events list)
                         ┌─────────────────────────────────────────┐
                         │ Advisor (async worker thread)           │
                         │ - rate limiter + voice-busy gate        │
                         │ - drop-newest pending slot              │
                         │ - LLM provider call (Anthropic / OpenAI │
                         │   / Gemini / Ollama)                    │
                         │ - canned-phrase fallback                │
                         │ - on_result callback → SessionLogger    │
                         └─────────────────────────────────────────┘
                                                  │
                                                  ▼
                                           ┌──────────────┐
                                           │ VoiceEngine  │
                                           │ pyttsx3/piper│
                                           └──────────────┘
```

`SessionLogger` writes telemetry / events / coach JSONL / meta + optional
LLM debrief in parallel, fed by `on_advisor_result` and per-packet hooks.

## Module layout (current)

```
src/gt7coach/
├── telemetry/
│   ├── receiver.py        UDP socket + heartbeat + auto-discovery
│   ├── decrypt.py         Salsa20
│   ├── packet.py          struct unpack -> Packet dataclass
│   ├── replay.py          CSV replay, matches live timing or fastest
│   ├── capture.py         gt7coach-capture entrypoint
│   └── format_b.py / format_c.py
├── detectors/
│   ├── base.py            Detector protocol + Event dataclass + helpers
│   ├── corner.py          CornerSegmenter + CornerTrace + corner_type
│   ├── braking.py         late_brake, lockup, trail_off_too_fast
│   ├── throttle.py        wheelspin, sawing, early_lift
│   ├── steering.py        understeer, oversteer
│   ├── line.py            late_apex
│   ├── quality.py         clean_corner (positive feedback)
│   └── incident.py        spin, slide, crash
├── tracks/
│   ├── database.py        Track + Turn dataclasses + JSON loader
│   ├── detector.py        TrackDetector (sequence matching)
│   └── data/
│       ├── tracks.json    84-track DB (~2 MB)
│       └── ATTRIBUTION.md
├── coach/
│   ├── providers.py       Anthropic / OpenAI / Gemini / Ollama / Mock
│   ├── prompt.py          SYSTEM / SARCASTIC / COMPLIMENT / LAP_SUMMARY prompts
│   ├── advisor.py         Advisor (async worker, on_result callback)
│   ├── laps.py            LapTracker + lap-end summary
│   └── rate_limiter.py
├── voice/
│   ├── base.py
│   ├── pyttsx3_engine.py
│   ├── piper_engine.py
│   └── null_engine.py
├── session/
│   ├── logger.py          SessionLogger (registers on_advisor_result)
│   └── summarizer.py
├── config.py              YAML loader -> LoadedConfig dataclass
├── main.py                gt7coach-coach entrypoint
├── capture.py             gt7coach-capture entrypoint
└── list_tracks.py         gt7coach-list-tracks entrypoint
```

CLIs registered in `pyproject.toml`:

| Script | Purpose |
|---|---|
| `gt7coach-coach` | Full coaching pipeline. Live or CSV replay. |
| `gt7coach-capture` | Recorder only. No coaching, no voice. |
| `gt7coach-list-tracks` | Print the 84-track database. |

## Telemetry layer

The PS5 sends Salsa20-encrypted UDP packets to whichever IP sent a
heartbeat byte. The receiver:

1. Binds local UDP `33740`.
2. Sends a heartbeat (`A` for format A, `~` for format B/C) to PS5:33739
   every 10 s.
3. Auto-discovers via broadcast or falls back to subnet scan if the user
   doesn't pass `--ip`.
4. Decrypts each packet, parses by format (296 byte A / 316 byte B), emits
   a typed `Packet` dataclass with monotonic `recv_time`.

Format B is the default — it carries `steer_angle`, body forces, and the
fields the Phase 6 prompt-context blocks need.

`replay_csv(path)` is bit-identical to the live source for downstream
code. Every test runs on replay; no PS5 is required to develop.

## Detector layer

Events come in two flavours:

* **Per-corner.** `CornerSegmenter` emits a `CornerTrace` (packet
  buffer + derived stats) when the corner state machine closes the
  segment. The trace is fanned out to nine corrective detectors plus
  the positive-feedback `quality.clean_corner` detector. Each returns
  zero or more `Event`s with type + severity + t_offset + evidence dict.
* **Per-packet.** `IncidentDetector` (spin / slide / crash) and the
  positional `TrackDetector` both see every packet. Incidents interrupt
  whatever the coach was about to say and route to a sarcastic prompt.

`CornerTrace` exposes physics aggregates the prompt needs:
`peak_lat_g`, `min_speed_kmh`, `entry_speed_kmh`, `exit_speed_kmh`,
`duration_s`, `corner_type` (hairpin / chicane / sweeper / fast / slow /
medium), `gear_at_apex`, `rpm_at_apex`, `peak_rpm`, `coasting_fraction`,
`total_yaw_deg`, `yaw_sign_flips`, plus tyre temperatures.

### Corner-segmentation state machine

`STRAIGHT → ENTRY → CORNER → EXIT → STRAIGHT`, with hysteresis + min
dwell (default 0.5 s) so the segmenter doesn't bounce. Phase 5 added a
hard cap (`max_corner_duration_s=8.0`) that force-splits long traces at
the cleanest internal point (steering zero-crossing → local min speed →
most recent packet).

### Detectors shipped

| Module | Type prefix | Triggers |
|---|---|---|
| braking.py | `braking.late_brake` / `lockup` / `trail_off_too_fast` |
| throttle.py | `throttle.wheelspin` / `sawing` / `early_lift` |
| steering.py | `steering.understeer` / `oversteer` |
| line.py | `line.late_apex` |
| quality.py | `quality.clean_corner` (positive — bypasses min_severity) |
| incident.py | `spin` / `slide` / `crash` (per-packet) |

Severity is computed per detector; thresholds are tunable.

## Track layer

`tracks.json` is built from `scripts/build_track_db.py`, joining
zetetos's recorded polylines with ddm999's metadata by fuzzy name
match. 84 entries; each carries id, display_name, country, length,
corner count, oval flag, bbox, ~570-point polyline, detected apex
turns, and a templated `shape_description`.

`TrackDetector` keeps a 30-packet position buffer. For every new
packet the buffer is scored against bbox-passing candidates by mean
nearest-polyline distance; the winner must beat both a 40 m absolute
threshold AND outrank the runner-up by ≥ 8 m. Sequence matching is
necessary because the polylines are quantised to a 16 m integer grid,
so single-point matches are ambiguous.

Sticky lock holds the chosen track until the car is ≥ 150 m outside
the bbox for ≥ 3 s (covers garage / pit returns).

`TrackDetector.force(track_id)` bypasses auto-detection for known
tracks (CLI flag `--track`, YAML `coach.track`).

## Coach layer

### Provider abstraction

```python
class CoachProvider(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...
```

Implementations: `AnthropicProvider`, `OpenAIProvider`, `GeminiProvider`
(Gemini 2.5 Flash with thinking disabled by default), `OllamaProvider`,
`MockProvider` (deterministic test responder).

Provider auto-selection (Phase 4): if `--provider` is not set, picks the
first one whose API key is in the environment, otherwise the configured
default, otherwise the first available with no key required
(`ollama` / `mock`).

### Advisor

* **Async worker** (Phase 6). One thread, single `_pending` slot,
  drop-newest semantics. `on_corner()` is non-blocking from the
  receive loop. `flush()` + `stop()` for clean shutdown.
* **Rate limiter.** Global cooldown (default 4 s) + duplicate-window
  suppression by event type (default 30 s).
* **Voice-busy gate.** If TTS is mid-utterance, skip — don't pile up.
* **Top-3 dedupe-by-type** (Phase 5). The LLM sees the highest-severity
  three distinct event types, not just the loudest one. This lets it
  identify root causes instead of symptoms.
* **Compliment routing** (Phase 7). `quality.*` events bypass the
  `min_severity` gate and route to `COMPLIMENT_SYSTEM_PROMPT`.
* **Incident routing.** Per-packet incidents use `SARCASTIC_SYSTEM_PROMPT`
  with type-specific canned fallbacks.
* **on_result callback** (Phase 7). Fires once per final result —
  including from the worker thread. `SessionLogger.on_advisor_result`
  is registered as the callback, so `coach.jsonl` contains real
  responses instead of the synchronous "queued" stub.

### Prompt context

The user prompt is templated, with optional blocks rendered only when
data is present:

```
Driver style: smooth.
Car: Gr.3 RWD.                              ; if set
Track context: <shape_description>.         ; if track detected
Lap 3 of 8, last lap 1:23.456 (+0.7 vs best).  ; if lap data valid
Tires: FL warm, FR warm, RL hot, RR hot.    ; if tyre fields present
Corner: hairpin, peak 1.4g lat, min 48 km/h @ gear 2 / 4500 rpm,
        peak 7200 rpm, coasted 12% of corner, duration 6.5s.
Top events: late_brake (0.7), early_lift (0.4), wheelspin (0.3).
Recent advice: "Brake earlier"; "Ease the throttle"; "Carry minimum speed".
Recent corners: hairpin/late_brake, sweeper/sawing, chicane/late_brake.
```

System prompt restricts to one imperative sentence ≤ 12 words, varies
the opening verb across consecutive corners, forbids track names in
the spoken reply, and lists allowed opening verbs (Brake, Trail, Ease,
Hold, etc.).

### LapTracker

Watches `lap_count` transitions. On `lap_count > last AND lap_count > 1`
(skipping the formation lap), builds a prompt with lap time + delta to
best + accumulated mistake counts and asks the LLM for a 1-2 sentence
verdict. Speaks via `voice.interrupt()` so it preempts stale mid-corner
advice and lands at the start/finish line. Falls back to a canned
summary (personal-best, near-best, dominant-mistake, or clean) when
the LLM errors.

## Voice layer

Plugin protocol with `speak(text)`, `interrupt(text)`, `is_idle()`,
`stop()`. Implementations:

* `pyttsx3` — default; SAPI5 on Windows. Single worker thread, bounded
  queue, drop-oldest if full.
* `piper` — neural TTS, optional install. Better quality, needs a
  voice model on disk.
* `null` — log advice to stderr, never speaks. Useful for replay /
  dry-run / Linux dev.

## Session layer

`SessionLogger` opens `sessions/run_<timestamp>/` and writes:

| File | Source | Format |
|---|---|---|
| `telemetry.csv` | every packet | replay-compatible CSV |
| `events.jsonl` | every corner | one JSON per event + trace summary |
| `coach.jsonl` | every advisor turn | system + user prompt + LLM response, written from `on_advisor_result` |
| `meta.json` | start/end | host info, CLI args, totals |
| `summary.txt` / `summary_prompt.txt` | end | optional LLM debrief (`--summary`) |

The logger is the *only* writer to those files; everything else is read-
only or in-memory. The receive loop never blocks on I/O — writes are
buffered.

## Config

`.env` carries API keys (one of `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
`GEMINI_API_KEY`; Ollama needs no key). `config.yaml` carries everything
else, with CLI flags overriding for any specific run. See
`config.example.yaml` for the full shape and defaults.

## Testing

121 tests, 120 pass + 1 skipped (`pyttsx3` on Linux sandbox). Split by
concern:

| File | Coverage |
|---|---|
| `test_telemetry.py` | Salsa20 decrypt, format A/B parse, replay |
| `test_capture.py` | gt7coach-capture entrypoint |
| `test_detectors.py` | Phase 2 detectors (late_brake, wheelspin, understeer) |
| `test_detectors_phase4.py` | Phase 4 detectors (lockup, trail_off, sawing, early_lift, oversteer, late_apex) |
| `test_incidents.py` | spin / crash with sarcastic prompt + canned fallback |
| `test_coach.py` | provider abstraction, rate limiter, advisor sync path |
| `test_phase5.py` | corner_type, force-split, top-3 dedupe, recent-advice |
| `test_phase6.py` | RPM/gear/coasting properties, async advisor, zero-event skip, prompt blocks |
| `test_phase7.py` | on_result callback, slide trigger, clean_corner, track DB, LapTracker |
| `test_main.py` | CLI wiring, provider auto-pick, glob expansion |
| `test_voice.py` | VoiceEngine protocol, null engine |

All tests run on replay. No PS5 required.

## What changed from the original ARCHITECTURE.md

Items the original spec deferred or marked out-of-scope that are now
live:

* **Track recognition.** Original said "Per-track corner naming" was
  out of scope. Phase 7 ships a real 84-track DB and sequence-matching
  detector. The coach uses the track *shape* for prioritisation but
  never speaks the name — that constraint survived.
* **Async LLM.** Original spec did not call out async — it became a
  hard requirement after Phase 5 because Gemini latency spikes were
  stalling telemetry processing.
* **Lap summaries.** Original had "post-session summary" but not the
  per-lap variant. Phase 7 adds `LapTracker` for spoken between-lap
  verdicts.
* **Incidents.** Original had nine detectors and stopped there. Phase 4
  added a separate per-packet `IncidentDetector` for spins / crashes
  with a different prompt (sarcastic) and the ability to interrupt
  mid-corner. Phase 7 added the slide trigger.
* **Positive feedback.** Original was all-corrective. Phase 7 adds
  `quality.clean_corner` for compliments.
* **`CornerContext` enrichment.** Original prompt was just driver style
  + corner shape + events. Phase 6 added car class, track shape, lap
  delta, tyre state, gear/RPM/coasting, recent fault pattern. Phase 5
  added top-3 events and recent advice.

Items still out of scope and intentionally so: GUI / overlay,
reference-lap comparison (Coach Dave Delta's territory), setup
recommendations, multi-game, web dashboard, per-car tyre optimum,
auto-numbered corners in advice, race-start / checkered-flag
announcements.
