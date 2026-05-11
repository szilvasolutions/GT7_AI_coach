# Phase 2 — detector layer

This branch implements section 6 of `ARCHITECTURE.md` (the detector layer)
for the three v1 detectors that the spec puts into Phase 2.

## What landed

* `src/gt7coach/detectors/`:
  * `base.py` — `Event` dataclass and the 1 g constant.
  * `corner.py` — `CornerSegmenter` state machine + `CornerTrace` container.
  * `braking.py` — `detect_late_brake(...)`.
  * `throttle.py` — `detect_wheelspin(...)`.
  * `steering.py` — `detect_understeer(...)`.
* `tests/test_detectors.py` — 14 tests covering the segmenter, each detector,
  and an end-to-end replay test that feeds a synthetic "bad-corner" trace
  through segmenter + all three detectors and asserts at least one event of
  each type fires.
* `tests/_synth.py` — `make_packet()` helper and `build_bad_corner_trace()`
  fixture builder (programmatic so the trace is deterministic and easy to
  tune as thresholds evolve).

Total: 24 tests, all passing. `ruff check` and `ruff format --check` both
clean.

## Design notes

### Segmenter

The legacy V62 segmenter bounced (spec bug §11.7: 8 entries / 100 ms in its
logs) because it had no exit hysteresis. The Phase-2 segmenter splits the
exit path into two steps:

1. When `brake < exit_brake` AND `|lat_g| < exit_lat_g`, start an
   `exit_pending` timer but keep accumulating frames.
2. If the signal comes back before `min_dwell_s` elapses, clear the timer
   and continue cornering — the corner is **not** split.
3. If `min_dwell_s` passes, the corner finalises and the trailing
   exit-pending frames are *trimmed* so `duration_s` reflects the
   actual cornering window (not the dwell).

`min_corner_duration_s` (default 0.7 s) drops sub-threshold blips so the
detectors don't get fed micro-segments.

### Detectors

All three use ratios between front and rear axle wheel-rps rather than
absolute slip (which would require a per-car tyre radius — bug §11.11
says don't hardcode 0.33). This is mathematically equivalent to the
spec's slip-based wording for RWD cars and tyre-radius-symmetric for
4WD/FWD.

| Detector | Condition | Severity |
|---|---|---|
| `braking.late_brake` | peak brake more than 0.30 s after turn-in (\|steer\| > 0.10 rad), peak brake > 100 | scales 0->1 between 0.30 s and 1.00 s offset |
| `throttle.wheelspin` | rear_rps / front_rps > 1.10 for > 0.05 s with throttle > 100 | scales 0->1 between ratio 1.10 and 1.50 |
| `steering.understeer` | front_rps / rear_rps > 1.15 for > 0.30 s with \|lat_g\| > 0.70 | scales 0->1 between ratio 1.15 and 1.40 |

Thresholds live in per-detector `Config` dataclasses with defaults that
mirror `config.example.yaml`. Phase 3 will wire the YAML loader so these
become user-tunable without code edits.

### Wheel-speed sign

The live capture (`capture_20260511_150824`) showed wheel speeds in the
packet are **signed and negative for forward motion**. All three detectors
take `abs()` on every wheel speed before computing ratios, which is robust
to the convention.

## What it does on the user's real capture

Running the segmenter + detectors on the 2049-frame
`capture_20260511_150824` produces:

* **3 corner segments** (the first one is a pre-race artefact from the
  duplicate-content packets I noted in Phase 1 — `lat_g` was already
  reading 13.6 m/s² before the race started because of vehicle staging).
  Corners #2 and #3 are the real driving:
  - #2: 6.08 s, entry 280 km/h, min 68 km/h, peak 1.47 g  -> the heavy
    braking zone into the corner.
  - #3: 6.16 s, entry 217 km/h, min 9 km/h, peak 1.23 g  -> the lockup
    and slide-out the user reported.
* **0 events fired.** Correct: the user didn't late-brake-into-turn-in,
  the car wasn't suffering rear wheelspin (controller + unfamiliar car
  on a track was leading them to brake too hard, not to over-throttle),
  and a front-wheel lockup pulls all four wheels down together rather
  than producing the front-vs-rear *imbalance* the understeer detector
  looks for. The driving mistake here is fundamentally `braking.lockup`,
  which is a Phase 4 detector per spec §6.

So Phase 2's detectors behave correctly — they're aimed at *different*
mistakes than the ones in this particular capture.

## Pre-race / duplicate-content frames

Confirmed: during the first ~3 seconds of the capture, several consecutive
packets had byte-identical payloads even though `packet_id` incremented
normally. Two consequences for downstream code:

1. The segmenter sees a high `lat_g` value (~13.6 m/s², ~1.4 g) on those
   frames and opens a "corner". The corner finalises only when the actual
   race starts and the body forces drop to near zero. This produced
   corner #1 in the analysis above — not a real corner.
2. Phase 3 / 4 detectors that compute deltas (e.g. throttle sawing rate,
   real m/s² accel from velocity differencing) will see zero change
   across duplicates. They should de-dup by `packet_id` or accept that
   `dt` may be > 1/60 s.

A clean fix is to drop consecutive packets where the payload byte-hash is
identical to the previous one. That's a one-line change in the receiver
and should land before Phase 4. Tracked separately so this branch stays
focused.

## How to verify

```bash
ruff check .
ruff format --check .
pytest -v
```

Should report 24 passed, 0 warnings.

## Out of scope (kept for later phases)

* `braking.lockup`, `braking.trail_off_too_fast` (Phase 4 per spec §6)
* `steering.oversteer`
* `throttle.sawing`, `throttle.early_lift`
* `line.late_apex`
* Wiring `config.yaml` into the detector configs (Phase 3)
