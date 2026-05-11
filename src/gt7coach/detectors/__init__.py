"""Detectors: pure physics functions that turn telemetry traces into Events.

Each detector takes a :class:`CornerTrace` and returns zero or more
:class:`Event` objects. **Detectors never call the LLM.** The LLM-facing
layer in :mod:`gt7coach.coach` consumes events; here we only care about the
physics.
"""

from gt7coach.detectors.base import Event
from gt7coach.detectors.braking import (
    LateBrakeConfig,
    LockupConfig,
    TrailOffConfig,
    detect_late_brake,
    detect_lockup,
    detect_trail_off_too_fast,
)
from gt7coach.detectors.corner import CornerSegmenter, CornerSegmenterConfig, CornerTrace
from gt7coach.detectors.incident import Incident, IncidentDetector, IncidentDetectorConfig
from gt7coach.detectors.line import LateApexConfig, detect_late_apex
from gt7coach.detectors.steering import (
    OversteerConfig,
    UndersteerConfig,
    detect_oversteer,
    detect_understeer,
)
from gt7coach.detectors.throttle import (
    EarlyLiftConfig,
    SawingConfig,
    WheelspinConfig,
    detect_early_lift,
    detect_sawing,
    detect_wheelspin,
)

__all__ = [
    "CornerSegmenter",
    "CornerSegmenterConfig",
    "CornerTrace",
    "EarlyLiftConfig",
    "Event",
    "Incident",
    "IncidentDetector",
    "IncidentDetectorConfig",
    "LateApexConfig",
    "LateBrakeConfig",
    "LockupConfig",
    "OversteerConfig",
    "SawingConfig",
    "TrailOffConfig",
    "UndersteerConfig",
    "WheelspinConfig",
    "detect_early_lift",
    "detect_late_apex",
    "detect_late_brake",
    "detect_lockup",
    "detect_oversteer",
    "detect_sawing",
    "detect_trail_off_too_fast",
    "detect_understeer",
    "detect_wheelspin",
]
