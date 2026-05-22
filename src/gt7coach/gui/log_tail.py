"""File-tailing helpers for the GUI status pipe.

The coach subprocess writes one JSON object per line to the status
file (path set by env var ``GT7COACH_STATUS_FILE``). This module exposes
a Qt object that tails that file and emits parsed events as Qt signals.

We use a polling timer rather than ``QFileSystemWatcher`` because the
latter only fires when the kernel emits a change notification, which
on Windows is unreliable for append-only writes from another process.
A 200 ms poll is plenty for our event volume (a corner event every
~5-10 s, rx_stats every 5 s).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StatusEvent:
    ts: float
    type: str
    payload: dict[str, Any]


class StatusTail(QObject):
    """Polling tail over a JSONL file. Emits one event per parsed line."""

    event = Signal(object)  # StatusEvent

    def __init__(self, parent: QObject | None = None, poll_ms: int = 200) -> None:
        super().__init__(parent)
        self._path: Path | None = None
        self._fh = None
        self._tail_buf = ""
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self._poll)

    def watch(self, path: Path) -> None:
        """Start tailing ``path``. Resets state if already watching."""
        self.stop()
        self._path = path
        self._tail_buf = ""
        # Open lazily on first poll — the subprocess may not have created
        # the file yet.
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        self._path = None
        self._tail_buf = ""

    def _poll(self) -> None:
        if self._path is None:
            return
        try:
            if self._fh is None:
                if not self._path.exists():
                    return
                self._fh = self._path.open("r", encoding="utf-8")
            chunk = self._fh.read()
        except OSError as exc:
            log.debug("status tail read failed: %s", exc)
            return
        if not chunk:
            return
        self._tail_buf += chunk
        while "\n" in self._tail_buf:
            line, self._tail_buf = self._tail_buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                log.debug("dropping malformed status line %r: %s", line, exc)
                continue
            ts = float(record.pop("ts", 0.0))
            t = str(record.pop("type", ""))
            if not t:
                continue
            self.event.emit(StatusEvent(ts=ts, type=t, payload=record))
