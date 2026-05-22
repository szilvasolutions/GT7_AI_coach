"""Filtered live tail of the subprocess stderr stream.

We let the underlying ``gt7coach-coach`` process write the comprehensive
``debug.log`` per session; the GUI's live log shows the same lines as
the user would see on stderr at INFO+ level (the noise filter from
main.py is already applied at source).
"""

from __future__ import annotations

from PySide6.QtGui import QFont, QTextCursor, QTextOption
from PySide6.QtWidgets import QPlainTextEdit, QWidget

_MAX_LINES = 1000


class LiveLog(QPlainTextEdit):
    """Auto-scrolling text view of the subprocess's stderr lines."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_LINES)
        # Long log lines scroll horizontally rather than wrapping mid-line
        # (timestamps + logger names stay aligned vertically).
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        f = QFont("Consolas, Monaco, monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self.setFont(f)

    def append_line(self, line: str) -> None:
        """Append one line and scroll to the bottom."""
        self.appendPlainText(line)
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)

    def clear_log(self) -> None:
        self.clear()
