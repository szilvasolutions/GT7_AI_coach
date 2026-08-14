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
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from gt7coach.config import (
    LoadedConfig,
    default_config,
    load,
    looks_like_gt7_config,
    save,
)

log = logging.getLogger(__name__)

_PROVIDER_CHOICES = ["gemini", "anthropic", "openai", "ollama", "mock"]
_VOICE_CHOICES = ["pyttsx3", "piper", "null"]
_DRIVER_STYLE_CHOICES = ["smooth", "aggressive", "learning"]
_LAP_ANNOUNCE_CHOICES = [
    ("both", "Both: best-lap callout + AI recommendation"),
    ("recommendation", "AI recommendation only"),
    ("best_lap", "Best lap only (no AI, no quota burn)"),
]

# Maps provider name → env var that the provider's SDK / our auto-picker
# looks for. ollama / mock need no key, so they're absent.
_PROVIDER_ENV_VAR: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


# Hover help for every field. Qt renders a tooltip containing rich text as
# rich text, so these can wrap properly instead of being one long line.
_FIELD_HELP: dict[str, str] = {
    "_ps5_ip": (
        "<b>IP address of your PS4/PS5.</b><br>"
        "Leave as <tt>auto</tt> and the coach finds the console by broadcast — "
        "that works on almost every home network.<br><br>"
        "Set it by hand only if discovery fails, e.g. the console is on a "
        "different subnet or VLAN. Find it on the console under "
        "Settings ▸ Network ▸ Connection Status."
    ),
    "_provider": (
        "<b>Which AI writes the coaching lines.</b><br>"
        "<b>gemini</b> — free tier, fast, the recommended starting point.<br>"
        "<b>anthropic</b> / <b>openai</b> — paid, need their own API key.<br>"
        "<b>ollama</b> — runs a model on your own PC, no key, no internet, "
        "but needs a decent GPU.<br>"
        "<b>mock</b> — canned phrases, no AI at all. Handy for testing the "
        "voice without spending anything."
    ),
    "_model": (
        "<b>Leave blank unless you know you want a different model.</b><br>"
        "Blank uses each provider's default, chosen to be fast enough to "
        "speak before the next corner:<br>"
        "gemini → <tt>gemini-2.5-flash-lite</tt><br>"
        "anthropic → <tt>claude-haiku-4-5</tt><br>"
        "openai → <tt>gpt-4o-mini</tt><br>"
        "ollama → <tt>llama3.1:8b</tt><br><br>"
        "Bigger models write better lines but often arrive too late to be "
        "useful mid-lap."
    ),
    "_api_key": (
        "<b>Your key for the selected provider.</b><br>"
        "Stored in a <tt>.env</tt> file next to the app — never sent anywhere "
        "except that provider. Get a free Gemini key at "
        "<tt>aistudio.google.com/apikey</tt>.<br><br>"
        "Leave blank to keep the key you already saved."
    ),
    "_driver_style": (
        "<b>The tone the coach takes.</b><br>"
        "<b>smooth</b> — calm and technical.<br>"
        "<b>aggressive</b> — blunt and pushy.<br>"
        "<b>learning</b> — patient and encouraging, for a track you don't know yet."
    ),
    "_car_class": (
        "<b>What you're driving, in GT7's own words</b> — e.g. "
        "<tt>Gr.3 RWD</tt>, <tt>N300 FF</tt>.<br>"
        "Goes into every prompt, so the advice suits the car: a Gr.3 RWD gets "
        "told about throttle-on oversteer, an FF road car about understeer.<br><br>"
        "Optional, but it noticeably improves the coaching."
    ),
    "_track": (
        "<b>Leave blank.</b> The coach recognises the circuit from your GPS "
        "position, across 84 tracks.<br><br>"
        "Only fill this in if detection picks the wrong track — run "
        "<tt>gt7coach-list-tracks</tt> to see the ids."
    ),
    "_cooldown": (
        "<b>Minimum seconds between spoken lines.</b><br>"
        "Lower is chattier. Below about 3 s the coach talks over itself on "
        "twisty circuits; 4 to 6 s suits most tracks."
    ),
    "_voice_engine": (
        "<b>How the coach speaks.</b><br>"
        "<b>pyttsx3</b> — your Windows system voice. Instant, robotic, no setup.<br>"
        "<b>piper</b> — a much more natural neural voice, needs a model file "
        "downloaded first.<br>"
        "<b>null</b> — silent; advice is written to the log only."
    ),
    "_voice_speed": (
        "<b>Speaking rate in words per minute.</b><br>"
        "200 is the default. Faster fits more into a short straight but gets "
        "harder to follow at speed."
    ),
    "_log_dir": (
        "<b>Where each session's recording goes.</b><br>"
        "One folder per run, holding the telemetry CSV, every event, every "
        "prompt and reply the AI saw, and <tt>debug.log</tt>.<br><br>"
        "Attach that folder when reporting a problem. It's also what "
        "<tt>build_demo_video.py</tt> reads to sync a demo recording."
    ),
    "_generate_summary": (
        "<b>Speak a short debrief when the session ends</b> — what to work on "
        "next time, based on the faults that came up most."
    ),
}


def _apply_field_help(dialog: QDialog) -> None:
    """Attach the tooltips above to whichever fields the dialog has."""
    for attr, text in _FIELD_HELP.items():
        widget = getattr(dialog, attr, None)
        if widget is not None:
            widget.setToolTip(text)


class _HelpBadge(QLabel):
    """A round "?" badge that shows its help on hover *and* on click.

    setToolTip() alone wasn't enough in practice — the badge rendered but
    hovering it produced nothing on the reporter's machine. Rather than keep
    guessing at why Qt withheld it, show the text explicitly: enterEvent
    pops it immediately (no hover delay to wait out), and a click pops it
    too, so the badge works even where automatic tooltips don't.
    """

    def __init__(self, text: str) -> None:
        super().__init__("?")
        self._help = text
        self.setObjectName("helpIcon")
        self.setToolTip(text)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setFixedSize(18, 18)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def _popup(self) -> None:
        # Anchor below the badge so the text never sits under the cursor.
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), self._help, self)

    def enterEvent(self, event) -> None:
        self._popup()
        super().enterEvent(event)

    def mousePressEvent(self, event) -> None:
        self._popup()
        super().mousePressEvent(event)


def _help_icon(text: str) -> QLabel:
    """A "?" badge for ``text`` — see :class:`_HelpBadge`."""
    return _HelpBadge(text)


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
        self.resize(560, 680)
        self._path = path or Path.cwd() / "config.yaml"
        # Set on a successful save so the main window can mirror it into the
        # toolbar, which is what actually reaches the coach.
        self.saved_car_class: str | None = None
        self._env_path = self._path.with_name(".env")

        try:
            self._cfg: LoadedConfig = load(self._path) if self._path.is_file() else default_config()
        except Exception as exc:
            log.warning("config load failed (%s); using defaults", exc)
            self._cfg = default_config()

        # Load existing .env so we can show "key already set" without ever
        # displaying the value.
        self._env_values = _read_env_file(self._env_path)

        # On a fresh install there's no config.yaml, so the provider would
        # fall back to the built-in default (anthropic) and the dialog would
        # ask for an ANTHROPIC_API_KEY while the user's Gemini key sat right
        # there in .env — which reads as "my key vanished". Start on a
        # provider they actually have a key for.
        if not self._path.is_file():
            for name in _PROVIDER_CHOICES:
                var = _PROVIDER_ENV_VAR.get(name)
                if var and self._env_values.get(var):
                    self._cfg.coach_provider = name
                    break

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
        self._lap_announce_mode = QComboBox()
        for key, label in _LAP_ANNOUNCE_CHOICES:
            self._lap_announce_mode.addItem(label, userData=key)
        # Select the row whose userData matches the current config value.
        for i in range(self._lap_announce_mode.count()):
            if self._lap_announce_mode.itemData(i) == self._cfg.session.lap_announce_mode:
                self._lap_announce_mode.setCurrentIndex(i)
                break

        # VR voice-HUD alerts — one checkbox per alert. These mirror
        # cfg.vr_alerts.*_enabled and apply on the next Start.
        vr = self._cfg.vr_alerts
        self._vr_tyre = QCheckBox(
            f"Tire temperature  (warn outside {int(vr.tyre_temp_cold_c)}-{int(vr.tyre_temp_hot_c)} °C)"
        )
        self._vr_tyre.setChecked(vr.tyre_temp_enabled)
        self._vr_fuel = QCheckBox("Low fuel + laps-remaining")
        self._vr_fuel.setChecked(vr.fuel_enabled)
        self._vr_coolant = QCheckBox(
            f"Oil / water overheat  (>{int(vr.oil_hot_c)} / {int(vr.water_hot_c)} °C)"
        )
        self._vr_coolant.setChecked(vr.coolant_enabled)
        self._vr_shift = QCheckBox("Shift-up beep (chatty)")
        self._vr_shift.setChecked(vr.shift_assist_enabled)
        self._vr_self_delta = QCheckBox("Self-delta vs personal best (each lap)")
        self._vr_self_delta.setChecked(vr.self_delta_enabled)

        # --- layout -------------------------------------------------------
        form = QFormLayout()
        _apply_field_help(self)
        self._add_row(form, "PS5 IP:", self._ps5_ip, "_ps5_ip")
        form.addRow(QLabel("<b>Coach</b>"))
        self._add_row(form, "Provider:", self._provider, "_provider")
        self._add_row(form, "Model override:", self._model, "_model")

        # API key row — line edit + Show checkbox in the same cell.
        from PySide6.QtWidgets import QHBoxLayout

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self._api_key, stretch=1)
        key_layout.addWidget(self._show_key)
        self._add_row(form, "API key:", key_row, "_api_key")
        disclaimer = QLabel(
            "<i>Your API key is private. It is stored only on this PC in <code>.env</code> "
            "and sent only to the chosen provider's official endpoint. "
            "<b>Don't share it</b> — anyone with the key can use up your quota / spend on "
            "your account.</i>"
        )
        disclaimer.setWordWrap(True)
        disclaimer.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("", disclaimer)

        self._add_row(form, "Driver style:", self._driver_style, "_driver_style")
        self._add_row(form, "Car class:", self._car_class, "_car_class")
        self._add_row(form, "Force track id:", self._track, "_track")
        self._add_row(form, "Cooldown between advice:", self._cooldown, "_cooldown")
        form.addRow(QLabel("<b>Voice</b>"))
        self._add_row(form, "Engine:", self._voice_engine, "_voice_engine")
        self._add_row(form, "Rate:", self._voice_speed, "_voice_speed")
        form.addRow(QLabel("<b>Session</b>"))
        self._add_row(form, "Log directory:", self._log_dir, "_log_dir")
        self._add_row(form, "", self._generate_summary, "_generate_summary")
        form.addRow("End-of-lap announce:", self._lap_announce_mode)
        form.addRow(QLabel("<b>VR Alerts</b>  (selectable voice-HUD callouts)"))
        form.addRow("", self._vr_tyre)
        form.addRow("", self._vr_fuel)
        form.addRow("", self._vr_coolant)
        form.addRow("", self._vr_shift)
        form.addRow("", self._vr_self_delta)

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

    def _add_row(self, form: QFormLayout, label: str, widget: QWidget, attr: str) -> None:
        """Add a form row with a "?" badge, and the same help on the label."""
        help_text = _FIELD_HELP.get(attr, "")
        if not help_text:
            form.addRow(label, widget)
            return

        from PySide6.QtWidgets import QHBoxLayout

        cell = QWidget()
        row = QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(widget, stretch=1)
        row.addWidget(_help_icon(help_text))

        label_widget = QLabel(label)
        label_widget.setToolTip(help_text)  # hovering the label is what people try first
        form.addRow(label_widget, cell)

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
            # Dots, not a truncated copy of the key: an empty-looking box
            # reads as "my key is gone", and echoing even part of a secret
            # into a screenshot-able field is a habit worth not having.
            self._api_key.setPlaceholderText(
                "•••••••••••••••••  saved — leave blank to keep it, or paste a new one"
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
        mode_data = self._lap_announce_mode.currentData()
        if mode_data:
            cfg.session.lap_announce_mode = str(mode_data)
        cfg.vr_alerts.tyre_temp_enabled = bool(self._vr_tyre.isChecked())
        cfg.vr_alerts.fuel_enabled = bool(self._vr_fuel.isChecked())
        cfg.vr_alerts.coolant_enabled = bool(self._vr_coolant.isChecked())
        cfg.vr_alerts.shift_assist_enabled = bool(self._vr_shift.isChecked())
        cfg.vr_alerts.self_delta_enabled = bool(self._vr_self_delta.isChecked())

        # Never clobber somebody else's config.yaml. The default path is
        # ./config.yaml, and the frozen .exe inherits whatever directory
        # Explorer launched it from — which may already hold an unrelated
        # config.yaml (Home Assistant's, for one).
        if self._path.is_file() and not looks_like_gt7_config(self._path):
            QMessageBox.critical(
                self,
                "Refusing to overwrite",
                f"{self._path}\n\nalready exists and belongs to another program — "
                "it has none of the sections gt7coach uses. Saving would destroy "
                "it.\n\nMove GT7Coach.exe to its own folder, or start it from one, "
                "and try again.",
            )
            return

        try:
            save(cfg, self._path)
            # Only after the write succeeds — a refused save must not push a
            # value into the toolbar.
            self.saved_car_class = cfg.coach_car_class
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
