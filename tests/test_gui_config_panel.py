"""ConfigDialog: hover help, and settings surviving a save/reopen cycle.

Reported from a live install: "the AI API seems to be lost after saving and
opening the config again", and the tooltips appeared to be missing.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed; skipping GUI tests")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from gt7coach.gui.config_panel import _FIELD_HELP, ConfigDialog


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _no_modal_dialogs(monkeypatch):
    """Offscreen, a QMessageBox blocks forever waiting for a click."""
    for name in ("information", "critical", "warning"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: None))


def test_every_field_has_hover_help(qapp, tmp_path):
    dlg = ConfigDialog(path=tmp_path / "config.yaml")
    for attr in _FIELD_HELP:
        widget = getattr(dlg, attr, None)
        assert widget is not None, f"{attr} named in _FIELD_HELP but not on the dialog"
        assert widget.toolTip(), f"{attr} has no tooltip"


def test_settings_survive_save_and_reopen(qapp, tmp_path):
    path = tmp_path / "config.yaml"
    dlg = ConfigDialog(path=path)
    dlg._provider.setCurrentText("anthropic")
    dlg._car_class.setText("Gr.3 RWD")
    dlg._voice_speed.setValue(180)
    dlg._cooldown.setValue(6.0)
    dlg._ps5_ip.setText("192.168.1.120")
    dlg._save_and_close()

    assert path.is_file(), "Save did not write config.yaml"
    again = ConfigDialog(path=path)
    assert again._provider.currentText() == "anthropic"
    assert again._car_class.text() == "Gr.3 RWD"
    assert again._voice_speed.value() == 180
    assert again._cooldown.value() == pytest.approx(6.0)
    assert again._ps5_ip.text() == "192.168.1.120"


def test_api_key_is_stored_and_reported_as_saved(qapp, tmp_path):
    """The key lives in .env, never in config.yaml, and the field stays
    blank on reopen — but it must SAY a key is stored, or it reads as lost."""
    path = tmp_path / "config.yaml"
    dlg = ConfigDialog(path=path)
    dlg._provider.setCurrentText("anthropic")
    dlg._api_key.setText("sk-ant-secret-key-1234567890")
    dlg._save_and_close()

    env = tmp_path / ".env"
    assert env.is_file(), "the API key was not written to .env"
    body = env.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=sk-ant-secret-key-1234567890" in body
    assert "sk-ant-secret-key" not in path.read_text(encoding="utf-8")

    again = ConfigDialog(path=path)
    assert again._api_key.text() == "", "the key must never be pre-filled"
    hint = again._api_key.placeholderText()
    assert "•" in hint, f"a saved key should be shown as dots, got {hint!r}"
    assert "sk-ant-secret-key-1234567890" not in hint, "the key must not be echoed in full"


def test_no_key_prompts_to_paste_one(qapp, tmp_path):
    dlg = ConfigDialog(path=tmp_path / "config.yaml")
    dlg._provider.setCurrentText("gemini")
    hint = dlg._api_key.placeholderText()
    assert "•" not in hint
    assert "GEMINI_API_KEY" in hint


def test_keyless_providers_disable_the_field(qapp, tmp_path):
    dlg = ConfigDialog(path=tmp_path / "config.yaml")
    dlg._provider.setCurrentText("ollama")
    assert dlg._api_key.isEnabled() is False


def test_saving_keeps_an_existing_key_when_left_blank(qapp, tmp_path):
    path = tmp_path / "config.yaml"
    first = ConfigDialog(path=path)
    first._provider.setCurrentText("gemini")
    first._api_key.setText("AIzaSy-original-key-value")
    first._save_and_close()

    second = ConfigDialog(path=path)
    second._car_class.setText("N300 FF")  # change something else, not the key
    second._save_and_close()

    body = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AIzaSy-original-key-value" in body, "leaving the field blank wiped the key"
