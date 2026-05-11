"""Piper voice engine — higher-quality offline neural TTS.

Piper is a neural TTS engine that ships ONNX voice models and runs entirely
offline. Quality is noticeably better than pyttsx3 / SAPI; the cost is
~5-10 MB of voice files and a one-time install of the ``piper-tts`` package.

This engine prefers the Python bindings (``piper-tts``) but falls back to
shelling out to a ``piper`` CLI binary if that's what's on PATH. The user
points us at the voice with ``piper_voice`` (a model name) and / or
``piper_model_path`` (an explicit ``.onnx`` path).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import wave
from collections import deque
from pathlib import Path

log = logging.getLogger(__name__)

_QUEUE_SIZE = 2


def _find_piper_binary() -> str | None:
    return shutil.which("piper")


class PiperVoiceEngine:
    """Speaks queued strings on a background thread using Piper.

    Two strategies are tried, in order:
      1. Python ``piper-tts`` package + ``simpleaudio`` for playback.
      2. ``piper`` CLI binary + the platform's default WAV player.

    Either one will produce a noticeably-better result than ``pyttsx3``.
    Voice models are downloaded by the user from
    https://huggingface.co/rhasspy/piper-voices/.
    """

    def __init__(
        self,
        *,
        model_path: str | Path | None = None,
        voice: str | None = "en_US-amy-medium",
        cli_binary: str | None = None,
    ) -> None:
        self._model_path: Path | None = None
        if model_path is not None:
            mp = Path(model_path)
            if not mp.is_file():
                raise RuntimeError(f"Piper model not found: {mp}")
            self._model_path = mp
        elif voice is not None:
            # Look in a couple of common spots; if nothing's there the
            # CLI/Python lookup will raise a friendlier error later.
            for cand in (
                Path.cwd() / "voices" / f"{voice}.onnx",
                Path.cwd() / f"{voice}.onnx",
                Path.home() / ".gt7coach" / "voices" / f"{voice}.onnx",
            ):
                if cand.is_file():
                    self._model_path = cand
                    break

        self._cli_binary = cli_binary or _find_piper_binary()
        self._python_engine = self._try_load_python_engine()
        if self._python_engine is None and self._cli_binary is None:
            raise RuntimeError(
                "Neither the piper-tts Python package nor a piper CLI binary "
                "is installed. pip install 'gt7coach[piper]' and download a "
                "voice model from https://huggingface.co/rhasspy/piper-voices/"
            )

        self._queue: deque[str] = deque(maxlen=_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._busy = False
        self._thread = threading.Thread(target=self._worker, name="gt7-tts-piper", daemon=True)
        self._thread.start()

    # ---- public ----------------------------------------------------------

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            if self._queue.maxlen and len(self._queue) == self._queue.maxlen:
                dropped = self._queue.popleft()
                log.debug("piper queue full, dropping oldest: %r", dropped)
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

    def _try_load_python_engine(self):
        try:
            from piper.voice import PiperVoice
        except ImportError:
            return None
        if self._model_path is None:
            return None
        try:
            return PiperVoice.load(str(self._model_path))
        except Exception as exc:  # pragma: no cover — model-load failures
            log.warning("piper-tts python load failed: %s", exc)
            return None

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    text = self._queue.popleft()
                    self._busy = True
                try:
                    if self._python_engine is not None:
                        self._speak_python(text)
                    else:
                        self._speak_cli(text)
                except Exception as exc:
                    log.warning("piper speak failed (%s): %r", exc, text)
                finally:
                    with self._lock:
                        self._busy = False

    def _speak_python(self, text: str) -> None:
        # Synthesize to an in-memory WAV, then play it.
        import io

        import simpleaudio as sa  # type: ignore[import-untyped]

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            self._python_engine.synthesize(text, wav)
        buf.seek(0)
        playback = sa.WaveObject.from_wave_read(wave.open(buf, "rb")).play()
        playback.wait_done()

    def _speak_cli(self, text: str) -> None:
        if self._cli_binary is None:
            raise RuntimeError("no piper CLI on PATH")
        if self._model_path is None:
            raise RuntimeError("piper CLI needs --model_path")
        tmp_wav = Path.cwd() / ".piper_tmp.wav"
        try:
            subprocess.run(
                [self._cli_binary, "--model", str(self._model_path), "--output_file", str(tmp_wav)],
                input=text.encode("utf-8"),
                capture_output=True,
                check=True,
            )
            self._play_wav_native(tmp_wav)
        finally:
            try:
                tmp_wav.unlink()
            except OSError:
                pass

    def _play_wav_native(self, path: Path) -> None:
        """Best-effort cross-platform WAV playback."""
        if os.name == "nt":
            try:
                import winsound

                winsound.PlaySound(str(path), winsound.SND_FILENAME)
                return
            except Exception:  # pragma: no cover
                pass
        # Fall back to whatever's on PATH.
        for binary in ("paplay", "aplay", "afplay"):
            if shutil.which(binary):
                subprocess.run([binary, str(path)], check=False, capture_output=True)
                return
        raise RuntimeError("no WAV player available")
