"""Compatibility shim — LiveLog moved into console_panel with its header.

Kept so ``from gt7coach.gui.widgets.live_log import LiveLog`` keeps
working for anything outside this repo.
"""

from __future__ import annotations

from gt7coach.gui.widgets.console_panel import LiveLog

__all__ = ["LiveLog"]
