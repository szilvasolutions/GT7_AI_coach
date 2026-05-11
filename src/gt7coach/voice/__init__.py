"""Voice engines for the coach's spoken output."""

from gt7coach.voice.base import VoiceEngine, make_voice
from gt7coach.voice.null_engine import NullVoiceEngine

__all__ = ["NullVoiceEngine", "VoiceEngine", "make_voice"]
