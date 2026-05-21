"""``gt7coach-test-voice`` — speaks a series of phrases with verbose logs.

Use this to verify the voice engine works end-to-end on your machine,
independent of GT7. If you hear all five phrases, the audio chain is
fine; if you only hear the first one, that's the pyttsx3 multi-utterance
bug and we need a different engine (or to investigate further).
"""

from __future__ import annotations

import argparse
import logging
import time

from gt7coach.voice import make_voice


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--voice",
        default="pyttsx3",
        choices=("pyttsx3", "piper", "null"),
        help="voice engine to test (default: pyttsx3)",
    )
    parser.add_argument(
        "--rate", type=int, default=230, help="speech rate (pyttsx3 only, default 230)"
    )
    parser.add_argument(
        "--gap", type=float, default=1.5, help="seconds between utterances (default 1.5)"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("gt7coach.voice_test")

    phrases = [
        "Test one. Voice engine is starting.",
        "Test two. If you hear this, the second utterance worked.",
        "Test three. Trail brake gently into the corner.",
        "Test four. Open the wheel earlier on exit.",
        "Test five. Voice engine looks healthy.",
    ]

    voice = make_voice(args.voice, rate=args.rate)
    log.info("speaking %d phrases via %s, %.1f s apart", len(phrases), args.voice, args.gap)

    for i, phrase in enumerate(phrases, start=1):
        log.info("queue #%d: %r", i, phrase)
        voice.speak(phrase)
        time.sleep(args.gap)

    log.info("all queued — waiting for voice to drain...")
    deadline = time.monotonic() + 30.0
    while not voice.is_idle() and time.monotonic() < deadline:
        time.sleep(0.05)
    if voice.is_idle():
        log.info("voice drained cleanly.")
    else:
        log.warning("voice did NOT drain within 30 s — something is stuck.")

    voice.stop()
    log.info("done. Did you hear all %d phrases?", len(phrases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
