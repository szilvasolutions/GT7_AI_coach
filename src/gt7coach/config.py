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
from gt7coach.coach.cue_timing import CueTimingConfig
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
    engine: str = "pyttsx3"  # pyttsx3 | piper | system | null
    speed: int = 200  # pyttsx3 rate (230 felt rushed in cockpit audio)
    piper_voice: str = "en_US-amy-medium"
    piper_model_path: str | None = None


@dataclass(slots=True)
class SessionSettings:
    log_dir: str = "./sessions"
    rotation_mb: int = 10  # reserved — log rotation is not implemented yet
    generate_summary: bool = True
    # End-of-lap voice mode. "recommendation" = current behaviour (LLM
    # summary), "best_lap" = short PB callout only (no LLM call, free-
    # tier-friendly), "both" = PB callout then LLM summary.
    # Best-lap callout is off by default: with "both" every lap produced two
    # separate utterances, which reads as a bug. Switch it on in the Config
    # dialog under "End-of-lap announce".
    lap_announce_mode: str = "recommendation"


@dataclass(slots=True)
class VRAlertsConfig:
    """Per-alert toggles + thresholds for the VR voice-HUD layer.

    All alerts go through the existing rate limiter, so the global
    cooldown still applies on top of these settings.
    """

    tyre_temp_enabled: bool = True
    tyre_temp_hot_c: float = 110.0
    tyre_temp_cold_c: float = 60.0

    fuel_enabled: bool = True
    fuel_low_laps_remaining: float = 3.0
    fuel_critical_laps_remaining: float = 1.5

    coolant_enabled: bool = True
    oil_hot_c: float = 130.0
    water_hot_c: float = 110.0

    shift_assist_enabled: bool = False  # chatty — opt-in only

    self_delta_enabled: bool = True
    self_delta_threshold_ms: int = 300  # only speak if |delta| > 0.3s


@dataclass(slots=True)
class LoadedConfig:
    """Everything the CLI needs after merging YAML + defaults."""

    network: ReceiverConfig
    corner: CornerSegmenterConfig
    advisor: AdvisorConfig
    rate_limiter: RateLimiterConfig
    voice: VoiceSettings
    session: SessionSettings
    vr_alerts: VRAlertsConfig
    cue_timing: CueTimingConfig
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
    "braking.no_trail",
}
# VR voice-HUD alerts are toggled via cfg.vr_alerts.*_enabled, not via
# detectors_enabled — the VRAlertDetector handles all sub-alerts internally.


def default_config() -> LoadedConfig:
    return LoadedConfig(
        network=ReceiverConfig(),
        corner=CornerSegmenterConfig(),
        advisor=AdvisorConfig(),
        rate_limiter=RateLimiterConfig(),
        voice=VoiceSettings(),
        session=SessionSettings(),
        vr_alerts=VRAlertsConfig(),
        cue_timing=CueTimingConfig(),
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


def save(cfg: LoadedConfig, path: str | Path) -> None:
    """Write the relevant subset of ``cfg`` back to ``path`` as YAML.

    Symmetrical to :func:`load`: the file produced is one ``load(...)``
    call can re-read into an equivalent ``LoadedConfig``. We only emit
    the fields a user is likely to want to edit — the dense detector
    threshold structs stay defaults unless explicitly set.

    PyYAML is the only dep; same as :func:`load`.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to save config.yaml. pip install PyYAML") from exc

    data: dict[str, Any] = {
        "network": {
            "ps5_ip": cfg.network.ps5_ip or "auto",
            "port_rx": int(cfg.network.port_rx),
            "port_tx": int(cfg.network.port_tx),
            "heartbeat_seconds": float(cfg.network.heartbeat_seconds),
            "packet_format": str(cfg.network.packet_format),
        },
        "coach": {
            "provider": str(cfg.coach_provider),
            "driver_style": str(cfg.advisor.driver_style),
            "global_rate_limit_seconds": float(cfg.rate_limiter.global_cooldown_s),
        },
        "voice": {
            "engine": str(cfg.voice.engine),
            "speed": int(cfg.voice.speed),
        },
        "session": {
            "log_dir": str(cfg.session.log_dir),
            "generate_summary": bool(cfg.session.generate_summary),
            "lap_announce_mode": str(cfg.session.lap_announce_mode),
        },
        "detectors": {
            "enabled": sorted(cfg.detectors_enabled),
            "thresholds": {
                "corner_min_speed_kmh": float(cfg.corner.min_speed_kmh),
                "corner_entry_brake": int(cfg.corner.entry_brake),
                "corner_entry_lat_g": float(cfg.corner.entry_lat_g),
                "corner_min_dwell_s": float(cfg.corner.min_dwell_s),
            },
        },
        "cue_timing": {
            "enabled": bool(cfg.cue_timing.enabled),
            "finish_margin_s": float(cfg.cue_timing.finish_margin_s),
            "max_hold_s": float(cfg.cue_timing.max_hold_s),
        },
        "vr_alerts": {
            "tyre_temp_enabled": bool(cfg.vr_alerts.tyre_temp_enabled),
            "tyre_temp_hot_c": float(cfg.vr_alerts.tyre_temp_hot_c),
            "tyre_temp_cold_c": float(cfg.vr_alerts.tyre_temp_cold_c),
            "fuel_enabled": bool(cfg.vr_alerts.fuel_enabled),
            "fuel_low_laps_remaining": float(cfg.vr_alerts.fuel_low_laps_remaining),
            "fuel_critical_laps_remaining": float(cfg.vr_alerts.fuel_critical_laps_remaining),
            "coolant_enabled": bool(cfg.vr_alerts.coolant_enabled),
            "oil_hot_c": float(cfg.vr_alerts.oil_hot_c),
            "water_hot_c": float(cfg.vr_alerts.water_hot_c),
            "shift_assist_enabled": bool(cfg.vr_alerts.shift_assist_enabled),
            "self_delta_enabled": bool(cfg.vr_alerts.self_delta_enabled),
            "self_delta_threshold_ms": int(cfg.vr_alerts.self_delta_threshold_ms),
        },
    }
    if cfg.coach_model:
        data["coach"]["model"] = str(cfg.coach_model)
    if cfg.coach_car_class:
        data["coach"]["car_class"] = str(cfg.coach_car_class)
    if cfg.coach_track:
        data["coach"]["track"] = str(cfg.coach_track)
    if cfg.voice.engine == "piper":
        data["voice"]["piper_voice"] = cfg.voice.piper_voice
        if cfg.voice.piper_model_path:
            data["voice"]["piper_model_path"] = cfg.voice.piper_model_path

    Path(path).write_text(
        "# Written by gt7coach. CLI flags still override anything in this file.\n"
        + yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


class ConfigError(Exception):
    """A config file was named explicitly but can't be used."""


# Top-level keys _merge knows about. Used to tell our config.yaml apart from
# somebody else's — "config.yaml" is a very popular filename, and the coach
# auto-picks one up from the working directory.
KNOWN_SECTIONS = frozenset(
    {"network", "coach", "voice", "session", "detectors", "cue_timing", "vr_alerts"}
)


def _parse_yaml(p: Path) -> dict[str, Any]:
    """Parse ``p`` as a YAML mapping, or raise ConfigError explaining why not."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load config.yaml. pip install PyYAML") from exc

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{p} cannot be read: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # Covers foreign dialects too: Home Assistant's `!include`, Docker
        # Compose anchors that reference undefined aliases, and so on.
        raise ConfigError(f"{p} is not valid YAML that gt7coach can read: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{p} must contain a YAML mapping, not {type(raw).__name__}")
    return raw


def looks_like_gt7_config(path: str | Path) -> bool:
    """True if ``path`` parses and carries at least one section we understand.

    Deliberately total: any failure means "not ours", because the only
    caller is the auto-discovery path, which must never crash on a file it
    merely guessed at.
    """
    try:
        raw = _parse_yaml(Path(path))
    except (ConfigError, RuntimeError):
        return False
    return bool(KNOWN_SECTIONS & raw.keys())


def load(path: str | Path | None = None) -> LoadedConfig:
    """Load ``config.yaml`` if it exists; otherwise return defaults.

    Raises :class:`ConfigError` if ``path`` exists but isn't a usable config
    — callers that guessed the path should check
    :func:`looks_like_gt7_config` first.
    """
    cfg = default_config()
    if path is None:
        return cfg
    p = Path(path)
    if not p.is_file():
        log.warning("config file %s not found; using defaults", p)
        return cfg

    raw = _parse_yaml(p)
    if raw and not (KNOWN_SECTIONS & raw.keys()):
        raise ConfigError(
            f"{p} has none of the sections gt7coach understands "
            f"({', '.join(sorted(KNOWN_SECTIONS))}) — is this another program's "
            "config file? See config.example.yaml."
        )
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
    # `enabled:` left empty in YAML parses as None — keep the defaults then,
    # rather than crashing (or silently disabling every detector).
    if detectors.get("enabled") is not None:
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
    if "lap_announce_mode" in session:
        mode = str(session["lap_announce_mode"])
        if mode in ("recommendation", "best_lap", "both"):
            cfg.session.lap_announce_mode = mode
        else:
            log.warning(
                "invalid lap_announce_mode %r; keeping %r", mode, cfg.session.lap_announce_mode
            )

    cue = raw.get("cue_timing") or {}
    if "enabled" in cue:
        cfg.cue_timing.enabled = bool(cue["enabled"])
    for cue_key in ("finish_margin_s", "max_hold_s", "poll_s", "min_speed_kmh", "max_offline_m"):
        if cue_key in cue:
            setattr(cfg.cue_timing, cue_key, float(cue[cue_key]))

    vr = raw.get("vr_alerts") or {}
    for bool_key in (
        "tyre_temp_enabled",
        "fuel_enabled",
        "coolant_enabled",
        "shift_assist_enabled",
        "self_delta_enabled",
    ):
        if bool_key in vr:
            setattr(cfg.vr_alerts, bool_key, bool(vr[bool_key]))
    for float_key in (
        "tyre_temp_hot_c",
        "tyre_temp_cold_c",
        "fuel_low_laps_remaining",
        "fuel_critical_laps_remaining",
        "oil_hot_c",
        "water_hot_c",
    ):
        if float_key in vr:
            setattr(cfg.vr_alerts, float_key, float(vr[float_key]))
    if "self_delta_threshold_ms" in vr:
        cfg.vr_alerts.self_delta_threshold_ms = int(vr["self_delta_threshold_ms"])

    return cfg
