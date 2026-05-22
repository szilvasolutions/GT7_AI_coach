"""CLI-level tests: provider auto-selection + source wildcard expansion."""

from __future__ import annotations

from pathlib import Path

import pytest

from gt7coach.main import _select_provider, _stream_replay, parse_args

# ---- _select_provider ------------------------------------------------------


def test_select_provider_cli_choice_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    name, reason = _select_provider("anthropic", "gemini", None)
    assert name == "anthropic"
    assert reason == "cli"


def test_select_provider_falls_back_to_only_available_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's actual setup: only GEMINI_API_KEY is set, config says anthropic."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    name, reason = _select_provider(None, "anthropic", None)
    assert name == "gemini"
    assert "fallback" in reason


def test_select_provider_uses_config_when_its_key_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    name, reason = _select_provider(None, "anthropic", None)
    assert name == "anthropic"
    assert reason == "config"


def test_select_provider_keeps_config_when_no_keys_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    name, reason = _select_provider(None, "anthropic", None)
    assert name == "anthropic"  # caller will see the helpful error
    assert "will error" in reason


def test_select_provider_explicit_api_key_lets_it_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    name, reason = _select_provider(None, "gemini", "user-passed-key")
    assert name == "gemini"
    assert reason == "config"


def test_select_provider_ollama_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    name, reason = _select_provider(None, "ollama", None)
    assert name == "ollama"
    assert reason == "config"


# ---- _stream_replay wildcard expansion -------------------------------------


def test_stream_replay_expands_glob(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    fixture = Path(__file__).parent / "fixtures" / "synthetic_brake_corner.csv"
    a = sessions / "capture_20260101_120000.csv"
    b = sessions / "capture_20260101_130000.csv"
    a.write_text(fixture.read_text())
    b.write_text(fixture.read_text())

    args = parse_args(["--source", str(sessions / "capture_*.csv")])
    args.realtime = False  # ensure attr exists
    stream, rx = _stream_replay(args)
    pkts = list(stream)
    assert rx is None
    assert len(pkts) == 24  # fixture has 24 rows


def test_stream_replay_glob_no_match_raises(tmp_path: Path) -> None:
    args = parse_args(["--source", str(tmp_path / "no-such-*.csv")])
    args.realtime = False
    with pytest.raises(FileNotFoundError, match="no files match pattern"):
        list(_stream_replay(args)[0])


def test_stream_replay_literal_path_unchanged(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "synthetic_brake_corner.csv"
    args = parse_args(["--source", str(fixture)])
    args.realtime = False
    stream, _ = _stream_replay(args)
    assert len(list(stream)) == 24


# ---- status event hooks use the right Track attributes --------------------


def test_track_object_has_attributes_status_emit_uses() -> None:
    """Regression for AttributeError: 'Track' object has no attribute 'name'.

    main.py calls ``status.emit('track', id=tr.id, name=tr.display_name)``
    immediately after track_detector.feed() returns a Track. If the Track
    dataclass field is renamed and main.py isn't updated, the receive loop
    crashes mid-race. This test pins the contract.
    """
    from dataclasses import fields

    from gt7coach.tracks.database import Track

    field_names = {f.name for f in fields(Track)}
    # main.py:486 -> status.emit("track", id=tr.id, name=tr.display_name)
    assert "id" in field_names
    assert "display_name" in field_names
