"""Config editor — a QDialog binding the most-edited fields of
:class:`gt7coach.config.LoadedConfig` to widgets.

The dialog loads ``./config.yaml`` (or default settings if absent),
lets the user tweak provider / driver-style / car-class / track /
voice engine / voice speed / cooldown / log-dir, and writes back via
:func:`gt7coach.config.save`. Changes take effect on the next ``Start``
(the running subprocess keeps its current settings).

Detector thresholds and per-detector configs are intentionally NOT
exposed here — they're niche and easy to fat-finger.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gt7coach.config import LoadedConfig, default_config, load, save

log = logging.getLogger(__name__)

_PROVIDER_CHOICES = ["gemini", "anthropic", "openai", "ollama", "mock"]
_VOICE_CHOICES = ["pyttsx3", "piper", "null"]
_DRIVER_STYLE_CHOICES = ["smooth", "aggressive", "learning"]


class ConfigDialog(QDialog):
    """Modal dialog for editing config.yaml."""

    def __init__(self, parent: QWidget | None = None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GT7 AI Coach — Configuration")
        self.setModal(True)
        self.resize(540, 500)
        self._path = path or Path.cwd() / "config.yaml"

        try:
            self._cfg: LoadedConfig = load(self._path) if self._path.is_file() else default_config()
        except Exception as exc:
            log.warning("config load failed (%s); using defaults", exc)
            self._cfg = default_config()

        # --- form fields --------------------------------------------------
        self._ps5_ip = QLineEdit(self._cfg.network.ps5_ip or "auto")
        self._provider = QComboBox()
        self._provider.addItems(_PROVIDER_CHOICES)
        self._provider.setCurrentText(self._cfg.coach_provider)
        self._model = QLineEdit(self._cfg.coach_model or "")
        self._model.setPlaceholderText("blank = provider default")
        self._driver_style = QComboBox()
        self._driver_style.addItems(_DRIVER_STYLE_CHOICES)
        self._driver_style.setCurrentText(self._cfg.advisor.driver_style)
        self._car_class = QLineEdit(self._cfg.coach_car_class)
        self._car_class.setPlaceholderText('e.g. "Gr.3 RWD"')
        self._track = QLineEdit(self._cfg.coach_track or "")
        self._track.setPlaceholderText("blank = auto-detect")
        self._cooldown = QDoubleSpinBox()
        self._cooldown.setRange(0.5, 30.0)
        self._cooldown.setSingleStep(0.5)
        self._cooldown.setValue(self._cfg.rate_limiter.global_cooldown_s)
        self._cooldown.setSuffix(" s")
        self._voice_engine = QComboBox()
        self._voice_engine.addItems(_VOICE_CHOICES)
        self._voice_engine.setCurrentText(self._cfg.voice.engine)
        self._voice_speed = QSpinBox()
        self._voice_speed.setRange(120, 320)
        self._voice_speed.setValue(self._cfg.voice.speed)
        self._voice_speed.setSuffix(" wpm")
        self._log_dir = QLineEdit(self._cfg.session.log_dir)
        self._generate_summary = QCheckBox("Generate end-of-session summary")
        self._generate_summary.setChecked(self._cfg.session.generate_summary)

        # --- layout -------------------------------------------------------
        form = QFormLayout()
        form.addRow("PS5 IP:", self._ps5_ip)
        form.addRow(QLabel("<b>Coach</b>"))
        form.addRow("Provider:", self._provider)
        form.addRow("Model override:", self._model)
        form.addRow("Driver style:", self._driver_style)
        form.addRow("Car class:", self._car_class)
        form.addRow("Force track id:", self._track)
        form.addRow("Cooldown between advice:", self._cooldown)
        form.addRow(QLabel("<b>Voice</b>"))
        form.addRow("Engine:", self._voice_engine)
        form.addRow("Rate:", self._voice_speed)
        form.addRow(QLabel("<b>Session</b>"))
        form.addRow("Log directory:", self._log_dir)
        form.addRow("", self._generate_summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addLayout(form)
        outer.addStretch(1)
        path_hint = QLabel(f"Will write: <code>{self._path}</code>")
        path_hint.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(path_hint)
        outer.addWidget(buttons)

    # ---- save -------------------------------------------------------------

    def _save_and_close(self) -> None:
        cfg = self._cfg
        cfg.network.ps5_ip = self._ps5_ip.text().strip() or "auto"
        cfg.coach_provider = self._provider.currentText()
        cfg.coach_model = self._model.text().strip() or None
        cfg.advisor.driver_style = self._driver_style.currentText()
        cfg.coach_car_class = self._car_class.text().strip()
        cfg.coach_track = self._track.text().strip() or None
        cfg.rate_limiter.global_cooldown_s = float(self._cooldown.value())
        cfg.voice.engine = self._voice_engine.currentText()
        cfg.voice.speed = int(self._voice_speed.value())
        cfg.session.log_dir = self._log_dir.text().strip() or "./sessions"
        cfg.session.generate_summary = bool(self._generate_summary.isChecked())

        try:
            save(cfg, self._path)
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", f"Could not write {self._path}:\n{exc}")
            return
        QMessageBox.information(
            self,
            "Saved",
            f"Config written to:\n{self._path}\n\nChanges take effect on the next Start.",
        )
        self.accept()
