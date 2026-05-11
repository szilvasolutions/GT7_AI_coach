# GT7 AI Coach — v1 Architecture & Specification

> Project brief for v1 rewrite. Treat this document as the source of truth.
> When in conflict with anything else, this wins.

## 1. What this project is

A real-time AI driving coach for **Gran Turismo 7** on PS4/PS5. Consumes the
unofficial UDP telemetry stream (Salsa20-encrypted, port 33739/33740), detects
specific driving events with physics, and uses an LLM to translate those events
into short, spoken coaching feedback delivered through TTS on the user's PC.

## 2. What this project is NOT

- Not a Coach Dave Delta competitor (they have pro drivers, leaderboards, setups)
- Not a SaaS product (no license server, no Stripe, no subscriptions)
- Not a closed-source product (open source, Apache 2.0)
- Not a multi-game tool (GT7 only for v1; the detector layer could later
  generalize, but don't over-engineer for that now)
- Not real-time inside the lap in the trophi.ai sense (we coach corner-by-corner
  with ~1s latency, not mid-corner)

## 3. Design principles

1. **Physics detects events; LLM translates them.** The LLM is never given raw
   telemetry tables and asked "what's wrong." Detectors find specific events
   (wheelspin, late brake, understeer) using physics/heuristics, and the LLM
   only converts a detected `Event` into spoken English. This eliminates the
   "AI always talks about throttle" failure mode of the legacy script.
2. **Local-first.** Telemetry never leaves the user's machine. Only the
   detected event summaries are sent to the LLM provider of the user's choice.
3. **BYO API key.** No proxy server. The user's key, the user's quota.
4. **Multi-provider LLM.** Anthropic, OpenAI, Gemini, Ollama. Provider is a
   pluggable interface.
5. **Replay-driven development.** Every detector and coaching path is
   testable against recorded session CSVs. A live PS5 is never required to
   develop or test. CI runs replay tests.
6. **No mystery.** Logs are structured. Every spoken utterance has a trace
   back to the events that triggered it.

## 4. Module layout

```
gt7coach/
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md          # this file
├── LICENSE                  # Apache 2.0
├── .env.example
├── config.example.yaml
├── .gitignore
├── src/gt7coach/
│   ├── __init__.py
│   ├── telemetry/
│   │   ├── receiver.py      # UDP socket, heartbeat, auto-discovery
│   │   ├── decrypt.py       # Salsa20
│   │   ├── packet.py        # struct unpack → Packet dataclass
│   │   └── replay.py        # read CSV, emit packets at recorded timing
│   ├── detectors/
│   │   ├── base.py          # Detector protocol, Event dataclass
│   │   ├── corner.py        # segmentation state machine
│   │   ├── braking.py
│   │   ├── steering.py
│   │   ├── throttle.py
│   │   └── line.py
│   ├── coach/
│   │   ├── providers.py     # Anthropic / OpenAI / Gemini / Ollama
│   │   ├── advisor.py       # Event → natural-language advice
│   │   └── rate_limiter.py
│   ├── voice/
│   │   ├── base.py          # VoiceEngine protocol
│   │   ├── pyttsx3_engine.py
│   │   └── piper_engine.py
│   ├── session/
│   │   ├── logger.py        # async buffered writer
│   │   └── summarizer.py    # post-session LLM summary
│   └── cli.py               # entrypoint
├── legacy/                  # sanitized old scripts for reference (DO NOT IMPORT)
└── tests/
    ├── fixtures/            # recorded CSV sessions
    └── test_*.py
```

## 5. Telemetry module

### Packet structure

GT7 sends 296-byte encrypted UDP packets to whichever IP sent a heartbeat to
port 33739. Decrypt with Salsa20 using the documented key/IV scheme. Parse into
a typed `Packet` dataclass with at minimum:

- `speed_mps` (float, m/s)
- `accel_long`, `accel_lat` (floats, m/s²) — **measured**, from the packet
- `yaw_rate` (float, rad/s)
- `steer_angle` (float, rad) — **extract this, do not hardcode to 0**
- `throttle` (uint8, 0-255)
- `brake` (uint8, 0-255)
- `gear` (uint8)
- `rpm` (float)
- `wheel_speed_fl/fr/rl/rr` (floats, rad/s)
- `tyre_temp_fl/fr/rl/rr` (floats, °C)
- `pos_x/y/z` (floats)
- `lap_count`, `lap_time_ms`
- `packet_id` (for ordering/loss detection)
- `recv_time` (float, monotonic clock at receipt)

Use struct offsets from reference projects — verify against:
- snipem/gt7dashboard
- vwhitteron/gt-telemetry
- MacManley/gt7-udp
- `granturismo` package on PyPI

### Receiver

- Bind UDP on local port 33740
- Send heartbeat byte `b'A'` to PS5:33739 every ~10 seconds (game disconnects
  at 16s silence; legacy used 10s, keep that)
- **Auto-discovery**: subnet broadcast `255.255.255.255:33739` first (per
  vwhitteron). If broadcast fails or returns nothing in 3s, fall back to
  subnet scan (legacy V58 behavior). User can override via config.
- Emit `Packet` objects to subscribers via a simple pub/sub or asyncio queue.

### Replay

- `replay_csv(path) -> Iterator[Packet]` reads a session CSV and re-emits
  packets at their recorded inter-arrival timing (or as fast as possible in
  test mode).
- Replay must be bit-identical to live for downstream code. This is what makes
  the whole pipeline testable.

## 6. Detector layer (the core architectural insight)

Each detector is a pure function over a corner trace, returning zero or more
`Event` objects. **Detectors never call the LLM.** They use physics and
heuristics.

```python
@dataclass
class Event:
    type: str               # e.g. "wheelspin_on_exit"
    severity: float         # 0..1
    t_offset: float         # seconds from corner entry
    evidence: dict          # supporting metrics (peak slip, g-load, etc.)
```

### Detectors to ship in v1

| Detector | Trigger condition |
|---|---|
| `corner.segment` | brake > threshold OR \|lat_g\| > threshold, with hysteresis + min dwell 0.5s |
| `braking.late_brake` | peak brake force occurs after turn-in begins |
| `braking.lockup` | wheel speed → 0 while car speed > 30 km/h |
| `braking.trail_off_too_fast` | brake release rate > threshold while still cornering |
| `steering.understeer` | front_slip - rear_slip > 0.15 for >0.3s mid-corner |
| `steering.oversteer` | rear_slip - front_slip > 0.15 for >0.3s mid-corner |
| `throttle.wheelspin` | rear wheel speed > car speed * 1.10 with throttle > 100 |
| `throttle.sawing` | throttle direction changes ≥ 4 times in 1.5s |
| `throttle.early_lift` | throttle < 50 with G-load still > 1.0 mid-corner |
| `line.late_apex` | min speed point occurs after geometric apex (requires position) |

Severity is computed per detector. Threshold values live in `config.yaml` so
users with different cars/skill levels can tune.

### Why this matters

The legacy script handed the LLM 6+ seconds of telemetry rows and asked for
advice. Two different corners (a hairpin and a chicane) both got "brake earlier,
smooth throttle" — because the LLM was pattern-matching to generic racing
platitudes. In v1, the LLM only sees events that physics actually detected, so
it can't talk about throttle if there was no throttle event.

## 7. Coach layer

### Provider abstraction

```python
class CoachProvider(Protocol):
    def advise(
        self,
        events: list[Event],
        context: CornerContext,
        driver_style: str,
    ) -> str: ...
```

Implementations:
- `AnthropicProvider` (default; use claude-haiku-4-5 for latency)
- `OpenAIProvider`
- `GeminiProvider` (legacy compatibility)
- `OllamaProvider` (local Llama / Qwen — for users who don't want to pay)

### Prompt strategy

The prompt is built from the detected events, not raw telemetry. Example input:

```
Driver style: smooth
Corner: hairpin (peak 1.4G lat, min speed 61 km/h, duration 4.2s)
Detected events:
  - late_brake (severity 0.7): peak brake force 0.8s into corner
  - throttle.early_lift (severity 0.4): throttle 0% with 1.1G still loaded
Task: ONE imperative coaching sentence, max 12 words, natural speech.
```

### Rate limiter

- At most one advice per corner (the highest-severity event wins)
- Global rate limit: at most one advice every 4s (configurable)
- Drop new advice if the TTS queue is non-empty
- Suppress duplicate advice types within a 30s window

## 8. Voice layer

Plugin protocol. Engines:
- `pyttsx3` — default, works offline, no install pain (recommended for v1)
- `piper` — much better quality, optional install
- `system` — macOS `say`, Linux `espeak`/`spd-say`

Single TTS worker thread, bounded queue (size 2), drop oldest if full.

## 9. Session & replay

### Session logger

- Background async thread, never blocks the receive loop
- Writes: every packet (binary or CSV), every detected event, every coach
  utterance, with monotonic timestamps
- Rotates by size (10MB default) or by lap
- One session per run, named `gt7coach_YYYYMMDD_HHMMSS/`

### Post-session summary

After the user ends a session, optionally call the LLM once with:
- Aggregated event counts by type and severity
- Lap-time deltas (if multi-lap)
- Most-improved and most-degraded areas

Produces a 3-5 sentence summary. This is where LLMs actually shine.

## 10. Config schema

`.env` (gitignored):
```
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=
```

`config.yaml`:
```yaml
network:
  ps5_ip: auto              # or 192.168.1.120
  port_rx: 33740
  port_tx: 33739
  heartbeat_seconds: 10

coach:
  provider: anthropic        # anthropic | openai | gemini | ollama
  model: claude-haiku-4-5
  driver_style: smooth       # smooth | aggressive | learning
  max_advice_per_corner: 1
  global_rate_limit_seconds: 4

voice:
  engine: pyttsx3            # pyttsx3 | piper | system
  speed: 230
  piper_voice: en_US-amy-medium

detectors:
  enabled:
    - corner.segment
    - braking.late_brake
    - throttle.wheelspin
    - throttle.sawing
    - steering.understeer
  thresholds:
    corner_min_speed_kmh: 45
    corner_entry_brake: 65
    corner_entry_lat_g: 0.95
    corner_min_dwell_s: 0.5

session:
  log_dir: ./sessions
  rotation_mb: 10
  generate_summary: true
```

## 11. Bug list (tickets carried over from legacy)

Each of these is a real bug in the V58/V62 scripts that v1 must fix:

1. **CRITICAL**: API keys hardcoded in source → move to `.env`
2. **CRITICAL**: `.gitignore` missing → must include `.env`, `sessions/`, `*.csv`, etc. **before any commit**
3. `steer` extracted as `0` (hardcoded) → extract real value from packet
4. `g_lon` hardcoded `0` → use measured value
5. `g_lat` computed from `(speed * yaw_rate) / 9.81` → use measured value
6. `dt = 0.016` hardcoded → use real time deltas (monotonic clock)
7. Corner state machine bounces (8 entries in 100ms in logs) → hysteresis + min dwell
8. CSV file reopened per packet → single async writer with buffer
9. V58 had subnet auto-discovery; V62 lost it → restore
10. No concurrency limit on LLM calls → rate limiter
11. Tire radius hardcoded `0.33` → per-car table or drop slip metric pending data
12. String-match post-filter (`"brake later" in advice`) → unnecessary once detection is upstream of LLM
13. Driver style baked into prompt template → move to config

## 12. Phased delivery

**Phase 1** (this iteration) — Foundation:
- Repo bootstrap (gitignore, license, pyproject, CI skeleton)
- `telemetry/` complete with replay support
- One smoke test: replay a fixture CSV, assert N packets parsed
- No detectors, no coach, no voice yet

**Phase 2** — Detection:
- `detectors/base.py` + `corner.py` + 3 detectors (late_brake, wheelspin,
  understeer)
- Tests: synthetic traces produce expected events
- Replay test: known-bad fixture produces ≥1 event of each type

**Phase 3** — Coaching:
- `coach/providers.py` (Anthropic first, others stubbed)
- `coach/advisor.py` with prompt template
- `voice/pyttsx3_engine.py`
- End-to-end replay test with mocked LLM

**Phase 4** — Polish:
- Session summary
- Remaining detectors
- Piper voice engine
- README + demo video
- GitHub Actions

## 13. Out of scope for v1

- GUI / overlay
- Reference-lap comparison (Coach Dave's moat; don't fight them)
- Setup recommendations
- Multi-game
- Per-track corner naming
- Web dashboard

## 14. Testing strategy

- **Unit**: each detector against synthetic traces
- **Integration**: replay a fixture, assert detected events match a golden file
- **End-to-end**: replay + mocked LLM provider + null voice engine
- **CI**: runs unit + integration on every PR; no live PS5 needed
- Fixtures live in `tests/fixtures/` — copies of the user's existing CSVs

## 15. Repo conventions

- Python 3.11+
- `ruff` for lint and format
- `pytest` for tests
- Type hints required on public APIs
- No commits with secrets, period (use `pre-commit` with `gitleaks`)
- Branch model: `main` is shippable, work on `feat/*`, `fix/*`

## 16. License & openness

- Apache 2.0
- No CLA
- Credit reverse-engineering pioneers (Nenkai, tarnheld, snipem, ddm999) in README
- Welcome PRs but no obligation to merge
