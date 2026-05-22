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

# Maps provider name → env var that the provider's SDK / our auto-picker
# looks for. ollama / mock need no key, so they're absent.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a very simple .env (KEY=value lines, no quoting). Returns
    an empty dict if the file doesn't exist or is unreadable."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
    return out


def _write_env_file(path: Path, values: dict[str, str], *, header: str = "") -> None:
    """Rewrite the .env file with ``values``, preserving any comments
    that were in the original file. Lines whose KEY is in ``values`` get
    the new value; lines whose KEY is missing from ``values`` are
    preserved verbatim; new keys are appended at the end."""
    existing_lines: list[str] = []
    seen_keys: set[str] = set()
    if path.is_file():
        try:
            existing_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("could not read %s for merge: %s", path, exc)

    new_lines: list[str] = []
    if header:
        new_lines.append(f"# {header}")
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.partition("=")[0].strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            seen_keys.add(key)
        else:
            new_lines.append(line)
    for key, value in values.items():
        if key not in seen_keys:
            new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class ConfigDialog(QDialog):
    """Modal dialog for editing config.yaml."""

    def __init__(self, parent: QWidget | None = None, path: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GT7 AI Coach — Configuration")
        self.setModal(True)
        self.resize(540, 500)
        self._path = path or Path.cwd() / "config.yaml"
        self._env_path = self._path.with_name(".env")

        try:
            self._cfg: LoadedConfig = load(self._path) if self._path.is_file() else default_config()
        except Exception as exc:
            log.warning("config load failed (%s); using defaults", exc)
            self._cfg = default_config()

        # Load existing .env so we can show "key already set" without ever
        # displaying the value.
        self._env_values = _read_env_file(self._env_path)

        # --- form fields --------------------------------------------------
        self._ps5_ip = QLineEdit(self._cfg.network.ps5_ip or "auto")
        self._provider = QComboBox()
        self._provider.addItems(_PROVIDER_CHOICES)
        self._provider.setCurrentText(self._cfg.coach_provider)
        self._model = QLineEdit(self._cfg.coach_model or "")
        self._model.setPlaceholderText("blank = provider default")

        # API key field. Masked by default. Empty text means "don't change
        # the existing key in .env" (existence shown in the placeholder).
        self._api_key = QLineEdit("")
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key = QCheckBox("Show")
        self._show_key.toggled.connect(
            lambda checked: self._api_key.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        self._refresh_api_key_placeholder()
        self._provider.currentTextChanged.connect(lambda _t: self._refresh_api_key_placeholder())
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

        # API key row — line edit + Show checkbox in the same cell.
        from PySide6.QtWidgets import QHBoxLayout

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self._api_key, stretch=1)
        key_layout.addWidget(self._show_key)
        form.addRow("API key:", key_row)
        disclaimer = QLabel(
            "<i>Your API key is private. It is stored only on this PC in <code>.env</code> "
            "and sent only to the chosen provider's official endpoint. "
            "<b>Don't share it</b> — anyone with the key can use up your quota / spend on "
            "your account.</i>"
        )
        disclaimer.setWordWrap(True)
        disclaimer.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("", disclaimer)

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

    def _refresh_api_key_placeholder(self) -> None:
        """Update the field placeholder + behavior based on the chosen
        provider. Ollama / mock have no key — disable the field entirely.
        For providers with an existing key in .env, show "(set; leave
        blank to keep)" so the user knows there's already one configured."""
        provider = self._provider.currentText()
        env_var = _PROVIDER_ENV_VAR.get(provider)
        if env_var is None:
            self._api_key.clear()
            self._api_key.setPlaceholderText("(this provider needs no key)")
            self._api_key.setEnabled(False)
            self._show_key.setEnabled(False)
            return
        self._api_key.setEnabled(True)
        self._show_key.setEnabled(True)
        existing = self._env_values.get(env_var, "")
        if existing:
            masked = existing[:6] + "…" + existing[-4:] if len(existing) > 12 else "(short)"
            self._api_key.setPlaceholderText(
                f"{env_var}={masked} (leave blank to keep, or paste new)"
            )
        else:
            self._api_key.setPlaceholderText(f"paste your {env_var} here")

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

        # If the user pasted a new API key, write it to .env (merging with
        # whatever was already there — never destroying existing keys for
        # other providers).
        new_key = self._api_key.text().strip()
        env_var = _PROVIDER_ENV_VAR.get(cfg.coach_provider)
        env_written = False
        if env_var and new_key:
            try:
                merged = dict(self._env_values)
                merged[env_var] = new_key
                _write_env_file(
                    self._env_path,
                    merged,
                    header="Written by gt7coach GUI. Do NOT share this file.",
                )
                env_written = True
                self._env_values = merged
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Save failed (.env)",
                    f"Wrote config.yaml but couldn't update .env:\n{exc}\n\n"
                    f"Paste your key into {self._env_path} manually:\n"
                    f"  {env_var}=<your key>",
                )
                return

        msg = f"Config written to:\n{self._path}\n"
        if env_written:
            msg += f"\nAPI key updated in:\n{self._env_path}\n"
        msg += "\nChanges take effect on the next Start."
        QMessageBox.information(self, "Saved", msg)
        self.accept()
