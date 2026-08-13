"""Stop the machine sleeping while the coach is running.

A session that survives the display going dark is not something the app can
arrange after the fact: once Windows suspends, nothing runs, the UDP socket
stops being serviced and the coach goes quiet mid-race. Two logged sessions
died exactly that way.

The fix is to tell Windows the machine is busy for as long as a session is
live, which is the same thing a video player does. No-ops on other platforms.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


class KeepAwake:
    """Context manager that holds off sleep for its lifetime."""

    def __init__(self, *, keep_display_on: bool = False) -> None:
        self._keep_display_on = keep_display_on
        self._held = False

    def __enter__(self) -> KeepAwake:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> None:
        if sys.platform != "win32" or self._held:
            return
        try:
            import ctypes

            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            if self._keep_display_on:
                flags |= _ES_DISPLAY_REQUIRED
            if ctypes.windll.kernel32.SetThreadExecutionState(flags) == 0:
                log.warning("could not ask Windows to stay awake")
                return
            self._held = True
            log.info(
                "sleep suppressed for this session%s",
                " (display too)" if self._keep_display_on else "",
            )
        except Exception as exc:  # pragma: no cover — platform specific
            log.warning("keep-awake unavailable: %s", exc)

    def release(self) -> None:
        if not self._held:
            return
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            log.info("sleep suppression released")
        except Exception as exc:  # pragma: no cover — platform specific
            log.warning("could not release keep-awake: %s", exc)
        finally:
            self._held = False
