# Phase 3 — coach + voice + end-to-end CLI

Implements sections 7 (coach) and 8 (voice) of ARCHITECTURE.md, plus the
`gt7coach-coach` CLI that ties Phases 1–3 into one runnable pipeline.

## What landed

### `src/gt7coach/coach/`

* `prompt.py` — single source of truth for system + user prompt. Stable
  system block is marked for Anthropic prompt caching.
* `providers.py` — `CoachProvider` protocol + five concrete providers
  (Anthropic, OpenAI, Gemini, Ollama, Mock) + a `make_provider()` factory.
  Each provider SDK is imported lazily so users only install what they use.
* `rate_limiter.py` — global 4 s cooldown + per-type 30 s dedup window.
  Tested with a fake clock so behaviour is deterministic.
* `advisor.py` — glue: picks the highest-severity event for a corner,
  consults the rate limiter and the voice queue, calls the provider, hands
  the resulting text to the voice. Returns a typed `AdvisorResult` per
  corner so callers can log suppressions.

### `src/gt7coach/voice/`

* `base.py` — `VoiceEngine` protocol + `make_voice()` factory.
* `null_engine.py` — records utterances, never makes a sound. Default for
  tests and `--voice null` dry runs.
* `pyttsx3_engine.py` — offline TTS engine. Single worker thread, bounded
  deque of size 2 with drop-oldest semantics (spec section 8).

### `src/gt7coach/main.py`

The `gt7coach-coach` console script. Accepts either `--source live` (UDP
discovery + heartbeat from Phase 1) or `--source path/to/capture.csv`
(replay from Phase 1). Same pipeline regardless of source.

CLI flags worth knowing:

* `--provider {anthropic,openai,gemini,ollama,mock}` — default anthropic
* `--model <name>` — override the provider's default model
* `--driver-style {smooth,aggressive,learning}` — embedded into the prompt
* `--cooldown <seconds>` — global rate limit (default 4)
* `--voice {pyttsx3,null}` — `null` logs advice without speaking
* `--api-key <key>` — overrides the env var
* `--env-file <path>` — non-standard .env location

### Optional install extras (`pyproject.toml`)

```toml
anthropic = ["anthropic>=0.39"]
openai    = ["openai>=1.40"]
gemini    = ["google-generativeai>=0.7"]
ollama    = ["requests>=2.32"]
voice     = ["pyttsx3>=2.90"]
all       = [...all of the above...]
```

### Tests

36 tests pass, 1 skipped (pyttsx3 not installed on the Linux CI host —
that's expected; it'll run on Windows / macOS where the SDK ships with the
OS). Coverage:

* `test_coach.py`: rate limiter cooldown + duplicate window, prompt builder,
  advisor picks highest-severity event, advisor respects voice-busy state,
  advisor respects rate limiter, advisor returns no-op on `ProviderError`,
  factory creates expected types.
* `test_voice.py`: null engine records, factory dispatches, unknown engine
  raises, pyttsx3 engine is skip-if-missing.

## Design decisions worth knowing

* **Prompt caching on Anthropic.** The system prompt is identical across
  every call, so it's marked `cache_control: ephemeral`. Per-call cost is
  effectively just the per-corner user message.
* **Lazy SDK imports.** Each provider imports its SDK in `__init__`, so a
  user who only wants Gemini doesn't need to install Anthropic.
* **`MockProvider` is part of the package, not just the tests.** Useful
  for `--provider mock` dry runs ("does the pipeline fire?" without
  spending API credits).
* **Voice queue depth is 2.** Pushing into a full queue drops the *oldest*
  pending utterance, not the new one — by the time the third utterance
  exists, the first is stale and not worth speaking. The `Advisor` also
  refuses to even ask the LLM when the voice is busy (spec section 7).
* **The advisor takes `now` as a parameter** for the rate limiter so tests
  don't need to sleep. Live mode just uses `time.monotonic()`.

## What it does on the user's real capture

```
$ gt7coach-coach --source captures/capture_20260511_150824.csv \
                 --provider mock --voice null
corner #1: 3.66s entry=94->min=93->exit=94 km/h peak=1.40g events=0
corner #2: 6.08s entry=280->min=68->exit=68 km/h peak=1.47g events=0
corner #3: 6.16s entry=217->min=9->exit=9 km/h peak=1.23g events=0
```

Same result as the Phase-2 sanity check: 3 corners segmented, 0 events
fired. The user's actual mistake was a front lockup, which is `braking.lockup`
in spec §6 — that detector lands in Phase 4.

## How a user runs this end-to-end

```bash
# 1. Branch with Phase-3 code
git fetch && git checkout feat/phase-3-coach
pip install -e ".[dev,gemini,voice]"

# 2. Configure
cp .env.example .env       # paste GEMINI_API_KEY=...
cp config.example.yaml config.yaml   # optional, not yet wired in

# 3a. Test on the captured CSV without spending API credits
gt7coach-coach --source ./sessions/capture_*.csv --provider mock --voice null

# 3b. Test on the captured CSV with the real LLM and TTS
gt7coach-coach --source ./sessions/capture_*.csv --provider gemini

# 4. Live on the PS5
gt7coach-coach --provider gemini --driver-style smooth
```

## Out of scope (Phase 4)

* Real `config.yaml` loading (thresholds, voice settings, etc.). Today
  these come from CLI flags only.
* `braking.lockup`, `braking.trail_off_too_fast`, `steering.oversteer`,
  `throttle.sawing`, `throttle.early_lift`, `line.late_apex`.
* Piper voice engine, system `say` / `espeak`.
* Session logger + post-session LLM summary.
