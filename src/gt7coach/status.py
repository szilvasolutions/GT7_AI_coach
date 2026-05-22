"""Opt-in status event emitter for GUI integration.

When the env var ``GT7COACH_STATUS_FILE`` is set, the coach pipeline
appends one JSON line per noteworthy event to that file. The GUI
(running as the parent process) tails the file with QFileSystemWatcher
and updates its status widgets in real time. When the env var is
unset, nothing is emitted — the CLI behaves identically.

The schema is intentionally minimal and append-only: the file is one
JSON object per line, each with a ``type`` discriminator. The GUI is
free to ignore unknown types.

Event types:

* ``corner``     — emitted from main.py after each corner_segmenter
                   finalises a trace
* ``advice``     — emitted from advisor._record() when a non-suppressed
                   AdvisorResult is produced (also: incident roasts)
* ``lap``        — emitted from LapTracker when a lap closes
* ``rx_stats``   — emitted by the receiver's stats thread every 5 s
* ``track``      — emitted on first track detection
* ``incident``   — emitted on spin/slide/crash
* ``session_end`` — emitted from session.close()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ENV_VAR = "GT7COACH_STATUS_FILE"
_lock = threading.Lock()
_path: Path | None = None
_enabled: bool | None = None


def enabled() -> bool:
    """True iff the env var is set and the file is writable."""
    global _enabled, _path
    if _enabled is not None:
        return _enabled
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        _enabled = False
        return False
    p = Path(raw)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # Truncate any stale file so the GUI sees a fresh stream.
        p.write_text("", encoding="utf-8")
    except OSError as exc:
        log.warning("status file %s not writable (%s); status emission disabled", p, exc)
        _enabled = False
        return False
    _path = p
    _enabled = True
    log.info("status events -> %s", p)
    return True


def emit(event_type: str, /, **fields: Any) -> None:
    """Append one event line to the status file. No-op if not enabled.

    ``event_type`` is positional-only so payloads are free to carry an
    ``event_type=...`` field (incidents do exactly this) without
    colliding with the discriminator.

    Safe to call from any thread. Failures are logged once at WARNING
    and then swallowed so a flaky filesystem can't crash the pipeline.
    """
    if not enabled():
        return
    record = {"ts": time.time(), "type": event_type, **fields}
    try:
        line = json.dumps(record, default=str) + "\n"
    except (TypeError, ValueError) as exc:
        log.debug("status emit skipped: %s (record=%r)", exc, record)
        return
    assert _path is not None  # enabled() set it
    with _lock:
        try:
            with _path.open("a", encoding="utf-8") as f:
                f.write(line)
        except OSError as exc:
            log.debug("status emit failed: %s", exc)
