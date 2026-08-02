"""Render the GUI offscreen with representative data and save a PNG.

Dev tool: lets you eyeball styling changes without a display or a PS5.

    QT_QPA_PLATFORM=offscreen python scripts/gui_screenshot.py out.png
"""

from __future__ import annotations

import sys
import time

from PySide6.QtWidgets import QApplication

from gt7coach.gui.app import MainWindow
from gt7coach.gui.log_tail import StatusEvent
from gt7coach.gui.theme import apply_theme


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "gui_screenshot.png"
    app = QApplication(sys.argv[:1])
    apply_theme(app)
    w = MainWindow()
    w.resize(1160, 700)
    w.show()
    app.processEvents()

    now = time.time()
    events = [
        StatusEvent(now, "rx_stats", {"hz": 59.8, "silent_for_s": 0.0}),
        StatusEvent(now, "track", {"id": "DeepForestRaceway", "name": "Deep Forest Raceway"}),
        StatusEvent(now, "corner", {"event_count": 2}),
        StatusEvent(now, "corner", {"event_count": 1}),
        StatusEvent(now, "corner", {"event_count": 3}),
        StatusEvent(
            now,
            "advice",
            {
                "advice": "Brake earlier into the esses; you're paying for it twice.",
                "event_type": "braking.late_brake",
                "event_severity": 0.8,
            },
        ),
        StatusEvent(
            now,
            "advice",
            {
                "advice": "Carry throttle through the long right.",
                "event_type": "throttle.early_lift",
                "event_severity": 0.6,
            },
        ),
        StatusEvent(now, "lap", {"lap": 3, "last_lap_ms": 92411, "best_lap_ms": 91876}),
    ]
    for ev in events:
        w._on_status_event(ev)
    for line in [
        "13:41:02 INFO gt7coach.coach: provider: gemini (config) model=<default>",
        "13:41:03 INFO gt7coach.tracks.detector: track detected: Deep Forest Raceway",
        "13:41:03 INFO gt7coach.coach.cue_timing: cue timing armed: Deep Forest (11 turns)",
        "13:41:09 INFO gt7coach.coach: corner #1: 3.47s entry=224->min=70->exit=70 km/h",
        "13:41:12 INFO gt7coach.coach: coach -> 'Brake earlier into the esses.'",
        "13:41:14 WARNING gt7coach.coach.advisor: provider slow (6.2s), advice may lag",
        "13:41:15 INFO gt7coach.coach: corner #2: 4.61s entry=70->min=70->exit=152 km/h",
    ]:
        w._live_log.append_line(line)
    app.processEvents()
    w.grab().save(out)
    print(f"saved {out}")
    # Skip app.exec(); tear down explicitly so QThreads in UpdateChecker etc.
    # don't abort the interpreter on exit.
    w.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
