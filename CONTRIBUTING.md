# Contributing

Thanks for taking a look. Patches, bug reports, and CSV traces are all
welcome — there's nothing to sign, just match the conventions below.

## Dev environment

```bash
git clone https://github.com/szilvasolutions/GT7_AI_coach.git
cd GT7_AI_coach
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,all]"   # everything, including all providers + voice
```

Python 3.11+ is required.

The full pipeline runs on **CSV replay** — you do not need a PS5 to
develop. Tests live in `tests/`, fixtures in `tests/fixtures/`, a real
sample run is at `examples/sample_session/`.

## Run the tests

```bash
ruff check .                # lint
ruff format --check .       # formatter (use `ruff format .` to auto-apply)
pytest                      # full test suite
pytest -v --tb=short        # when you want to see what ran
pytest tests/test_phase7.py # one file
```

CI runs the same three commands. A passing local run is what we expect
before opening a PR.

The one expected skip is `tests/test_voice.py::test_pyttsx3_speaks` on
machines without `pyttsx3` installed — install the `[voice]` extra to
exercise it.

## Repo conventions

* **Branch naming.** `feat/<topic>`, `fix/<topic>`, `docs/<topic>`.
  Phase-scoped work lives on `feat/phase-N-<topic>`. `main` is shippable.
* **Commit messages.** Imperative subject, body explains the *why* not
  the *what*. Look at recent commits on `main` for the house style.
  Multi-line bodies are encouraged when a fix has a non-obvious motive.
* **No secrets in commits.** `.env` is gitignored; keep keys there.
* **Type hints on public APIs.** Internal helpers can stay loose.
* **Don't import from `legacy/`.** It's there as a historical reference,
  not a dependency.

## Adding a new detector

Detectors are pure functions over a `CornerTrace`. The trace exposes
`packets`, derived stats (`peak_lat_g`, `min_speed_kmh`, `corner_type`,
`gear_at_apex`, `rpm_at_apex`, ...), and the underlying packet stream so
you can compute whatever new metric you need.

Minimum viable detector:

1. **Pick a type prefix.** `braking.*`, `throttle.*`, `steering.*`,
   `line.*` are existing buckets. New buckets are OK if the behaviour
   doesn't fit. Positive-feedback events go under `quality.*` and bypass
   the `min_severity` gate.
2. **Write it next to siblings.** A `throttle.surge` event goes in
   `src/gt7coach/detectors/throttle.py`. A whole new bucket gets its
   own module.
3. **Shape:**
   ```python
   from dataclasses import dataclass

   from gt7coach.detectors.base import Event
   from gt7coach.detectors.corner import CornerTrace


   @dataclass(slots=True)
   class FooConfig:
       threshold: float = 0.5
       min_duration_s: float = 0.3


   def detect_foo(trace: CornerTrace, *, config: FooConfig | None = None) -> list[Event]:
       cfg = config or FooConfig()
       events: list[Event] = []
       # ... walk trace.packets ...
       if found:
           events.append(Event(
               type="bucket.foo",
               severity=...,           # 0..1
               t_offset=...,           # seconds from corner entry
               evidence={"peak_x": ..., "duration_s": ...},
           ))
       return events
   ```
4. **Re-export from `src/gt7coach/detectors/__init__.py`.** Add it to
   the imports + `__all__`.
5. **Wire into `main._run_detectors`.** One line. Order matters only if
   you're adding a positive-feedback detector — it should run last and
   take `other_events=events` so it can stay silent when something
   already fired.
6. **Add a canned fallback in `_FALLBACK_PHRASES`** (`coach/advisor.py`)
   so the coach still speaks when the LLM is down.
7. **Test it.** Add a `tests/test_<phase>.py` case with synthetic
   packets (use `tests._synth.make_packet`). Cover at least the
   positive trigger and one negative case so you've thought about
   false positives.

A real, small example to imitate: `detect_clean_corner` in
`src/gt7coach/detectors/quality.py` (~60 lines including config).

## Adding a new LLM provider

Providers implement the `CoachProvider` protocol in
`src/gt7coach/coach/providers.py`:

```python
class CoachProvider(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...
```

That's it. The advisor builds the prompt; you take two strings and
return one. Throw `ProviderError` for anything LLM-side that fails — the
advisor will speak the canned fallback when it sees one.

Steps:

1. **Add a class to `providers.py`.** Class name ends in `Provider`. SDK
   imports go inside `__init__` so users without your provider don't pay
   the import cost.
2. **Define `DEFAULT_MODEL`.** A reasonable production default for the
   account types most users have.
3. **Wire `make_provider`.** Add a branch to the dispatch at the bottom
   of the file.
4. **Register the env var** in `_PROVIDER_ENV` in
   `src/gt7coach/main.py` so the auto-pick logic finds your key.
5. **Add an optional-extra** in `pyproject.toml`:
   ```toml
   yourprov = ["yourprov-sdk>=1.0"]
   ```
6. **Add CLI choice** in `main.py`'s `--provider` argparse.
7. **Test it** — copy one of the existing provider tests in
   `tests/test_coach.py` (mock the SDK, assert `complete()` returns
   the model's text).

`MockProvider` (same file) is the easiest reference; it's ~15 lines and
gets used everywhere in tests.

## Adding a new voice engine

Implement `VoiceEngine` in `src/gt7coach/voice/base.py`:

```python
class VoiceEngine(Protocol):
    def speak(self, text: str) -> None: ...
    def interrupt(self, text: str) -> None: ...
    def is_idle(self) -> bool: ...
    def stop(self) -> None: ...
```

`interrupt` is what `LapTracker` calls — it should preempt any queued
utterance and speak the new text immediately. Put the engine in
`src/gt7coach/voice/<engine>_engine.py` and wire it into `make_voice`.

## What we won't merge

* Mock-only "tests" that don't actually exercise behaviour.
* Features the original [`ARCHITECTURE.md`](./ARCHITECTURE.md) marked
  out of scope — overlay GUIs, reference-lap comparison, multi-game
  support, web dashboards. Read [`ARCHITECTURE_LIVE.md`](./ARCHITECTURE_LIVE.md)
  to see where we've already diverged (and why).
* PRs that add new dependencies without a strong reason. The pipeline
  is meant to run on a laptop, ideally offline.
* Anything that compromises the local-first guarantee. Telemetry stays
  on the user's machine — only event summaries go to the LLM.

## Questions

Open a GitHub issue. Bug reports are most useful with the
`sessions/run_*/` directory attached (telemetry.csv + events.jsonl +
coach.jsonl + meta.json) — that's enough to reproduce most things
offline.
