"""VoiceEngine protocol and a factory for the engine names listed in the spec."""

from __future__ import annotations

from typing import Protocol


class VoiceEngine(Protocol):
    """Per ARCHITECTURE.md section 8."""

    def speak(self, text: str) -> None:
        """Enqueue ``text`` for speech. Non-blocking."""

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
