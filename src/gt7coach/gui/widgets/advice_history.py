"""Scrollable list of all advice + incident lines spoken this session.

Each entry: HH:MM:SS  •  <event_type>  •  "the advice text"
"""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from gt7coach.gui.log_tail import StatusEvent


class AdviceHistory(QListWidget):
    """Append-only history widget. Reset on Start."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        f = QFont()
        f.setPointSize(10)
        self.setFont(f)

    def on_status_event(self, ev: StatusEvent) -> None:
        if ev.type != "advice":
            return
        text = str(ev.payload.get("advice", "")).strip()
        if not text:
            return
        event_type = str(ev.payload.get("event_type") or "—")
        # ev.ts is unix time; format as HH:MM:SS local.
        try:
            wall = datetime.fromtimestamp(ev.ts).strftime("%H:%M:%S")
        except (OSError, ValueError):
            wall = time.strftime("%H:%M:%S")
        line = f"{wall}  •  {event_type:<28}  •  {text}"
        item = QListWidgetItem(line)
        item.setTextAlignment(Qt.AlignmentFlag.AlignLeft)
        self.addItem(item)
        self.scrollToBottom()

    def reset(self) -> None:
        self.clear()
