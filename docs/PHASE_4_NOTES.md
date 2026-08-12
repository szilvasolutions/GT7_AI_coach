# Phase 4 — polish

Implements the remaining spec items: the five detectors that were carried
over from §6, the post-session LLM summary (§9), the Piper voice engine
(§8), and the YAML config loader (§10).

## What landed

### Five new detectors

| Detector | Spec wording | Implementation summary |
|---|---|---|
| `braking.trail_off_too_fast` | "brake release rate > threshold while still cornering" | per-tick `d(brake)/dt`, gated to `\|lat_g\| > 0.60` |
| `steering.oversteer` | "rear_slip - front_slip > 0.15 for >0.3s mid-corner" | rear/front rps ratio > 1.15 under load (mirror of understeer) |
| `throttle.sawing` | "throttle direction changes >=4 times in 1.5s" | sliding 1.5 s window over signed throttle deltas (deadband 15) |
| `throttle.early_lift` | "throttle < 50 with G-load still > 1.0 mid-corner" | streak of throttle<50 frames under load >= 0.25 s |
| `line.late_apex` | "min speed point occurs after geometric apex" | apex proxied by argmax(\|steer_angle\|); min_speed_idx - apex_idx > 0.30 s |

All five carry the same config + severity / evidence shape as the Phase-2
detectors. Tyre radius is still not used (bug §11.11 stays fixed); ratios
of wheel RPS sidestep the need.

### Session summary (`gt7coach.session.summarizer`)

`aggregate(session_dir)` reads `events.jsonl` and returns a typed
`SessionStats`. `summarise(session_dir, provider=...)` builds a short
user message (driver style + event counts + averages) and asks the
provider for a 3-5 sentence debrief. The prompt + response are saved as
`summary.txt` + `summary_prompt.txt` alongside the rest of the run.

CLI: opt-in via `--summary`, opt-out via `--no-summary`, default driven
by `session.generate_summary` in `config.yaml` (true by default).

### Piper voice engine (`gt7coach.voice.piper_engine`)

Optional offline neural TTS. Two install paths supported:

* Python: `pip install 'gt7coach[piper]'` (pulls `piper-tts` +
  `simpleaudio`), then drop a `.onnx` voice model into `./voices/` or
  `~/.gt7coach/voices/`.
* CLI: install a system `piper` binary, point at it with
  `--voice piper` and a model path.

Falls back gracefully if neither is available — `RuntimeError` at
startup with a clear message instead of silent breakage.

### YAML config loader (`gt7coach.config`)

`config.example.yaml` was always shipped but nothing read it. Now:

* `gt7coach-coach --config <path>` reads YAML and merges it over
  defaults. `./config.yaml` is auto-detected if present.
* Sections covered: `network`, `coach`, `voice`, `detectors.enabled`,
  `detectors.thresholds`, `session`.
* Precedence: defaults < YAML < explicit CLI flag.
* PyYAML is already a base dep, so no extra install.

### Tests

15 new tests in `tests/test_detectors_phase4.py`:

* one positive + one negative per new detector
* `aggregate()` builds the right stats from a synthetic `events.jsonl`
* `summarise()` writes `summary.txt` + `summary_prompt.txt`
* default config enables all 9 detectors
* YAML override changes all the right fields
* missing config file falls back to defaults

55 total tests passing, 1 skipped (pyttsx3 not on Linux CI host).

## What the new detectors find on the user's first capture

```
$ gt7coach-coach --source captures/capture_20260511_150824.csv \
                 --provider mock --voice null --summary
corner #1: 3.66s entry=94->min=93->exit=94 km/h peak=1.40g events=1
corner #2: 6.08s entry=280->min=68->exit=68 km/h peak=1.47g events=3
corner #3: 6.16s entry=217->min=9->exit=9 km/h peak=1.23g events=3
```

Events per corner (from `events.jsonl`):

* **Corner #1** (pre-race / staging): `throttle.early_lift` sev=1.00
  — false positive caused by the duplicate-content packets noted in
  Phase 1. Real fix is to dedup pre-race ticks in the receiver.
* **Corner #2** (heavy braking zone): `throttle.sawing` 0.25,
  `throttle.early_lift` 0.65, `throttle.early_lift` 0.37 — the
  controller-vs-wheel handling mismatch the user mentioned ("I usually
  play with a wheel") shows up as throttle modulation issues.
* **Corner #3** (the lockup / slide): `braking.lockup` 0.88,
  `throttle.sawing` 0.25, `line.late_apex` 0.78 — exactly the slide-out
  the user described.

Phase 4 thus turns the same capture from 1 event into 7 events with
specific diagnoses.

## Known issues / things worth iterating on

* **Replay-mode rate-limiting is too aggressive.** The 4 s cooldown
  uses `time.monotonic()`, and corners replay in milliseconds, so only
  the first event in a replayed CSV fires advice. Live mode is fine.
  Fix: pass `packet.recv_time` as the advisor's `now` so cooldowns track
  game time, not wall time. ~5 line change, not done yet.
* **Pre-race duplicate-content frames still register as a corner.**
  Same root cause as the corner #1 false positive above. Filter
  byte-identical consecutive packets in the receiver / replay.
* **Sawing detector is sensitive to controller jitter.** The default
  `min_delta=15` dead-band is conservative; users with wheels may want
  to lower it.

These are now the user-testing checklist for the next pass.

## How a user runs Phase 4

```bash
git fetch && git checkout feat/phase-4-polish
pip install -e ".[dev,gemini,voice]"

# 1) Replay the existing capture with all 9 detectors + LLM debrief
gt7coach-coach --source ./sessions/capture_*.csv \
               --provider gemini --voice pyttsx3 --summary

# 2) Live with config.yaml overrides
cp config.example.yaml config.yaml   # edit if you want
gt7coach-coach --config config.yaml --summary
```

Check `sessions/run_<ts>/coach.jsonl` for the AI audit log;
`sessions/run_<ts>/summary.txt` for the post-session debrief.
