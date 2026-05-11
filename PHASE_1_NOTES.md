# Phase 1 — telemetry foundation

This branch implements section 12 of `ARCHITECTURE.md`: nothing more, nothing
less. No detectors, no coach, no voice — those land in Phase 2+.

## What landed

* `pyproject.toml` — Python 3.11+ project with `ruff` + `pytest` configured.
* `src/gt7coach/` package skeleton (only the `telemetry/` sub-module has code).
* `src/gt7coach/telemetry/`:
  * `decrypt.py` — Salsa20 with the canonical key and per-format IV seed.
  * `packet.py` — `Packet` dataclass covering every field listed in section 5,
    plus a `parse_packet()` and a `build_synthetic_packet()` for tests.
  * `receiver.py` — UDP receiver with background heartbeat thread; broadcast
    auto-discovery on `255.255.255.255:33739` with subnet-scan fallback and
    explicit-IP override via `ReceiverConfig.ps5_ip`.
  * `replay.py` — `replay_csv()` iterator that emits `Packet` objects from a
    fixture CSV, with optional real-time pacing for end-to-end smoke tests.
* `tests/fixtures/synthetic_brake_corner.csv` — 24-frame synthetic trace of a
  brake-and-corner; lateral g peaks above 1.0 mid-corner.
* `tests/test_telemetry.py` — four tests:
  * `test_decrypt_roundtrip` (synthetic packet through encrypt → decrypt)
  * `test_packet_parse_fields` (every spec field present, sane values)
  * `test_packet_a_format_optional_fields_are_none` (graceful A/B handling)
  * `test_replay_from_fixture` (24 packets, peak lat-g > 1.0)
* `README.md`, `.env.example`, `config.example.yaml` (verbatim from §10).
* `.github/workflows/ci.yml` — runs `ruff check`, `ruff format --check`,
  `pytest` on Python 3.11 and 3.12 for every push and PR to `main`.

All `ruff check` / `ruff format --check` and `pytest` runs are green locally.

## Bugs from §11 explicitly fixed

| # | Bug | Fix |
|---|---|---|
| 1 | API keys hardcoded | `.env` + `.env.example`; no key constant in `src/` |
| 2 | `.gitignore` missing | already present on `main`; verified `.env`, `sessions/`, `*.csv` (except fixtures) excluded |
| 3 | `steer = 0` hardcoded | `Packet.steer_angle` read from offset `0x128` (B+ format) |
| 4 | `g_lon = 0` hardcoded | `Packet.accel_long` read from body-surge field at `0x138` (B+ format) |
| 5 | `g_lat` computed | `Packet.accel_lat` read from body-sway field at `0x130` (B+ format) |
| 9 | V58 had auto-discovery, V62 lost it | `discover_ps5()` broadcasts first, falls back to subnet scan |
| 11 | tyre radius hardcoded | not used in phase 1 (slip metric is a Phase 2 detector concern) |

Bugs 6 (real `dt`) / 7 (corner-state hysteresis) / 10 (LLM concurrency limit)
are Phase 2/3 territory and intentionally left for later.

## Deviations from ARCHITECTURE.md

A first live capture (`capture_20260511_150824`, 2049 frames over 34.17s,
60.0 pps, zero packet loss across IDs 24947→26995) resolved the three open
questions. Details follow each item.

1. **Default heartbeat is `B` (316-byte packets), not `A` (296-byte). — RESOLVED.**
   The live capture confirmed the PS5 responds to `B` with 316-byte packets
   (100% of 2049 frames). Spec section 5 says "296-byte" but also lists
   steer/accel as required, which only exist in B+. `B` is the correct
   default. **Update spec section 5 to specify "316-byte packets via heartbeat
   byte `B`"**; `A` remains selectable via `network.packet_format` for users
   who want the legacy payload.

2. **`accel_long` / `accel_lat` units — RESOLVED, kaitai docs are wrong.**
   The `zetetos/gt-telemetry` kaitai struct labels these "body forces (-1..1)".
   Live capture proves they are actually **m/s²**, matching the spec:
   - Frame 500, full throttle in 4th gear at 209 km/h: `accel_long = +6.05`
     (≈0.6 g forward — physically correct for a sports car at full throttle).
   - Frame 1 (94 km/h, hard left, yaw_rate -0.527 rad/s):
     `accel_lat = +13.65` ≈ `speed × yaw_rate = 26.22 × 0.527 = 13.82` m/s²
     (≈1.4 g — matches circular-motion calculation to 1%).
   Spec section 5 (m/s²) wins; no code change required. **Suggest leaving a
   note in section 5 explicitly contradicting the kaitai labelling so future
   readers don't get confused.**

3. **`lap_time_ms` = `last_laptime` (0x7C) — RESOLVED.** The capture crosses
   one lap boundary (frames 573→574). Behaviour observed:
   - During lap 1: `lap_count=1`, `lap_time_ms=-1` (no previous lap yet).
   - At line crossing: `lap_count=2`, `lap_time_ms=63246` (= 1:03.246, the
     just-completed lap 1).
   This is exactly what we want for post-corner analysis. **Update spec
   section 5 to say `lap_time_ms` is the most-recent completed lap (-1 if
   none).** `current_laptime` (C-format only) would be needed for
   in-lap-progress UI — out of scope for v1.

4. **`packet_id` is read from `0x70` (sequence_id), not `0x00`.** The legacy
   V62 reads `0x00`, which is actually the GT7 magic header (always
   `0x47375330`). V62's packet IDs are therefore all identical — a real bug.
   `0x70` is the kaitai-defined per-frame sequence counter.

## Findings from the first live capture

Worth recording so we don't re-derive them later:

* **Tick rate is 60 Hz on the wire** (59.96 pps measured over 34 s) but the
  underlying physics tick may be coarser: in the first ~3 s of the capture
  several consecutive packets with incrementing `packet_id` had byte-identical
  payloads. This was the pre-race / countdown phase. Detectors should treat
  duplicate consecutive payloads as a single physics step (track by
  `packet_id` or by `recv_time` delta, not by content hash).
* **Wheel speeds are signed and negative for forward motion.** At 209 km/h
  (58 m/s) all four wheels read ≈-170 rad/s; with a 0.33 m tyre radius this
  is +56 m/s in magnitude — matches `speed_mps`. The legacy V62 used
  `abs()` for this reason; Phase 2 detectors should do the same (or compare
  signed values consistently). The sign convention is consistent across all
  four wheels.
* **Lockup detection is feasible from this packet alone.** Frame 810
  (heavy-brake zone, brake=255, car at 261 km/h ≈ 72.5 m/s, wheels at
  ≈195 rad/s ≈ 64.3 m/s effective) shows ~11 % slip on the front axle —
  the moment the driver locked up. Good news for Phase 2's `braking.lockup`
  detector.

## Reference-source disagreements I had to resolve

* **Salsa20 key.** V62 uses `b"Simulator Interface Packet GT7__"`; V23 and
  `snipem/gt7dashboard` use `b"Simulator Interface Packet GT7 ver 0.0"[:32]`.
  The latter is the documented canonical key and what real packets decrypt
  with. V62's key is wrong.
* **IV XOR seed.** V62 uses `0xDEADBEEF`; V23 and `snipem/gt7dashboard` use
  `0xDEADBEAF` for the A format. `zetetos/gt-telemetry` shows the seed
  varies by heartbeat: `A → 0xDEADBEAF`, `B → 0xDEADBEEF`, `~ → 0x55FABB4F`,
  `C → 0xDEADBEEF`. We encode the full table in
  `decrypt.FORMAT_IV_SEEDS`.
* **Wheel-speed offsets.** V62 reads `0xA0..0xB0`; V23 reads `0x94..0xA4`.
  Both are wrong. The kaitai struct places `wheel_radians_per_second` at
  `0xA4..0xB4` (the cells V62 used start one float earlier, at the
  `road_plane_distance` field).
* **Yaw-rate offset.** V62 reads `angular_velocity_y` from `0x2C`. Per the
  kaitai struct `0x2C` is `angular_velocity_x`; `0x30` is `_y`. We use
  `0x30`.

These are explicitly the bugs the legacy scripts had and why the legacy
output was the way it was; v1 fixes them all.

## What's still TBD (won't block Phase 2 but worth noting)

* `Receiver.discover_ps5()` opens a broadcast-capable socket and then a
  separate scan socket. Both bind to `port_rx`; on some hosts the second
  bind needs `SO_REUSEPORT` in addition to `SO_REUSEADDR`. Untested without
  a real PS5 on the LAN — the algorithm is right, but expect to tune.
* Replay does not yet do realtime pacing under test (it can, but no test
  asserts the timing). Phase 2 corner-detector tests will exercise it.

## How to verify

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest -v
```

Should report 4 passed and no warnings.
