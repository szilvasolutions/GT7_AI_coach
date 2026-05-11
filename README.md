# GT7 AI Coach

Open-source real-time AI driving coach for **Gran Turismo 7** on PS4/PS5.

The coach reads the unofficial UDP telemetry stream from the console, detects
specific driving events (late braking, understeer, wheelspin, …) with physics,
and uses an LLM of your choice to translate those events into short, spoken
coaching feedback.

## Status — pre-alpha

This is a v1 rewrite of an older prototype. **Phase 1 is shipping the
telemetry layer only — there is no coaching, no voice, no detectors yet.**
See [ARCHITECTURE.md](./ARCHITECTURE.md) for the full plan and
[PHASE_1_NOTES.md](./PHASE_1_NOTES.md) for what's actually in this branch.

Do not point this at a real PS5 yet expecting coaching. You can replay
fixture CSVs end-to-end through the telemetry module today.

## Design in one paragraph

Physics detects events. An LLM translates them. The LLM never sees raw
telemetry — it only sees a short structured description of what physics
already found. This eliminates the "AI always talks about throttle" failure
mode of the original prototype.

## Install (dev)

```bash
git clone https://github.com/szilvasolutions/GT7_AI_coach.git
cd GT7_AI_coach
python -m venv .venv
. .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Requires Python 3.11+.

## Configuration

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Open `.env` and paste the API key for whichever provider you intend to use
(Anthropic / OpenAI / Gemini — pick one). The Ollama provider needs no key.

`config.yaml` controls the rest: PS5 IP (or `auto`), heartbeat cadence, voice
engine, detector thresholds. Defaults are sensible.

Telemetry never leaves your machine. Only short detected-event summaries are
sent to the LLM you configure.

## Run the tests

```bash
pytest
ruff check
```

The test suite runs without any PS5. Phase 1 verifies Salsa20 decrypt, packet
parsing against the canonical kaitai struct, and CSV replay.

## Capture a live session (no coaching, no voice — telemetry only)

`gt7coach-capture` records a real PS5 session to disk so the data can be
replayed and analysed offline. Useful for sanity-checking the receiver on
your LAN and for resolving the open questions in
[PHASE_1_NOTES.md](./PHASE_1_NOTES.md).

```bash
# 1. Start GT7 on the PS5 and enter a race / time trial.
# 2. On your PC, on the same LAN:
gt7coach-capture                           # auto-discover PS5, format B
gt7coach-capture --ip 192.168.1.120        # explicit PS5 IP (if broadcast is blocked)
gt7coach-capture --duration 60             # auto-stop after 60 seconds
gt7coach-capture --out ./mycaps --format A # legacy 296-byte format
```

Hit Ctrl-C to stop. Each capture produces three files in `./sessions/`:

* `capture_<timestamp>.bin`  — raw decrypted packets, for offline byte-level analysis
* `capture_<timestamp>.csv`  — same schema as the replay loader, parses with `replay_csv()`
* `capture_<timestamp>.json` — metadata: format, host, packet-size histogram, packet rate

The live status line shows speed, gear, RPM, pedal positions, steering angle
and lateral / longitudinal g so you can confirm the data is alive before
walking away.

## Credits

This project would be impossible without the reverse-engineering work of:

- [Nenkai](https://github.com/Nenkai) — original protocol disclosure
- [snipem](https://github.com/snipem) — `gt7dashboard`
- [vwhitteron](https://github.com/vwhitteron) / [zetetos](https://github.com/zetetos) — `gt-telemetry` and kaitai struct definitions
- [ddm999](https://github.com/ddm999) — additional protocol notes

## License

Apache 2.0. No CLA.
