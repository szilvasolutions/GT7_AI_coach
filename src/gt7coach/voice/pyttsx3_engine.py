"""pyttsx3 voice engine: offline TTS that ships with Python.

Spec section 8: "Single TTS worker thread, bounded queue (size 2), drop
oldest if full." A :class:`collections.deque` with ``maxlen=2`` gives us
the drop-oldest behaviour for free.

On Linux pyttsx3 needs ``espeak`` (``apt install espeak``) installed at the
system level; on Windows it uses SAPI5, on macOS it uses NSSpeechSynthesizer.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

_QUEUE_SIZE = 2


class PyttsxVoiceEngine:
    """Speaks queued strings on a background thread.

    The queue depth is capped at 2 (spec section 8). Pushing to a full queue
    discards the oldest pending utterance, not the new one — by the time a
    third utterance is queued, the first one is stale advice anyway.
    """

    def __init__(self, *, rate: int = 200) -> None:
        try:
            import pyttsx3  # noqa: F401  (presence check; real import in worker)
        except ImportError as exc:
            raise RuntimeError("pyttsx3 not installed. pip install 'gt7coach[voice]'") from exc

        self._rate = rate
        self._queue: deque[str] = deque(maxlen=_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._utterance_idx = 0
        self._thread = threading.Thread(target=self._worker, name="gt7-tts", daemon=True)
        self._thread.start()
        log.info("pyttsx3 voice engine starting (rate=%d)", self._rate)

    # ---- public ----------------------------------------------------------

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if self._queue.maxlen and len(self._queue) == self._queue.maxlen:
                dropped = self._queue.popleft()
                log.warning("voice queue full, dropping oldest: %r", dropped)
            self._queue.append(text)
        log.debug("voice.speak queued: %r (queue=%d)", text, len(self._queue))
        self._wake.set()

    def interrupt(self, text: str) -> None:
        """Clear pending utterances and queue ``text`` next."""
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._queue.clear()
            self._queue.append(text)
        self._wake.set()

    def is_idle(self) -> bool:
        with self._lock:
            return not self._queue and not self._busy

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    # ---- worker ----------------------------------------------------------

    def _worker(self) -> None:
        """Drain the queue, re-initialising the engine per utterance.

        pyttsx3's SAPI backend on Windows reliably fails on the second and
        subsequent ``runAndWait()`` calls against the same engine instance —
        the first utterance speaks, every later one is silently swallowed.
        Re-initialising costs ~100 ms but works every time, which matters more
        than throughput at the 4 s rate-limit cadence the coach runs at.
        """
        import pyttsx3

        while not self._stop.is_set():
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    text = self._queue.popleft()
                    self._busy = True
                self._utterance_idx += 1
                idx = self._utterance_idx
                t0 = time.monotonic()
                engine = None
                try:
                    engine = pyttsx3.init()
                    engine.setProperty("rate", self._rate)
                    t_init = time.monotonic() - t0
                    # SAPI's audio device needs ~80-150 ms to warm up on the
                    # first phoneme of each utterance — without padding, the
                    # leading consonant of words like "Brake" or "Carry" gets
                    # clipped. Prepending a short throwaway syllable + ellipsis
                    # gives the device time to come up before the real content
                    # hits the speakers. The leading text reads as a tiny
                    # pause and is essentially inaudible.
                    padded = ". " + text
                    engine.say(padded)
                    engine.runAndWait()
                    t_total = time.monotonic() - t0
                    log.info(
                        "pyttsx3 utterance #%d done (init=%.0fms total=%.0fms): %r",
                        idx,
                        t_init * 1000,
                        t_total * 1000,
                        text,
                    )
                except Exception as exc:
                    log.warning("pyttsx3 utterance #%d failed (%s): %r", idx, exc, text)
                finally:
                    if engine is not None:
                        try:
                            engine.stop()
                        except Exception:  # pragma: no cover
                            pass
                    with self._lock:
                        self._busy = False
