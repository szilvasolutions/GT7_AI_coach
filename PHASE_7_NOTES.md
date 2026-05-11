# Phase 7 — async-result logging, slide detection, clean corners, real track DB, lap summary

Phase 6 fixed the *operational* issues with the LLM pipeline (async,
no pile-up, richer prompt). The post-Phase-6 live run surfaced five
*observability and coverage* gaps that this branch closes.

## 1. Async-result logging via on_result callback

**Problem.** Post-session `coach.jsonl` showed `advice_spoken: 0`
despite the coach clearly talking through the run. Root cause: the
async worker thread's final `AdvisorResult` never made it to the
`SessionLogger`. The receive loop called `session.log_advisor(...)`
synchronously with the "queued" stub that `on_corner` returned, and
nothing ever surfaced the eventual real result. `meta.json[totals]`
was therefore wrong, and post-session review was blind to what the
coach actually said.

**Fix.** `Advisor.__init__` accepts an `on_result` callback
(`Callable[[int, CornerTrace | None, AdvisorResult | IncidentResult], None]`).
`_record()` and `_record_incident()` fire it *after* a result is
finalised — including from the worker thread. The synchronous "queued"
stub no longer calls `_record`, so the callback fires exactly once per
corner with the actual advice + prompt + failure reason.

`SessionLogger.on_advisor_result(corner_idx, trace, result)` is
registered as the callback from `main.py`. It dispatches by result
type (advice vs incident), looks up the cached `CornerTrace` for the
corner_idx, and writes a real record to `coach.jsonl`.
`meta.json[totals]` gains an `incidents` counter.

To make the callback wiring possible, `main.py` now constructs the
`SessionLogger` BEFORE the `Advisor` so it can be passed in.

**Test coverage.** Two cases in `tests/test_phase7.py`:

* `test_on_result_callback_fires_for_async_worker_completion` — kicks
  off a corner with `async_mode=True`, flushes the worker, asserts the
  callback fired exactly once with the right `corner_idx` and the
  worker's actual advice (not the queued stub).
* `test_on_result_callback_fires_for_synchronous_suppressions` —
  below-min-severity suppression in `async_mode=False` still fires
  the callback with `suppressed_reason` populated.

## 2. Slide-out incident trigger

**Problem.** Corner #25 in the user's session was an obvious slide-out
(entry 94 → 0.1 → 36 km/h, wheelspin sev 1.0 peak_ratio 1.9,
oversteer sev 1.0). No incident fired. The existing `IncidentDetector`
only checked rotation: yaw_rate ≥ 2.5 rad/s sustained for 150 ms.
A car skating sideways without much rotation passed it by.

**Fix.** Two changes in `IncidentDetector`:

* `spin_min_duration_s` 150 ms → 80 ms to catch brief 180° spins.
* New "slide" incident fires when ALL of:
  * `rear_avg / front_avg` wheel speed ≥ 1.6 (wheels spinning, not gripping),
  * 25 ≤ current speed ≤ 90 km/h (excludes parking and excludes
    high-speed wheelspin under full-throttle exit which is NOT a slide),
  * `max(speed_in_last_1s) - current_speed` ≥ 30 km/h (the slide cost
    you significant speed).

The detector maintains an 80-entry `(recv_time, speed_kmh)` deque
regardless of cooldown so the drop calculation is always fresh.

The Advisor's `_INCIDENT_FALLBACKS["slide"]` ships five canned quips
("Skating, are we?", "Off-line. Tidy that up.", etc.) so a slide gets
spoken even when the LLM fails.

**Test coverage.** Two cases in `tests/test_phase7.py`:

* `test_slide_incident_fires_on_wheelspin_with_speed_drop` —
  synthesises 90 km/h cruise then 30 km/h with rear/front = 2.0,
  asserts the slide incident fires.
* `test_no_slide_at_full_throttle_high_speed_exit` — 120 km/h with
  1.25 rear/front (above `slide_max_speed_kmh`); asserts no slide.

## 3. quality.clean_corner — positive feedback

**Problem.** 19 of 44 corners in the live session produced zero
detector events. They were *clean*, not neglected. The user wanted
the coach to acknowledge a well-driven corner, not stay silent.

**Fix.** New `gt7coach.detectors.quality.detect_clean_corner(trace,
*, other_events)`. Returns a single `quality.clean_corner` event
(severity 0.15 so it never outranks a real fault) when:

* peak lateral acceleration ≥ 1.2 g (real cornering, not a straight),
* mid-corner throttle held above 50 for ≥ 90% of mid-frames (committed),
* no other event fired for the corner (one positive note per turn).

In `Advisor.on_corner`, events with `type.startswith("quality.")`
bypass the `min_severity` gate. `_process_job` routes to the new
`COMPLIMENT_SYSTEM_PROMPT` (verb-led, references corner_type, no
track names) instead of the corrective `SYSTEM_PROMPT`. Empty
responses fall back to one of five canned compliments.

`main.py._run_detectors` calls `detect_clean_corner` LAST, after all
the corrective detectors, with `other_events=events` so it can see
whether anything else has already fired.

**Test coverage.** Four cases in `tests/test_phase7.py`:

* `test_clean_corner_fires_on_high_g_no_lift` — 1.4 g lat, throttle
  pinned at 200, no other events; asserts one `quality.clean_corner`
  with severity < 0.3.
* `test_clean_corner_quiet_when_other_events_present` — same trace
  but a `braking.late_brake` is already in `other_events`; asserts
  empty list.
* `test_clean_corner_quiet_below_g_threshold` — 0.7 g lat; asserts
  empty.
* `test_clean_corner_quiet_if_driver_lifted_off` — 30-frame mid-corner
  throttle dip to 10; asserts empty.

## 4. Real 84-track database + sequence-matching detector

**Problem.** Phase 6 shipped a 1-track Deep Forest stub seeded from
the user's own session bbox. The user explicitly rejected the "user
types shape descriptions" approach — they wanted to pick a track in
the UI (or auto-detect it) and have the AI lean on real knowledge.

**Sourcing.** No in-packet track ID exists (verified against zetetos's
kaitai schema and Nenkai's PDTools). Detection has to be spatial.
Two public MIT-compatible sources cover GT7:

* `zetetos/gt-telemetry` (MIT) — recorded polylines for 84 GT7 layouts
  in world coordinates that match `pos_x` / `pos_z` in the telemetry
  packet. Polylines are quantised to a 16 m integer grid.
* `ddm999/gt7info` — track metadata (length, country, corner count,
  PD course IDs).

**Build.** `scripts/build_track_db.py` clones zetetos shallow,
downloads `course.csv`, fuzzy-matches the two by normalised name
(strips "Autodromo" / "Circuit" / spaces / punctuation), derives
bbox + apex turns (Heron's-formula circumradius peaks) from each
polyline, and generates a templated `shape_description` per track
from the length / oval flag / corner count / straight stats. Output
is vendored at `src/gt7coach/tracks/data/tracks.json` (~2 MB, 84
tracks, ~570 polyline points each).

**Detector rewrite.** Single-point bbox matching wasn't reliable
because of the 16-m grid — many tracks coincide at random points
(e.g. Deep Forest and Eiger Nordwand both have a sample at
(-48, -256)). Switched to **sequence matching**: a 30-packet rolling
buffer of recent positions. For each bbox-passing candidate, compute
the mean nearest-polyline distance across the buffer. The winner
must beat 40 m mean AND outrank the runner-up by ≥ 8 m. Below those
gates we keep buffering. Sticky lock holds the chosen track until the
car is ≥ 150 m outside the bbox for ≥ 3 s (covers garage / pit
returns).

**CLI.** New `gt7coach-list-tracks` (`src/gt7coach/list_tracks.py`)
prints the DB with `--filter <substring>` and `-v` (verbose). Useful
for picking a value for `--track` or `coach.track`.

**Test coverage.** Two integrity cases in `tests/test_phase7.py`:

* `test_track_database_loads_84_tracks` — DB loads, includes Deep
  Forest, bbox bounds the polyline, `shape_description` non-empty.
* `test_track_database_includes_popular_tracks` — Suzuka, Brands
  Hatch Indy, Laguna Seca, Trial Mountain, Deep Forest all present.

Phase-6 tests were updated to use the new track IDs
(`DeepForestRaceway` not `deep_forest`) and exercise sequence matching
via consecutive points from the real polyline.

## 5. LapTracker — start/finish-line summaries

**Problem.** The coach had nothing to say at the start/finish line.
Lap deltas, dominant mistakes, "new personal best" — all sat in the
session log but were never spoken back to the driver.

**Fix.** New `gt7coach.coach.laps.LapTracker`:

* `feed_packet(pkt) -> str | None` watches `lap_count` transitions.
  Fires only on `pkt.lap_count > self._last AND pkt.lap_count > 1`
  (skips the 0→1 formation-lap transition where `lap_time_ms` is
  still -1).
* `feed_events(events)` accumulates a `Counter[str]` of event types
  per lap.
* When a lap completes, builds a prompt with driver style + lap time
  + delta-to-best + mistake counts + clean-corner count and calls the
  LLM via `LAP_SUMMARY_SYSTEM_PROMPT` (1-2 sentences, max 20 words,
  no track names).
* Falls back to `_canned_summary` when the LLM errors or returns
  fewer than 3 words. Canned branches:
  * Personal best → `"New personal best — 1:23.456."`
  * Within 0.3 s of best → `"1:23.456, right on the money."`
  * Dominant mistake → `"1:23.456. Stay on the gas next lap."` (with
    a 9-entry action map for the common event types).
  * Clean lap → one of three compliments.
* Speaks via `voice.interrupt()` so it preempts whatever stale
  mid-corner advice was queued.

`main.py` instantiates a `LapTracker`, calls `feed_packet(pkt)` from
the receive loop and `feed_events(events)` whenever a corner closes.

**Test coverage.** Six cases in `tests/test_phase7.py`:

* `test_format_lap_time_basic` — 83456 ms → "1:23.456", edge cases.
* `test_canned_summary_personal_best` — PB branch renders.
* `test_canned_summary_dominant_mistake` — dominant `throttle.early_lift`
  surfaces in the spoken line.
* `test_lap_tracker_speaks_on_lap_transition` — 0→1→2 with valid
  lap_time_ms calls the provider and speaks the result.
* `test_lap_tracker_uses_canned_fallback_when_provider_fails` — raises
  `ProviderError`; canned summary including the lap time is spoken.
* `test_lap_tracker_ignores_first_lap_transition_with_no_time` — 0→1
  with `lap_time_ms=-1` (formation lap) stays silent.

## Real-world impact

The trigger session for Phase 7 was the 2026-05-11 21:04 Deep Forest
Gr.3 run (3 min 36 s, 25 corners). Before Phase 7 (Phase 6 codebase):

| Symptom | Before Phase 7 | After Phase 7 |
|---|---|---|
| `meta.json[totals].advice_spoken` | 0 (despite coach speaking) | matches actual utterances |
| `coach.jsonl` final results | only "queued" stubs | real advice + prompt + response |
| Corner #25 slide flagged as incident | no | yes, with "Skating, are we?" fallback |
| Clean corners acknowledged | no (19/44 silent) | yes, one compliment per clean turn |
| Track auto-detect range | Deep Forest only (1 track) | 84 tracks |
| Lap-end summary | none | spoken via `voice.interrupt()` |

A check-in saved at `examples/sample_session/` captures the pre-fix
state (advice_spoken=0). The wake-up note for Phase 8 should be the
first session that has both async-result logging AND
`gt7coach-list-tracks` showing the right track.

## Tests

`tests/test_phase7.py`: 16 cases (callback × 2, slide × 2, clean
corner × 4, track DB × 2, lap summary × 6). One test update in
`tests/test_incidents.py` forces `async_mode=False` for the legacy
spin-roast synchronous expectations. One bulk update in
`tests/test_phase6.py` to use the real track-DB IDs.

Totals: 120 pass, 1 skipped (pyttsx3-on-Linux, sandbox only).

## How to test

```powershell
git pull
git checkout main
.\.venv\Scripts\python.exe -m pip install -e ".[dev,gemini,voice]"
.\.venv\Scripts\gt7coach-list-tracks.exe --filter forest
.\.venv\Scripts\gt7coach-coach.exe --config config.yaml --summary
```

Expected vs the 2026-05-11 21:04 run:

* `meta.json[totals].advice_spoken` matches utterances heard (not 0).
* New `meta.json[totals].incidents` count appears.
* `coach.jsonl` has final `result` entries (not just "queued" stubs)
  with `advice` strings.
* Corner #25-style slide-outs are flagged with a sarcastic one-liner.
* Clean corners receive an occasional short compliment.
* Log shows `track detected: Deep Forest Raceway` within ~20 packets.
* At each lap boundary, a 1-2 sentence summary is spoken via
  `voice.interrupt()`, including the lap time.
