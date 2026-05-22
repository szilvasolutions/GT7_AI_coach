"""Tests for ``gt7coach.setup_wizard``.

Strategy: monkeypatch stdin (via ``builtins.input``) + the few external
calls the wizard makes (``getpass``, ``discover_ps5``, ``make_provider``,
``make_voice``), then assert on the files produced in a tmp_path.
Drive a full scripted run-through; don't try to exercise every branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gt7coach import setup_wizard


@pytest.fixture
def scripted_inputs(monkeypatch):
    """Returns a callable that queues a list of stdin lines for input()."""
    queue: list[str] = []

    def feed(*lines: str) -> None:
        queue.extend(lines)

    def fake_input(prompt: str = "") -> str:
        if not queue:
            raise AssertionError(
                f"setup_wizard asked for more input than test provided. Last prompt: {prompt!r}"
            )
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return feed


@pytest.fixture
def mock_provider(monkeypatch):
    """Always-succeeds provider. Records the API key passed."""
    captured: dict[str, str] = {}

    class _OK:
        def complete(self, system, user, *, max_tokens=None):
            return "OK"

    def fake_make_provider(name, api_key=None, **kw):
        captured["provider"] = name
        captured["api_key"] = api_key or ""
        return _OK()

    monkeypatch.setattr("gt7coach.coach.providers.make_provider", fake_make_provider)
    return captured


@pytest.fixture
def mock_voice(monkeypatch):
    """No-op voice that records what was spoken."""
    spoken: list[str] = []

    class _Voice:
        def speak(self, text):
            spoken.append(text)

        def is_idle(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(
        "gt7coach.voice.make_voice",
        lambda name, rate=200, **kw: _Voice(),
    )
    return spoken


@pytest.fixture
def mock_discover(monkeypatch):
    """Pretends discovery succeeded at a fixed IP."""

    def fake_discover(cfg):
        return "10.0.0.42"

    monkeypatch.setattr("gt7coach.telemetry.receiver.discover_ps5", fake_discover)


@pytest.fixture
def mock_getpass(monkeypatch):
    """Returns a fixed API key without touching the terminal."""
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "test-api-key-1234567890")


def test_full_run_gemini_pyttsx3(
    tmp_path: Path,
    scripted_inputs,
    mock_provider,
    mock_voice,
    mock_discover,
    mock_getpass,
    monkeypatch,
):
    # Avoid pulling a real GEMINI_API_KEY from the env on this machine.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Disable SAPI voice enumeration (no pyttsx3 on CI/sandbox).
    monkeypatch.setattr(setup_wizard, "_pick_pyttsx3_voice", lambda: None)

    scripted_inputs(
        "y",  # Try to auto-discover PS5? -> yes
        "y",  # Use the discovered IP? -> yes
        "gemini",  # Provider
        "pyttsx3",  # Voice engine
        "200",  # Voice speed
        "y",  # Speak a test phrase? -> yes
        "smooth",  # Driver style
        "Gr.3 RWD",  # Car class
    )

    result = setup_wizard.run(target_dir=tmp_path)

    assert result.ps5_ip == "10.0.0.42"
    assert result.provider == "gemini"
    assert result.api_key == "test-api-key-1234567890"
    assert result.voice_engine == "pyttsx3"
    assert result.voice_speed == 200
    assert result.driver_style == "smooth"
    assert result.car_class == "Gr.3 RWD"

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=test-api-key-1234567890" in env_text

    yaml_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "ps5_ip: 10.0.0.42" in yaml_text
    assert "provider: gemini" in yaml_text
    assert "driver_style: smooth" in yaml_text
    assert 'car_class: "Gr.3 RWD"' in yaml_text
    assert "engine: pyttsx3" in yaml_text
    assert "speed: 200" in yaml_text


def test_ollama_skips_api_key(
    tmp_path: Path,
    scripted_inputs,
    mock_voice,
    mock_discover,
    monkeypatch,
):
    monkeypatch.setattr(setup_wizard, "_pick_pyttsx3_voice", lambda: None)

    scripted_inputs(
        # skip_discovery=True skips the "auto-discover?" yn, jumps to IP prompt.
        "192.168.1.50",  # PS5 IP
        "ollama",  # Provider (needs no key)
        "pyttsx3",  # Voice engine
        "200",  # Voice speed
        "n",  # Skip test phrase
        "aggressive",  # Driver style
        "",  # No car class
    )

    result = setup_wizard.run(target_dir=tmp_path, skip_discovery=True)

    assert result.ps5_ip == "192.168.1.50"
    assert result.provider == "ollama"
    assert result.api_key is None
    assert result.driver_style == "aggressive"
    assert result.car_class == ""

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    # Ollama needs no key; .env still gets written but with no key line.
    assert "_API_KEY=" not in env_text

    yaml_text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "provider: ollama" in yaml_text


def test_existing_files_are_backed_up(
    tmp_path: Path,
    scripted_inputs,
    mock_voice,
    monkeypatch,
):
    # Pre-existing .env and config.yaml should be moved aside.
    (tmp_path / ".env").write_text("OLD=keep-me\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("old: data\n", encoding="utf-8")

    monkeypatch.setattr(setup_wizard, "_pick_pyttsx3_voice", lambda: None)

    scripted_inputs(
        # skip_discovery=True jumps straight to IP prompt.
        "auto",  # Use auto at runtime
        "mock",  # Provider — no key, no smoke test path
        "pyttsx3",  # Voice engine
        "200",  # Voice speed
        "n",  # Skip test phrase
        "smooth",  # Driver style
        "",  # No car class
    )

    setup_wizard.run(target_dir=tmp_path, skip_discovery=True)

    new_env = (tmp_path / ".env").read_text(encoding="utf-8")
    new_yaml = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert "OLD=keep-me" not in new_env
    assert "old: data" not in new_yaml

    # The backup files must exist and contain the original content.
    env_backups = list(tmp_path.glob(".env.bak.*"))
    yaml_backups = list(tmp_path.glob("config.yaml.bak.*"))
    assert env_backups, "expected a backup of the old .env"
    assert yaml_backups, "expected a backup of the old config.yaml"
    assert "OLD=keep-me" in env_backups[0].read_text(encoding="utf-8")
    assert "old: data" in yaml_backups[0].read_text(encoding="utf-8")


def test_smoke_test_handles_provider_error(
    tmp_path: Path,
    scripted_inputs,
    mock_voice,
    monkeypatch,
):
    """If the first key smoke-test fails and user declines to retry,
    the wizard still completes and writes the key as-given."""

    class _BadProvider:
        def complete(self, *args, **kwargs):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(
        "gt7coach.coach.providers.make_provider",
        lambda name, api_key=None, **kw: _BadProvider(),
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "bad-key")
    monkeypatch.setattr(setup_wizard, "_pick_pyttsx3_voice", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    scripted_inputs(
        # skip_discovery=True jumps straight to IP prompt.
        "auto",  # IP
        "gemini",  # Provider
        "n",  # Don't try a different key
        "pyttsx3",  # Voice engine
        "200",  # Voice speed
        "n",  # Skip test phrase
        "smooth",  # Driver style
        "",  # No car class
    )

    result = setup_wizard.run(target_dir=tmp_path, skip_discovery=True)
    assert result.api_key == "bad-key"
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "GEMINI_API_KEY=bad-key" in env_text
