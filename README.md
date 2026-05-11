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

## Credits

This project would be impossible without the reverse-engineering work of:

- [Nenkai](https://github.com/Nenkai) — original protocol disclosure
- [snipem](https://github.com/snipem) — `gt7dashboard`
- [vwhitteron](https://github.com/vwhitteron) / [zetetos](https://github.com/zetetos) — `gt-telemetry` and kaitai struct definitions
- [ddm999](https://github.com/ddm999) — additional protocol notes

## License

Apache 2.0. No CLA.
