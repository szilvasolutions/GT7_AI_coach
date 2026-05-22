"""Lap-times table fed from ``lap`` status events.

Three columns: Lap # | Lap time | Δ to best. New laps append at the
bottom; the best lap is shown in bold + green text.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QHeaderView, QTableWidget, QTableWidgetItem, QWidget

from gt7coach.gui.log_tail import StatusEvent
from gt7coach.gui.widgets.status_panel import _fmt_laptime  # reuse formatter


class LapTable(QTableWidget):
    """Append-only table of laps. Best lap highlighted."""

    _BEST_COLOR = QColor("#1f7a3f")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["Lap", "Time", "Δ best"])
        self.verticalHeader().setVisible(False)
        self.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(True)
        hh = self.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._best_row: int = -1
        self._best_lap_ms: int = -1

    # ---- event sink --------------------------------------------------------

    def on_status_event(self, ev: StatusEvent) -> None:
        if ev.type != "lap":
            return
        lap_no = int(ev.payload.get("lap", 0))
        last_ms = int(ev.payload.get("last_lap_ms", 0))
        if last_ms <= 0:
            return  # GT7 emits -1 for "no lap recorded yet"
        row = self.rowCount()
        self.insertRow(row)

        lap_item = QTableWidgetItem(str(lap_no))
        lap_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        time_item = QTableWidgetItem(_fmt_laptime(last_ms))
        time_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        # New best?
        is_new_best = self._best_lap_ms < 0 or last_ms < self._best_lap_ms
        if is_new_best:
            self._clear_best_highlight()
            self._best_lap_ms = last_ms
            self._best_row = row
            delta_text = "—"
        else:
            delta_text = f"+{_fmt_laptime(last_ms - self._best_lap_ms)}"

        delta_item = QTableWidgetItem(delta_text)
        delta_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        self.setItem(row, 0, lap_item)
        self.setItem(row, 1, time_item)
        self.setItem(row, 2, delta_item)
        # Items must be in the table before we can find them via .item(row, col),
        # so the highlight pass runs after setItem().
        if is_new_best:
            self._apply_best_highlight(row)
        self.scrollToBottom()

    def clear_all(self) -> None:
        """Wipe the table. NOT named ``reset()`` — see the comment on
        AdviceHistory.clear_all. Overriding the Qt virtual ``reset()``
        slot triggers infinite recursion on Windows in some scenarios."""
        self.setRowCount(0)
        self._best_row = -1
        self._best_lap_ms = -1

    # ---- internals ---------------------------------------------------------

    def _apply_best_highlight(self, row: int) -> None:
        for col in range(self.columnCount()):
            item = self.item(row, col)
            if item is None:
                continue
            f = QFont(item.font())
            f.setBold(True)
            item.setFont(f)
            item.setForeground(QBrush(self._BEST_COLOR))

    def _clear_best_highlight(self) -> None:
        if self._best_row < 0 or self._best_row >= self.rowCount():
            return
        for col in range(self.columnCount()):
            item = self.item(self._best_row, col)
            if item is None:
                continue
            f = QFont(item.font())
            f.setBold(False)
            item.setFont(f)
            item.setForeground(QBrush())  # default
