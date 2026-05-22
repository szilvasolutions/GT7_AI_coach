"""Tests for ``gt7coach.config.save`` — must round-trip through ``load``."""

from __future__ import annotations

from pathlib import Path

from gt7coach.config import default_config, load, save


def test_save_then_load_preserves_user_visible_fields(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.network.ps5_ip = "192.168.1.99"
    cfg.network.heartbeat_seconds = 1.7
    cfg.coach_provider = "gemini"
    cfg.coach_model = "gemini-2.5-flash-lite"
    cfg.advisor.driver_style = "aggressive"
    cfg.coach_car_class = "Gr.3 RWD"
    cfg.coach_track = "deep_forest"
    cfg.rate_limiter.global_cooldown_s = 3.5
    cfg.voice.engine = "pyttsx3"
    cfg.voice.speed = 215
    cfg.session.log_dir = "./mysessions"
    cfg.session.generate_summary = False

    path = tmp_path / "config.yaml"
    save(cfg, path)

    re = load(path)
    assert re.network.ps5_ip == "192.168.1.99"
    assert re.network.heartbeat_seconds == 1.7
    assert re.coach_provider == "gemini"
    assert re.coach_model == "gemini-2.5-flash-lite"
    assert re.advisor.driver_style == "aggressive"
    assert re.coach_car_class == "Gr.3 RWD"
    assert re.coach_track == "deep_forest"
    assert re.rate_limiter.global_cooldown_s == 3.5
    assert re.voice.engine == "pyttsx3"
    assert re.voice.speed == 215
    assert re.session.log_dir == "./mysessions"
    assert re.session.generate_summary is False


def test_save_omits_blank_optional_fields(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.coach_car_class = ""  # blank
    cfg.coach_track = None
    cfg.coach_model = None
    path = tmp_path / "config.yaml"
    save(cfg, path)

    text = path.read_text(encoding="utf-8")
    # Optional fields should be omitted, not written as 'null' / empty string.
    assert "car_class" not in text
    assert "track:" not in text
    assert "model:" not in text


def test_save_includes_piper_settings_only_for_piper(tmp_path: Path) -> None:
    cfg = default_config()
    cfg.voice.engine = "piper"
    cfg.voice.piper_voice = "en_US-amy-medium"
    cfg.voice.piper_model_path = "/opt/piper/amy.onnx"
    path = tmp_path / "piper.yaml"
    save(cfg, path)
    text = path.read_text(encoding="utf-8")
    assert "piper_voice" in text
    assert "en_US-amy-medium" in text
    assert "piper_model_path" in text

    # Switch back to pyttsx3 — piper fields should disappear.
    cfg.voice.engine = "pyttsx3"
    path2 = tmp_path / "pyttsx3.yaml"
    save(cfg, path2)
    text2 = path2.read_text(encoding="utf-8")
    assert "piper_voice" not in text2
    assert "piper_model_path" not in text2
