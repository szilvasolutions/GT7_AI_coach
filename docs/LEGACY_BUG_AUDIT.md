# Legacy-bug regression audit

[ARCHITECTURE.md §11](./ARCHITECTURE.md) lists 13 bugs from the V58 / V62
prototype that the v1 rewrite must never reintroduce. After shipping
Phases 5, 6, and 7 (a lot of code churn), this audit walks the six
behaviour-critical items end to end against the current `main` and
confirms each one still holds.

**Verdict: zero regressions.** Each finding below is sourced to the
actual file + line so it can be re-verified on demand.

## a) `steer` extracted from packet, not hardcoded 0

**Bug §11.3.** The legacy script stored `steer = 0` because the original
authors hadn't yet found the offset.

**Current state.** ✅ extracted from packet at `_OFF_STEER_ANGLE_RAD =
0x128` when the packet is the B-format (length ≥ 316). A-format packets
get `steer_angle=None` because the field doesn't exist there.

Source: `src/gt7coach/telemetry/packet.py:87` (offset constant),
`src/gt7coach/telemetry/packet.py:191` (parse).

```python
steer_angle=_f32(buf, _OFF_STEER_ANGLE_RAD) if has_b else None,
```

## b) `g_lat` and `g_lon` use measured values, not computed from yaw_rate

**Bug §11.4 / §11.5.** The legacy script:

* Hardcoded `g_lon = 0`.
* Computed `g_lat = (speed * yaw_rate) / 9.81`, which is *modelled* lateral
  acceleration assuming pure circular motion. Real GT7 telemetry has the
  measured values right in the packet.

**Current state.** ✅ both extracted from the body-force fields:

* `accel_long` from `_OFF_BODY_SURGE = 0x138`.
* `accel_lat` from `_OFF_BODY_SWAY = 0x130`.

Source: `src/gt7coach/telemetry/packet.py:89-91, 192-193`.

```python
accel_long=_f32(buf, _OFF_BODY_SURGE) if has_b else None,
accel_lat=_f32(buf, _OFF_BODY_SWAY) if has_b else None,
```

Detectors that read these fields divide by `G_MS2 = 9.80665` to surface
g-units, but the underlying signal is the measured normalised body force.

## c) `dt` uses real time deltas, no hardcoded 0.016

**Bug §11.6.** The legacy script assumed a fixed 60 Hz step (`dt = 0.016`)
which silently broke integration whenever GT7 dropped a packet or paused.

**Current state.** ✅ every dt is computed from `recv_time` deltas. Two
spots actually use dt:

1. `src/gt7coach/detectors/corner.py:95` — `total_yaw_deg` integration:
   ```python
   dt = self.packets[i].recv_time - self.packets[i - 1].recv_time
   ```
2. `src/gt7coach/detectors/braking.py:192` — `trail_off_too_fast` rate:
   ```python
   dt = packets[i].recv_time - packets[i - 1].recv_time
   ```

Grep for `0.016` across `src/` returns zero hits.

## d) `tire_radius` per-car or absent, not hardcoded 0.33 m

**Bug §11.11.** The legacy script used `tire_radius = 0.33 m` for *every*
car, then computed slip ratio from `rear_rps × 0.33` versus speed.
Different cars have different radii (an LMP1's rear is ~0.35 m, an
N100 kei car's is ~0.28 m), so the slip metric was systematically wrong
by ~10-20 %.

**Current state.** ✅ the wheelspin detector sidesteps the radius
question entirely by comparing `rear_rps` to `front_rps` directly — a
RWD car's front axle rolls true under throttle and reads ground speed, so
their *ratio* is the slip metric. No radius is multiplied in, so no car
needs one.

Source: `src/gt7coach/detectors/throttle.py:31-42` (docstring),
and the comparison line is `rear_rps / front_rps >= cfg.ratio_threshold`.

Grep for `0.33` in `src/` returns zero hits in code; the only mention
is the docstring above noting the legacy hack and why we don't repeat it.

(The packet *does* carry per-wheel tyre radius fields at `_OFF_TYRE_RADIUS_*`,
but no detector reads them — the slip ratio is dimensionless.)

## e) CSV writer is buffered, not reopened per packet

**Bug §11.8.** The legacy script opened `gt7_session_*.csv` in append
mode, wrote one row, and closed the file *per packet*. At 60 Hz that's
60 open / write / close syscalls per second for the lifetime of the
session, which on Windows under contention was visible in the profiler.

**Current state.** ✅ `SessionLogger.__init__` opens one long-lived file
handle and uses `csv.DictWriter` to write rows. The handle is closed
exactly once, in `close()`.

Source: `src/gt7coach/session/logger.py:95-98, 117-119, 238-242`.

```python
self._telemetry_fh = self._telemetry_path.open("w", newline="", encoding="utf-8")
fieldnames = list(Packet.__dataclass_fields__.keys())
self._telemetry_writer = csv.DictWriter(self._telemetry_fh, fieldnames=fieldnames)
...
def log_packet(self, pkt):
    self._telemetry_writer.writerow(_packet_row(pkt))
```

The handle uses Python's default text-mode buffering. The receive loop
has been profiled at <10 % tick budget on logging, so the synchronous
buffered write is fine and we haven't paid the cost of a writer thread.

The `events.jsonl` and `coach.jsonl` handles do explicit `flush()` after
every record so a hard kill keeps the most recent advisor turn.

## f) LLM calls are rate-limited, no thread-per-corner

**Bug §11.10.** The legacy script spawned a fresh thread for every LLM
call, with no concurrency cap. A long Gemini response could overlap with
the next corner's call, and the user would hear advice for corner N
*after* corner N+1's call had already fired.

**Current state.** ✅ exactly one worker thread per process
(`gt7coach-coach-worker`), a single `_pending` slot with drop-newest
semantics, AND a `RateLimiter` checking global-cooldown + duplicate-window
before any provider call is even queued.

Source: `src/gt7coach/coach/advisor.py` — `Advisor.__init__` allocates
one worker; `on_corner` consults the rate limiter and voice-busy gate,
then drops the job into the single slot. `_worker_loop` blocks on
`_wake` and pulls one job at a time.

```python
# Receive loop call path:
on_corner -> rate_limiter.allow -> voice.is_idle -> _pending = job -> _wake.set()

# Worker thread:
_worker_loop:
    while True:
        _wake.wait()
        job = take_pending()
        _process_job(job)   # calls provider.complete()
```

No code path anywhere creates a new thread per corner. The lap-summary
provider call (`LapTracker._on_lap_complete`) runs synchronously on the
receive loop, but is gated by a lap-transition (~1 minute apart) so it
cannot stack up.

## What this audit did not check

The seven other §11 items either landed before the audit window or are
non-behavioural:

* §11.1 — secrets in `.env`. Confirmed at repo bootstrap, still good.
* §11.2 — `.gitignore` correctness. Still present (`.env`, `sessions/`,
  `*.csv` with `examples/**/*.csv` whitelist for the sample session).
* §11.7 — corner state-machine hysteresis. Verified at Phase 2 by the
  detector tests; still passes.
* §11.9 — subnet auto-discovery. Verified at Phase 1; the `--ip auto`
  default still works on the user's machine.
* §11.12 — string-match post-filter on LLM output. Confirmed absent.
* §11.13 — driver style baked into prompt. Now lives in
  `AdvisorConfig.driver_style` (CLI flag + YAML override).
* §11.* — API-key handling. Loaded from `.env` via `python-dotenv`,
  never hardcoded.

If any of these need a fresh check, the relevant module's tests cover
the behaviour and `pytest` will catch a regression.

## How to re-run this audit

The six checks above are mostly grep / read commands. Quick verification:

```bash
# (a) steer_angle parse
grep -n "_OFF_STEER_ANGLE_RAD" src/gt7coach/telemetry/packet.py

# (b) accel_long / accel_lat parse
grep -n "_OFF_BODY_SURGE\|_OFF_BODY_SWAY" src/gt7coach/telemetry/packet.py

# (c) no hardcoded dt
grep -nR "0\.016" src/    # should return zero
grep -nR "dt = " src/gt7coach/detectors/   # should be recv_time deltas

# (d) no tire_radius 0.33
grep -nR "0\.33" src/     # only docstring hit in throttle.py

# (e) one long-lived csv writer
grep -n "open(" src/gt7coach/session/logger.py

# (f) one worker thread, rate-limited
grep -n "_worker_thread\|RateLimiter" src/gt7coach/coach/advisor.py
```
