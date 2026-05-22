"""Live status widget — connection / track / corners / last advice / laps."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from gt7coach.gui.log_tail import StatusEvent


def _fmt_laptime(ms: int | None) -> str:
    if ms is None or ms <= 0:
        return "—"
    s, frac = divmod(ms, 1000)
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}.{frac:03d}"


class _Dot(QFrame):
    """Tiny coloured circle for the connection status indicator."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(12, 12)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._color = "#888888"
        self._restyle()

    def set_color(self, color: str) -> None:
        self._color = color
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(
            f"background-color: {self._color};border-radius: 6px;border: 1px solid rgba(0,0,0,40);"
        )


class StatusPanel(QWidget):
    """The left-column live status panel. Reacts to StatusTail events."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # --- Connection ----------------------------------------------------
        self._dot = _Dot()
        self._conn_label = QLabel("Idle")
        conn_row = QWidget()
        conn_layout = QFormLayout(conn_row)
        conn_layout.setContentsMargins(0, 0, 0, 0)
        row = QWidget()
        from PySide6.QtWidgets import QHBoxLayout

        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addWidget(self._dot)
        rl.addWidget(self._conn_label, stretch=1)
        conn_layout.addRow("Receiver:", row)

        self._track_label = QLabel("—")
        conn_layout.addRow("Track:", self._track_label)

        conn_box = QGroupBox("Connection")
        conn_box.setLayout(conn_layout)

        # --- Run stats -----------------------------------------------------
        self._corner_count = QLabel("0")
        self._event_count = QLabel("0")
        self._last_advice = QLabel("—")
        self._last_advice.setWordWrap(True)
        f = QFont()
        f.setItalic(True)
        self._last_advice.setFont(f)

        run_layout = QFormLayout()
        run_layout.addRow("Corners detected:", self._corner_count)
        run_layout.addRow("Events fired:", self._event_count)
        run_layout.addRow("Last advice:", self._last_advice)
        run_box = QGroupBox("Run")
        run_box.setLayout(run_layout)

        # --- Lap times -----------------------------------------------------
        self._current_lap = QLabel("0")
        self._last_lap = QLabel("—")
        self._best_lap = QLabel("—")
        lap_layout = QFormLayout()
        lap_layout.addRow("Lap:", self._current_lap)
        lap_layout.addRow("Last lap:", self._last_lap)
        lap_layout.addRow("Best lap:", self._best_lap)
        lap_box = QGroupBox("Lap times")
        lap_box.setLayout(lap_layout)

        # --- Compose -------------------------------------------------------
        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(conn_box)
        outer.addWidget(run_box)
        outer.addWidget(lap_box)
        outer.addStretch(1)

        self._corners_seen = 0
        self._events_seen = 0

    # ---- event sink --------------------------------------------------------

    def on_status_event(self, ev: StatusEvent) -> None:
        if ev.type == "rx_stats":
            hz = float(ev.payload.get("hz", 0.0))
            silent = float(ev.payload.get("silent_for_s", 0.0))
            if hz > 0:
                self._dot.set_color("#37c850")  # green
                self._conn_label.setText(f"{hz:.1f} Hz")
            elif silent >= 16.0:
                self._dot.set_color("#e34c4c")  # red
                self._conn_label.setText(f"deaf for {silent:.0f}s — PS5 disconnected")
            else:
                self._dot.set_color("#e0c14c")  # amber
                self._conn_label.setText(f"silent for {silent:.0f}s")

        elif ev.type == "track":
            name = str(ev.payload.get("name", ev.payload.get("id", "—")))
            self._track_label.setText(name)

        elif ev.type == "corner":
            self._corners_seen += 1
            self._corner_count.setText(str(self._corners_seen))
            self._events_seen += int(ev.payload.get("event_count", 0))
            self._event_count.setText(str(self._events_seen))

        elif ev.type == "advice":
            text = str(ev.payload.get("advice", "")).strip()
            if text:
                self._last_advice.setText(text)

        elif ev.type == "lap":
            self._current_lap.setText(str(ev.payload.get("lap", "—")))
            self._last_lap.setText(_fmt_laptime(int(ev.payload.get("last_lap_ms", 0))))
            self._best_lap.setText(_fmt_laptime(int(ev.payload.get("best_lap_ms", 0))))

    # ---- lifecycle helpers -------------------------------------------------

    def reset(self) -> None:
        self._dot.set_color("#888888")
        self._conn_label.setText("Idle")
        self._track_label.setText("—")
        self._corner_count.setText("0")
        self._event_count.setText("0")
        self._last_advice.setText("—")
        self._current_lap.setText("0")
        self._last_lap.setText("—")
        self._best_lap.setText("—")
        self._corners_seen = 0
        self._events_seen = 0
