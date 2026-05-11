"""Voice engine tests.

We test the NullVoiceEngine fully and the PyttsxVoiceEngine only as far as
its public protocol — the actual TTS output depends on a system espeak/SAPI
binary and is out of scope for unit tests.
"""

from __future__ import annotations

import pytest

from gt7coach.voice import NullVoiceEngine, make_voice


def test_null_engine_records_and_reports_idle() -> None:
    v = NullVoiceEngine()
    assert v.is_idle()
    v.speak("brake later")
    assert v.spoken == ["brake later"]
    assert v.is_idle()  # null engine doesn't actually queue
    v.set_busy(True)
    assert not v.is_idle()


def test_make_voice_null_returns_null_engine() -> None:
    v = make_voice("null")
    assert isinstance(v, NullVoiceEngine)


def test_make_voice_unknown_raises() -> None:
    with pytest.raises(ValueError):
        make_voice("not-a-real-engine")


def test_pyttsx3_engine_is_optional() -> None:
    """If pyttsx3 isn't installed, make_voice('pyttsx3') should raise cleanly."""
    pyttsx3 = pytest.importorskip("pyttsx3", reason="pyttsx3 not installed")
    # If pyttsx3 IS installed, init might still fail (no audio backend); we
    # only check that the engine class can be instantiated without crashing
    # the test runner.
    try:
        engine = make_voice("pyttsx3")
    except RuntimeError:
        pytest.skip("pyttsx3 installed but no audio backend available")
    assert engine.is_idle()
    engine.stop()
    _ = pyttsx3  # silence unused-import linter
