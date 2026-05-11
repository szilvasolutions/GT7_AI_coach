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

These are flagged so we can either update the spec or revise the code:

1. **Default heartbeat is `B` (316-byte packets), not `A` (296-byte).**
   The spec section 5 says "296-byte packets" but also requires
   `steer_angle`, `accel_long`, and `accel_lat`. Those fields only exist in
   the addendum-1 (`B`) format and later; the A format (`A`) lacks them.
   Defaulting to `B` is the only way to satisfy section 5's field list and
   the section 11 bug list at the same time. Configurable via
   `network.packet_format` in `config.yaml`. **Suggest updating the spec to
   say "316-byte packets via heartbeat `B`" or to mark steer/accel as
   "best-effort, optional in A format."**

2. **`accel_long` / `accel_lat` units.** The kaitai struct from
   `zetetos/gt-telemetry` documents these (the `translational_envelope`'s
   `surge` and `sway`) as "Body forces along axes (-1 to 1)" — i.e. they
   are *normalized* body-force values, not values in m/s². Section 5 of the
   spec labels them "m/s²". We read the raw float verbatim; the *unit
   interpretation* is incorrect in the spec. **Suggest updating section 5 to
   `accel_long`, `accel_lat` (float, normalized -1..+1 body force)** and
   either accept that for Phase 2 detectors or derive true m/s² values by
   differencing the velocity vector at offsets `0x10` / `0x14` / `0x18`.

3. **`lap_time_ms` source.** The spec says simply "`lap_time_ms`". The packet
   has `best_laptime` at `0x78`, `last_laptime` at `0x7C`, and (only in C
   format) `current_laptime`. We picked `last_laptime` — the most useful
   single value for post-corner analysis. **Suggest the spec name this
   explicitly so we don't drift later.**

4. **`packet_id` is read from `0x70` (sequence_id), not `0x00`.** The legacy
   V62 reads `0x00`, which is actually the GT7 magic header (always
   `0x47375330`). V62's packet IDs are therefore all identical — a real bug.
   `0x70` is the kaitai-defined per-frame sequence counter.

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
