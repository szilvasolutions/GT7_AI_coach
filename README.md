# GT7 AI Coach

Open-source real-time AI driving coach for **Gran Turismo 7** on PS4 / PS5.

The coach reads the unofficial UDP telemetry stream from the console, detects
specific driving events (late braking, understeer, wheelspin, slides, clean
corners, ...) with physics, and uses an LLM of your choice to translate those
events into short, spoken coaching feedback.

It talks to you while you drive, calls out spins with a dry one-liner, gives
you a 1-2 sentence verdict at the start/finish line of every lap, and writes
a per-corner audit log you can review afterwards.

## What it does today

* **9 corner detectors** — late_brake, lockup, trail_off_too_fast, wheelspin,
  sawing, early_lift, understeer, oversteer, late_apex.
* **Incidents** that interrupt mid-corner advice — spin, slide, crash — each
  with its own sarcastic prompt + canned fallbacks.
* **Positive feedback** — a `quality.clean_corner` event fires when you nail
  a high-g corner with no mistakes, and the coach compliments you.
* **Lap-end summary** spoken at the start/finish line: personal best,
  delta-to-best, or "tidy up that throttle next lap".
* **84-track database** with polylines, bbox prefilter, and sequence-based
  detection so the AI knows whether you're at Suzuka or Deep Forest without
  you having to type anything. Vendored from public sources, see Credits.
* **Async LLM worker** with drop-newest semantics — a slow Gemini call never
  stalls telemetry processing or backs up a queue of stale advice.
* **Duration-aware cue timing** — the coach estimates how long each line
  takes to say and holds it until it can *finish* at least 2.5 s before the
  next corner's apex, so advice never talks over your braking point. Uses
  the track polyline + your live speed; tune or disable under `cue_timing:`
  in config.yaml.
* **Sequence coaching** — when corners come too fast to coach individually
  (esses, chicane complexes), superseded corners aren't discarded: their
  faults fold into the next line, and the coach gives ONE sentence about
  the pattern across the whole run instead of only the last corner.
* **Per-run session logs** (telemetry CSV, events JSONL, coach JSONL with
  every prompt + response, meta JSON, optional end-of-session debrief).

## Design in one paragraph

Physics detects events. An LLM translates them. The LLM never sees raw
telemetry — it only sees a short structured description of what physics
already found (corner shape, peak g, top events, lap context, car class,
tyre temps, recent fault pattern). This eliminates the "AI always talks
about throttle" failure mode of the original prototype.

## Requirements

* **Python 3.11+**
* A PS4 or PS5 running **GT7** on the same LAN as your PC. Telemetry is
  enabled by entering a race / time trial — the console starts sending UDP
  packets to port `33739` and listens for a heartbeat on `33740`.
* An API key for one of: Anthropic, OpenAI, Gemini. Or run a local
  [Ollama](https://ollama.com) instance for offline coaching.
* Optional: speakers / headphones for spoken advice (otherwise use
  `--voice null` to log advice as text).

Tested on Windows 11. Linux / macOS should work for the recorder and the
replay path; live UDP needs the PS5 to be reachable on the LAN.

## Download (Windows binary)

The easiest path on Windows is the pre-built bundle. No Python, no
git, no command line.

1. Go to the [latest release page](https://github.com/szilvasolutions/GT7_AI_coach/releases/latest).
2. Download `GT7Coach-vX.Y.Z-win64.zip`.
3. Unzip somewhere with write access (e.g. `C:\GT7Coach\`). **Avoid
   `Program Files`** — the in-app updater needs to write into the
   install folder.
4. Double-click `GT7Coach.exe`.
5. On first launch, use **Tools → Configure** to pick a provider and
   paste your API key. Then **Start**.

Windows SmartScreen will warn about an unknown publisher on first
launch — click "More info" → "Run anyway". Code-signing is on the
roadmap. The GUI auto-checks GitHub Releases on launch and offers a
one-click in-app update when a newer build is published.

## Setup — from source (developers)

### 1. Clone and create a virtual environment

```powershell
git clone https://github.com/szilvasolutions/GT7_AI_coach.git
cd GT7_AI_coach
python -m venv .venv
.\.venv\Scripts\activate         # Windows
# source .venv/bin/activate      # macOS / Linux
```

### 2. Install with the extras you need

Pick one provider extra (`anthropic` / `openai` / `gemini` / `ollama`) and
one voice extra (`voice` for pyttsx3, `piper` for neural TTS).

```powershell
pip install -e ".[dev,gemini,voice]"      # Gemini + pyttsx3 (recommended)
pip install -e ".[dev,anthropic,voice]"   # Anthropic + pyttsx3
pip install -e ".[dev,ollama]"            # local Ollama, no voice
pip install -e ".[dev,gemini,voice,gui]"  # + the desktop GUI (gt7coach-gui)
pip install -e ".[dev,all]"               # everything
```

### 3. Add your API key

```powershell
copy .env.example .env       # cp .env.example .env on macOS / Linux
```

Open `.env` and paste the key for the provider you installed:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=
# GEMINI_API_KEY=AIza...
```

Only the key for your chosen provider is required. The coach auto-picks
whichever provider has a key set if you don't pass `--provider`.

**Privacy note.** Telemetry never leaves your machine. The LLM only ever
sees the short event summary the detectors produced (corner shape, peak g,
top three events, recent advice, lap context). No raw packets, no GPS
beyond track auto-detection, no replay frames.

### 4. (Optional) Tune `config.yaml`

```powershell
copy config.example.yaml config.yaml
```

Defaults are sensible. The most useful overrides are `coach.car_class`
(spliced into every prompt, e.g. `"Gr.3 RWD"`), `coach.track` (force a
track id and skip auto-detection), and `voice.engine`.

Run `gt7coach-list-tracks` to see every available track id:

```powershell
gt7coach-list-tracks --filter forest
# DeepForestRaceway              Deep Forest Raceway              JP   3596 m   15 corners
```

### 5. Confirm the receiver sees the PS5

Start GT7 on the console, enter a race or time trial, then on your PC:

```powershell
gt7coach-capture
```

A live status line should show speed / gear / RPM / pedal positions / lat-g
within a few seconds. If nothing arrives, see the troubleshooting section
below. Ctrl-C stops the capture.

### 6. Run the coach

Live, with auto-discovery of the PS5 IP:

```powershell
gt7coach-coach --car-class "Gr.3 RWD" --summary
```

Live, against a known PS5 IP (faster startup):

```powershell
gt7coach-coach --ip 192.168.1.120 --provider gemini --voice pyttsx3
```

Replay a recorded CSV (no PS5 needed — great for offline testing):

```powershell
gt7coach-coach --source .\sessions\capture_20260511_210417.csv `
               --provider mock --voice null      # dry run, no LLM call
gt7coach-coach --source .\sessions\capture_20260511_210417.csv `
               --provider gemini --voice pyttsx3  # real coach on recording
```

Hit Ctrl-C to stop. The coach drains its in-flight LLM call, speaks any
final advice, and writes the session log to `./sessions/run_<timestamp>/`.

## Useful flags

| Flag | What it does |
|---|---|
| `--provider {anthropic,openai,gemini,ollama,mock}` | Which LLM to use. Defaults to the first one with an API key set. |
| `--model <name>` | Override the provider's default model. |
| `--driver-style {smooth,aggressive,learning}` | Tone of the coaching prompt. |
| `--car-class "Gr.3 RWD"` | Free-form descriptor fed into every prompt. |
| `--track <id>` | Force a track id (see `gt7coach-list-tracks`). |
| `--cooldown <seconds>` | Global rate-limit between coaching utterances (default 4 s). |
| `--voice {pyttsx3,piper,null}` | TTS engine. `null` logs advice without speaking. |
| `--voice-rate 230` | pyttsx3 speech rate (words/min). |
| `--summary` | Generate a 3-5 sentence LLM debrief at end of run. |
| `--source <path-or-pattern>` | Replay a CSV instead of going live. Glob patterns work. |
| `--realtime` | When replaying, recreate inter-arrival timing (default: as fast as possible). |
| `--config config.yaml` | Load defaults from YAML. |
| `--no-log` | Don't write `sessions/run_<timestamp>/`. |
| `-v` | Verbose logging. |

## Output: per-run session log

Each `gt7coach-coach` run writes `sessions/run_<timestamp>/` containing:

| File | Contents |
|---|---|
| `telemetry.csv` | Every packet, replay-compatible with `--source`. |
| `events.jsonl` | Every detected event + the corner trace summary. |
| `coach.jsonl` | Every advisor turn: verbatim system + user prompt and the LLM response. The AI audit log. |
| `meta.json` | Host info, CLI args, totals (`packets`, `corners`, `events`, `advice_spoken`, `incidents`). |
| `summary.txt` + `summary_prompt.txt` | Post-session debrief (only with `--summary`). |

A real session is checked into the repo at
[`examples/sample_session/`](./examples/sample_session/) so you can see the
file format without running anything.

## Capture only (no coaching, no voice)

`gt7coach-capture` records a real PS5 session to disk so the data can be
replayed and analysed offline.

```powershell
gt7coach-capture                          # auto-discover PS5, format B (default)
gt7coach-capture --ip 192.168.1.120       # explicit PS5 IP if broadcast is blocked
gt7coach-capture --duration 60            # auto-stop after 60 seconds
gt7coach-capture --out .\mycaps --format A   # legacy 296-byte format
```

Each capture produces three files in `./sessions/`:

* `capture_<timestamp>.bin` — raw decrypted packets, for offline byte-level analysis
* `capture_<timestamp>.csv` — same schema as the replay loader, feed back via `--source`
* `capture_<timestamp>.json` — metadata: format, host, packet-size histogram, packet rate

## Run the tests

```powershell
pytest
ruff check
```

The full suite runs without any PS5. 195+ tests cover Salsa20 decrypt, packet
parsing, every detector, the corner segmenter, all providers, the rate
limiter, the async advisor worker, the track database, and the lap tracker.

## Troubleshooting

**The PS5 isn't auto-discovered.**
Most consumer routers block UDP broadcast across subnets / VLANs. Pass
`--ip <ps5-address>` explicitly. The PS5 only sends telemetry while
something is happening (race / time-trial / replay), so try entering an
actual race first.

**`provider setup failed: ... API key`.**
Check `.env` is in the directory you ran the coach from, the key has no
quotes / trailing whitespace, and you installed the matching extra
(`gemini` extra for `GEMINI_API_KEY`, etc).

**Coach is silent.**
Verify `--voice pyttsx3` (or `piper`) — `--voice null` writes advice to
`coach.jsonl` only. On Windows, pyttsx3 uses SAPI5 and needs an installed
voice. On macOS / Linux the easiest path is `--voice null` then read the
log.

**Long latency on Gemini free tier.**
The advisor runs the LLM call on a background worker and drops stale
corners when a new one arrives, so even a 20 s call won't back up. If
calls are routinely >5 s, switch to `--model gemini-2.5-flash-lite` or to
Anthropic / Ollama.

## Architecture

See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full design,
[PHASE_1_NOTES.md](./PHASE_1_NOTES.md) through Phase 7 notes for what
shipped in each milestone, and the per-module docstrings in `src/gt7coach/`
for the implementation details.

## Credits

This project would be impossible without the reverse-engineering work of:

* [Nenkai](https://github.com/Nenkai) — original protocol disclosure
  (`PDTools`)
* [snipem](https://github.com/snipem) — `gt7dashboard`
* [vwhitteron](https://github.com/vwhitteron) / [zetetos](https://github.com/zetetos) —
  `gt-telemetry` and the kaitai struct definitions; track polylines used
  in the built-in DB (MIT)
* [ddm999](https://github.com/ddm999) — additional protocol notes and the
  `gt7info` track metadata (course IDs, names, lengths)

See [`src/gt7coach/tracks/data/ATTRIBUTION.md`](./src/gt7coach/tracks/data/ATTRIBUTION.md)
for the track-database specifics.

## License

Apache 2.0. No CLA.
