"""Detectors: pure physics functions that turn telemetry traces into Events.

Each detector takes a :class:`CornerTrace` and returns zero or more
:class:`Event` objects. **Detectors never call the LLM.** The LLM-facing
layer in :mod:`gt7coach.coach` consumes events; here we only care about the
physics.
"""

from gt7coach.detectors.base import Event
from gt7coach.detectors.braking import LateBrakeConfig, detect_late_brake
from gt7coach.detectors.corner import CornerSegmenter, CornerSegmenterConfig, CornerTrace
from gt7coach.detectors.steering import UndersteerConfig, detect_understeer
from gt7coach.detectors.throttle import WheelspinConfig, detect_wheelspin

__all__ = [
    "CornerSegmenter",
    "CornerSegmenterConfig",
    "CornerTrace",
    "Event",
    "LateBrakeConfig",
    "UndersteerConfig",
    "WheelspinConfig",
    "detect_late_brake",
    "detect_understeer",
    "detect_wheelspin",
]
