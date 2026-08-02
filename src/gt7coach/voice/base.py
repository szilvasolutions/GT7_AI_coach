"""VoiceEngine protocol and a factory for the engine names listed in the spec."""

from __future__ import annotations

from typing import Protocol


def estimate_speech_seconds(text: str, *, wpm: float = 170.0, overhead_s: float = 0.4) -> float:
    """Rough duration of ``text`` spoken at ``wpm`` words per minute.

    Used by the cue scheduler to decide whether an utterance fits before the
    next corner. Word count over rate plus a fixed engine spin-up overhead is
    within ~20% of real TTS output for coaching-length lines, which is plenty
    — the scheduler's finish margin absorbs the error.
    """
    words = max(1, len(text.split()))
    return words / (wpm / 60.0) + overhead_s


class VoiceEngine(Protocol):
    """Per ARCHITECTURE.md section 8."""

    def speak(self, text: str) -> None:
        """Enqueue ``text`` for speech. Non-blocking."""

    def estimate_duration(self, text: str) -> float:
        """Estimated seconds of audio ``speak(text)`` would produce."""

    def interrupt(self, text: str) -> None:
        """Clear any pending utterances and queue ``text`` next.

        Used for important messages (spins, crashes) that should preempt
        whatever stale coaching tip was queued. Mid-utterance audio is not
        forcibly aborted — the currently-speaking message finishes first.
        """

    def is_idle(self) -> bool:
        """True when nothing is queued or being spoken right now."""

    def stop(self) -> None:
        """Drain the queue, halt the worker, release any audio resources."""


def make_voice(name: str, **kwargs) -> VoiceEngine:
    """Build a voice engine by short name.

    Recognised names:
        ``null``      -> :class:`NullVoiceEngine` (records, no audio)
        ``pyttsx3``   -> :class:`PyttsxVoiceEngine` (offline, cross-platform)
        ``piper``     -> :class:`PiperVoiceEngine` (neural TTS, better quality)
    """
    key = (name or "").lower()
    if key == "system":
        # Documented alias — pyttsx3 wraps the OS system TTS (SAPI/NSSpeech/espeak).
        key = "pyttsx3"
    if key in ("null", "none", ""):
        from gt7coach.voice.null_engine import NullVoiceEngine

        return NullVoiceEngine()
    if key == "pyttsx3":
        from gt7coach.voice.pyttsx3_engine import PyttsxVoiceEngine

        return PyttsxVoiceEngine(**{k: v for k, v in kwargs.items() if k == "rate"})
    if key == "piper":
        from gt7coach.voice.piper_engine import PiperVoiceEngine

        accepted = {"model_path", "voice", "cli_binary"}
        return PiperVoiceEngine(**{k: v for k, v in kwargs.items() if k in accepted})
    raise ValueError(f"unknown voice engine {name!r}")
