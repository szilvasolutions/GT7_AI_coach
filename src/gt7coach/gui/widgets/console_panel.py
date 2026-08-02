"""Console panel: the live subprocess log with its own header bar.

Wraps :class:`LiveLog` and adds what a raw QPlainTextEdit can't offer:

* a slim header (CONSOLE title, Clear, and a ✕ that hides the panel),
* show/hide as a first-class feature — MainWindow exposes it in the
  View menu (Ctrl+L) and remembers the choice across sessions,
* log-level colouring (warnings amber, errors red, spoken advice green)
  so the one line that matters isn't buried in white-on-black noise.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gt7coach.gui.theme import LOG_COLOR_ADVICE, LOG_COLOR_ERROR, LOG_COLOR_WARNING

_MAX_LINES = 1000


class LiveLog(QPlainTextEdit):
    """Auto-scrolling, level-coloured view of the subprocess's log lines."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("consoleView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_LINES)
        # Long log lines scroll horizontally rather than wrapping mid-line
        # (timestamps + logger names stay aligned vertically).
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        f = QFont("Consolas, Monaco, monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)

    @staticmethod
    def _colour_for(line: str) -> str | None:
        if " ERROR " in line or " CRITICAL " in line:
            return LOG_COLOR_ERROR
        if " WARNING " in line:
            return LOG_COLOR_WARNING
        if "coach -> " in line or "vr alert" in line:
            return LOG_COLOR_ADVICE
        return None

    def append_line(self, line: str) -> None:
        """Append one line (coloured by level) and scroll to the bottom."""
        colour = self._colour_for(line)
        escaped = html.escape(line)
        if colour is None:
            self.appendHtml(f"<span style='white-space:pre'>{escaped}</span>")
        else:
            self.appendHtml(f"<span style='white-space:pre;color:{colour}'>{escaped}</span>")
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def clear_log(self) -> None:
        self.clear()


class ConsolePanel(QWidget):
    """Header bar + LiveLog. ``hide_requested`` fires when ✕ is clicked so
    MainWindow can keep the View-menu checkbox in sync."""

    hide_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("consolePanel")
        self.log = LiveLog(self)

        header = QWidget(self)
        header.setObjectName("consoleHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 4, 4, 4)
        hl.setSpacing(4)
        title = QLabel("CONSOLE")
        title.setObjectName("consoleTitle")
        hl.addWidget(title)
        hl.addStretch(1)

        clear_btn = QToolButton(header)
        clear_btn.setObjectName("consoleButton")
        clear_btn.setText("Clear")
        clear_btn.setToolTip("Clear the console")
        clear_btn.clicked.connect(self.log.clear_log)
        hl.addWidget(clear_btn)

        hide_btn = QToolButton(header)
        hide_btn.setObjectName("consoleButton")
        hide_btn.setText("✕")
        hide_btn.setToolTip("Hide the console (View ▸ Console log, Ctrl+L)")
        hide_btn.clicked.connect(self.hide_requested.emit)
        hl.addWidget(hide_btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addWidget(self.log, stretch=1)

    # Delegates so MainWindow wiring stays one-liner signal connects.
    def append_line(self, line: str) -> None:
        self.log.append_line(line)

    def clear_log(self) -> None:
        self.log.clear_log()
