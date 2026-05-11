"""Load ``config.yaml`` and merge it into the per-component dataclasses.

This is the missing wiring between the YAML example shipped in
``config.example.yaml`` and the actual code. The CLI now takes
``--config <path>`` and uses these helpers to build:

* :class:`ReceiverConfig`
* :class:`CornerSegmenterConfig` + per-detector configs
* :class:`RateLimiterConfig`
* :class:`AdvisorConfig`
* Voice settings + session settings (kept as plain dicts since they're
  consumed in one place each)

CLI flags still override anything from the YAML file — the precedence is
``defaults < YAML < CLI flag``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gt7coach.coach.advisor import AdvisorConfig
from gt7coach.coach.rate_limiter import RateLimiterConfig
from gt7coach.detectors import (
    CornerSegmenterConfig,
    EarlyLiftConfig,
    LateApexConfig,
    LateBrakeConfig,
    LockupConfig,
    OversteerConfig,
    SawingConfig,
    TrailOffConfig,
    UndersteerConfig,
    WheelspinConfig,
)
from gt7coach.telemetry.receiver import ReceiverConfig

log = logging.getLogger(__name__)


@dataclass(slots=True)
class VoiceSettings:
    engine: str = "pyttsx3"  # pyttsx3 | piper | null
    speed: int = 230  # pyttsx3 rate
    piper_voice: str = "en_US-amy-medium"
    piper_model_path: str | None = None


@dataclass(slots=True)
class SessionSettings:
    log_dir: str = "./sessions"
    rotation_mb: int = 10  # currently informational
    generate_summary: bool = True


@dataclass(slots=True)
class LoadedConfig:
    """Everything the CLI needs after merging YAML + defaults."""

    network: ReceiverConfig
    corner: CornerSegmenterConfig
    advisor: AdvisorConfig
    rate_limiter: RateLimiterConfig
    voice: VoiceSettings
    session: SessionSettings
    detectors_enabled: set[str]
    detector_configs: dict[str, Any]
    coach_provider: str
    coach_model: str | None
    coach_car_class: str
    coach_track: str | None


_DEFAULT_ENABLED = {
    "corner.segment",
    "braking.late_brake",
    "braking.lockup",
    "throttle.wheelspin",
    "throttle.sawing",
    "throttle.early_lift",
    "steering.understeer",
    "steering.oversteer",
    "line.late_apex",
    "braking.trail_off_too_fast",
}


def default_config() -> LoadedConfig:
    return LoadedConfig(
        network=ReceiverConfig(),
        corner=CornerSegmenterConfig(),
        advisor=AdvisorConfig(),
        rate_limiter=RateLimiterConfig(),
        voice=VoiceSettings(),
        session=SessionSettings(),
        detectors_enabled=set(_DEFAULT_ENABLED),
        detector_configs={
            "braking.late_brake": LateBrakeConfig(),
            "braking.lockup": LockupConfig(),
            "braking.trail_off_too_fast": TrailOffConfig(),
            "throttle.wheelspin": WheelspinConfig(),
            "throttle.sawing": SawingConfig(),
            "throttle.early_lift": EarlyLiftConfig(),
            "steering.understeer": UndersteerConfig(),
            "steering.oversteer": OversteerConfig(),
            "line.late_apex": LateApexConfig(),
        },
        coach_provider="anthropic",
        coach_model=None,
        coach_car_class="",
        coach_track=None,
    )


def load(path: str | Path | None = None) -> LoadedConfig:
    """Load ``config.yaml`` if it exists; otherwise return defaults."""
    cfg = default_config()
    if path is None:
        return cfg
    p = Path(path)
    if not p.is_file():
        log.warning("config file %s not found; using defaults", p)
        return cfg

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load config.yaml. pip install PyYAML") from exc

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return _merge(cfg, raw)


def _merge(cfg: LoadedConfig, raw: dict[str, Any]) -> LoadedConfig:
    network = raw.get("network") or {}
    if "ps5_ip" in network:
        cfg.network.ps5_ip = network["ps5_ip"]
    if "port_rx" in network:
        cfg.network.port_rx = int(network["port_rx"])
    if "port_tx" in network:
        cfg.network.port_tx = int(network["port_tx"])
    if "heartbeat_seconds" in network:
        cfg.network.heartbeat_seconds = float(network["heartbeat_seconds"])
    if "packet_format" in network:
        cfg.network.packet_format = str(network["packet_format"])

    coach = raw.get("coach") or {}
    cfg.coach_provider = str(coach.get("provider", cfg.coach_provider))
    cfg.coach_model = coach.get("model", cfg.coach_model)
    if "driver_style" in coach:
        cfg.advisor.driver_style = str(coach["driver_style"])
    if "global_rate_limit_seconds" in coach:
        cfg.rate_limiter.global_cooldown_s = float(coach["global_rate_limit_seconds"])
    if "car_class" in coach:
        cfg.coach_car_class = str(coach["car_class"])
    if "track" in coach:
        cfg.coach_track = str(coach["track"]) if coach["track"] else None

    voice = raw.get("voice") or {}
    if "engine" in voice:
        cfg.voice.engine = str(voice["engine"])
    if "speed" in voice:
        cfg.voice.speed = int(voice["speed"])
    if "piper_voice" in voice:
        cfg.voice.piper_voice = str(voice["piper_voice"])
    if "piper_model_path" in voice:
        cfg.voice.piper_model_path = str(voice["piper_model_path"])

    detectors = raw.get("detectors") or {}
    if "enabled" in detectors:
        cfg.detectors_enabled = set(detectors["enabled"])
    thresholds = detectors.get("thresholds") or {}
    if "corner_min_speed_kmh" in thresholds:
        cfg.corner.min_speed_kmh = float(thresholds["corner_min_speed_kmh"])
    if "corner_entry_brake" in thresholds:
        cfg.corner.entry_brake = int(thresholds["corner_entry_brake"])
    if "corner_entry_lat_g" in thresholds:
        cfg.corner.entry_lat_g = float(thresholds["corner_entry_lat_g"])
    if "corner_min_dwell_s" in thresholds:
        cfg.corner.min_dwell_s = float(thresholds["corner_min_dwell_s"])

    session = raw.get("session") or {}
    if "log_dir" in session:
        cfg.session.log_dir = str(session["log_dir"])
    if "rotation_mb" in session:
        cfg.session.rotation_mb = int(session["rotation_mb"])
    if "generate_summary" in session:
        cfg.session.generate_summary = bool(session["generate_summary"])

    return cfg
