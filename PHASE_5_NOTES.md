# Phase 5 — truncation fix + smarter coach

Two shippable pieces. The first (truncation fix) is already on its own
branch and is the user-blocking change; this branch adds it plus the
intelligence improvements on top.

## Branch 1: `fix/truncation-and-summary-tokens`

Per-corner advice was being clipped mid-sentence ("Carry", "Settle the",
"Ease the steering") and the post-session summary came back as just
"Alright". Root cause: Gemini 2.5 emits internal *thinking* tokens
before the visible answer, and those tokens count against
`max_output_tokens`. We were passing 64; thinking ate them all.

Fix:

* `GeminiProvider` now passes `thinking_config=ThinkingConfig(thinking_budget=0)`
  inside `GenerateContentConfig` — the model emits the answer
  immediately, lower latency too.
* `CoachProvider.complete(system, user, *, max_tokens=None)` — every
  provider now accepts a per-call override. Default raised from 64 to
  200 (the constant `DEFAULT_MAX_TOKENS`).
* `session.summarizer.summarise(..., max_tokens=1024)` — the post-
  session debrief now has enough headroom for 3-5 sentences.

## Branch 2: `feat/phase-5-smarter-coach` (this branch)

Includes the truncation fix above PLUS:

### `corner_type` classification

`CornerTrace` exposes new properties:

* `total_yaw_deg` — integrated `|yaw_rate| × dt` over the trace, in degrees.
* `yaw_sign_flips` — number of times the yaw direction reverses (with a
  small deadband). One flip = chicane-style.
* `corner_type` — one of:
  * `chicane` (1+ yaw sign flips)
  * `hairpin` (min_speed < 65 km/h AND total_yaw > 100°)
  * `fast_corner` (min_speed >= 160 km/h AND yaw < 60°)
  * `sweeper` (min_speed >= 100 km/h AND duration > 4 s)
  * `slow_corner` (min_speed < 100 km/h)
  * `medium_corner` (default)

The classifier feeds into the LLM prompt so the coach can frame advice
differently for a hairpin vs a sweeper.

### Force-split long corners

`CornerSegmenter` now has `max_corner_duration_s` (default 8.0). When
the running buffer exceeds the cap it splits at the cleanest available
point inside the buffer:

1. Latest steering zero-crossing (chicane / S-curve apex).
2. Otherwise the local minimum speed point (apex of a single corner).
3. Otherwise just the most recent packet.

The tail of the split is kept as the seed of the next corner, so no
data is lost. Set `max_corner_duration_s=0` to disable.

Real-world impact on the user's 14-corner session:

| Before | After |
|---|---|
| corner #3: 10.14 s (243 → 0.5 km/h crash) | split into #3 (7.06 s) + #4 (3.05 s) |
| corner #12: 18.77 s (whole section) | split into #15 (7.27 s) + #16 (6.34 s) + #17 (5.12 s) |
| 14 corners total | 18 corners total |

Each split is a coachable unit with focused events instead of a noisy
average across a whole section.

### Top-3 events per corner + dedup

The advisor previously sent only the single highest-severity event to
the LLM. Long corners (12-14 events!) were always diagnosed as the
same event type, which is why the spoken advice kept saying "Carry".

Now `_top_events()` sorts by severity, dedupes by `event.type`, and
returns the top 3. So if a corner has 5 wheelspin events plus an
oversteer plus a late_brake, the LLM sees one wheelspin (the worst),
one oversteer, and one late_brake. That gives it enough context to
identify the root cause rather than just the loudest symptom.

The advisor still picks the absolute-highest event for the rate-limit
key + the `chosen_event` field (logging continuity); the prompt just
gets richer context.

### Recent-advice memory

Advisor keeps a `deque[(event_type, advice_text)]` of the last 3
utterances. They're surfaced to the LLM in a "Recent advice (DO NOT
repeat verbatim, vary your verb)" block. The system prompt also tells
the model to vary the opening verb across consecutive corners. Result:
the model can stop opening every advice with "Carry…".

### Strengthened system prompt

Added:

* Explicit list of allowed opening verbs (Brake, Trail, Ease, Hold,
  Wait, Open, Carry, Add, Lift, Settle, Roll, Square, Straighten,
  Smooth, Apply, Release, Unwind, Patience).
* Rule: when multiple events fire for one corner, treat them as
  symptoms of one underlying mistake. Coach the root cause once.
* Rule: vary opening verbs across consecutive corners (the prompt
  shows the model what it just said).
* 4 good-example responses and 4 bad-example responses (sentence
  fragments, label echoes, filler).

### Tests

13 new tests in `tests/test_phase5.py`:

* `corner_type` returns hairpin / fast_corner / sweeper / chicane /
  slow_corner correctly.
* Force-split produces ≥ 2 segments at the steering zero-crossing
  when the cap is hit.
* Disabling the cap (`max_corner_duration_s=0`) preserves the legacy
  single-segment behaviour.
* `_top_events` dedupes by type and keeps the highest-severity copy.
* `_top_events` respects the `n` cap.
* Prompt builder includes `corner_type` + `total_yaw_deg` + recent
  advice when present.
* Prompt omits the recent-advice block when empty.
* Advisor passes top-3 distinct types to the provider.
* Advisor records utterances and feeds them back on the next call.

Total: 78 tests pass, 1 skipped (pyttsx3-on-Linux).

## LLM matrix (Q5 answered)

| Model | Latency | Cost in/out per M tok | Quality | Notes |
|---|---|---|---|---|
| **gemini-2.5-flash** (thinking OFF, our default) | ~500 ms | $0.15 / $0.60 | excellent | best balance after the truncation fix |
| gemini-2.0-flash | ~400 ms | $0.10 / $0.40 | very good | safe fallback; no thinking system |
| gemini-2.5-flash-lite | ~350 ms | $0.075 / $0.30 | good | what legacy V23 used; lighter |
| claude-haiku-4-5 | ~600 ms | $1.00 / $5.00 | excellent | recommended when user has Anthropic key |
| gpt-4o-mini | ~700 ms | $0.15 / $0.60 | good | OK fallback |
| ollama llama3.1:8b (local) | varies | free | OK | works offline; less consistent on imperatives |

## Q-by-Q recap

1. **Q: Physics only vs all telemetry?** Physics only (the spec's core
   architectural decision; raw telemetry was the legacy bug). But we
   now send the **top 3 distinct event types** per corner instead of
   just one, plus a `corner_type` tag derived from the trace.
2. (Same as Q1.)
3. **Q: How long should it talk?** Live advice: 4-12 word imperative,
   one sentence (1-2 s spoken). Summary: 3-5 sentences (~30-60 s
   spoken), `max_tokens=1024`. Voice-busy gate prevents pile-ups; the
   rate limiter prevents same-type spam.
4. **Q: How big should corners be?** Capped at 8 s by default with
   smart-split. Configurable via `detectors.thresholds` (extend the
   YAML once you want to expose it; the default works well on the
   user's data). Naturally classifies into hairpin / sweeper / etc.
5. **Q: Which LLM?** Gemini 2.5 Flash with thinking off is the
   recommended default; matrix above lists alternatives.

## How to test

```powershell
git pull
git checkout feat/phase-5-smarter-coach
.\.venv\Scripts\python.exe -m pip install -e ".[dev,gemini,voice]"
.\.venv\Scripts\gt7coach-coach.exe --config config.yaml --summary
```

Expected vs the user's last live run:

* Every advice utterance is a full sentence, not a fragment.
* Successive corners with the same dominant event type get phrased
  differently (the model sees the recent advice).
* No "corners" longer than ~8 s — the 18 s section that used to merge
  into one trace is now 3 separate coachable pieces.
* `coach.jsonl` shows the prompt now includes `corner_type` (e.g.
  `hairpin`, `chicane`) plus a `Recent advice` block.
* Session summary is 3-5 sentences, not "Alright".
