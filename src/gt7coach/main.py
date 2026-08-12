"""``gt7coach-coach`` CLI: runs the full pipeline against live PS5 or a CSV.

Pipeline:

    source (live UDP | CSV replay)
        -> CornerSegmenter
        -> detectors (late_brake, wheelspin, understeer)
        -> Advisor (rate-limited provider call)
        -> VoiceEngine (or stdout-only via --voice null)

Requires API keys to be in environment (typically loaded from a .env in the
working directory). See README and config.example.yaml.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

from gt7coach import status
from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    RateLimiter,
    RateLimiterConfig,
    make_provider,
)
from gt7coach.coach.cue_timing import CueScheduler
from gt7coach.coach.laps import LapTracker
from gt7coach.config import ConfigError, looks_like_gt7_config
from gt7coach.config import load as load_config
from gt7coach.detectors import (
    CornerSegmenter,
    CornerTrace,
    Event,
    IncidentDetector,
    VRAlertDetector,
    detect_clean_corner,
    detect_early_lift,
    detect_late_apex,
    detect_late_brake,
    detect_lockup,
    detect_oversteer,
    detect_sawing,
    detect_trail_off_too_fast,
    detect_understeer,
    detect_wheelspin,
)
from gt7coach.session import SessionLogger, summarise
from gt7coach.telemetry import Packet, Receiver, ReceiverConfig, replay_csv
from gt7coach.tracks import TrackDetector
from gt7coach.voice import make_voice

log = logging.getLogger("gt7coach.coach")


def _load_env(path: Path | None) -> None:
    """Load .env into os.environ if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if path is None:
        load_dotenv()
    else:
        load_dotenv(path)


_PROVIDER_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def _resolve_api_key(provider: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    env_var = _PROVIDER_ENV.get(provider)
    return os.environ.get(env_var) if env_var else None


def _providers_with_keys() -> list[str]:
    """Names of providers whose API key is set in the environment, in preference order."""
    return [name for name, env in _PROVIDER_ENV.items() if os.environ.get(env)]


def _select_provider(
    cli_choice: str | None,
    config_choice: str,
    cli_api_key: str | None,
) -> tuple[str, str]:
    """Pick a provider name. Returns (chosen, reason) for logging.

    Precedence:
        1. ``--provider X`` on the CLI -> use X verbatim.
        2. ``config.yaml`` -> use it if its env key is set, or if it doesn't
           need one (``ollama``).
        3. Otherwise pick the first provider that has an API key.
        4. Last resort: keep the config's choice so the error message is clear.
    """
    if cli_choice:
        return cli_choice, "cli"

    available = _providers_with_keys()
    no_key_needed = {"ollama", "mock"}

    if config_choice in available or config_choice in no_key_needed or cli_api_key:
        return config_choice, "config"

    if available:
        return available[0], f"fallback from {config_choice!r} (no API key)"

    return config_choice, "config (no API keys found — will error)"


def _stream_live(cfg: ReceiverConfig) -> tuple[Iterator[Packet], Receiver | None]:
    rx = Receiver(cfg)
    rx.start()
    return rx.packets(), rx


def _stream_replay(args: argparse.Namespace) -> tuple[Iterator[Packet], None]:
    """Open the replay CSV, expanding ``*`` / ``?`` patterns the shell didn't."""
    raw = args.source
    if any(ch in raw for ch in "*?["):
        # PowerShell (and cmd.exe) don't glob arguments the way bash does.
        # Expand the pattern ourselves so '--source sessions\capture_*.csv' works
        # the same on every shell.
        import glob as _glob

        matches = sorted(_glob.glob(raw))
        if not matches:
            raise FileNotFoundError(f"no files match pattern: {raw}")
        path = Path(matches[-1])  # most recent (filenames embed a timestamp)
        if len(matches) > 1:
            log.info("pattern %r matched %d files; using most recent: %s", raw, len(matches), path)
    else:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"replay source not found: {path}")
    return replay_csv(path, realtime=args.realtime), None


# Per-corner detectors keyed by the names used in config.yaml's
# ``detectors.enabled`` list and ``detector_configs``. Order matters: the
# positive-feedback clean-corner detector runs after all of these.
_DETECTORS: list[tuple[str, object]] = [
    ("braking.late_brake", detect_late_brake),
    ("braking.lockup", detect_lockup),
    ("braking.trail_off_too_fast", detect_trail_off_too_fast),
    ("throttle.wheelspin", detect_wheelspin),
    ("throttle.sawing", detect_sawing),
    ("throttle.early_lift", detect_early_lift),
    ("steering.understeer", detect_understeer),
    ("steering.oversteer", detect_oversteer),
    ("line.late_apex", detect_late_apex),
]


def _off_track_or_paused(packet: Packet) -> bool:
    """True for frames captured in menus / replays / while paused.

    GT7's flags word: bit 0 = car on track, bit 1 = paused. ``flags == 0``
    means unknown (synthetic packets, captures from before the flags column
    existed) — treat those as live so replays keep working.
    """
    return packet.flags != 0 and (not packet.flags & 0x01 or bool(packet.flags & 0x02))


def _run_detectors(
    trace: CornerTrace,
    enabled: set[str] | None = None,
    configs: dict[str, object] | None = None,
) -> list[Event]:
    events: list[Event] = []
    for name, func in _DETECTORS:
        if enabled is not None and name not in enabled:
            continue
        cfg = (configs or {}).get(name)
        events += func(trace, config=cfg)  # type: ignore[operator]
    # Positive-feedback detector runs LAST and only fires when nothing else
    # did — it explicitly looks at the other events.
    events += detect_clean_corner(trace, other_events=events)
    return events


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gt7coach-coach",
        description="Run the full GT7 AI coaching pipeline.",
    )
    p.add_argument(
        "--source",
        default="live",
        help='"live" (default) or path to a capture CSV',
    )
    p.add_argument(
        "--realtime",
        action="store_true",
        help="When replaying a CSV, recreate inter-arrival timing (default: as fast as possible)",
    )
    # Live source flags (mirror gt7coach-capture).
    p.add_argument("--ip", default="auto", help='PS5 IP, or "auto" to discover')
    p.add_argument("--format", default="B", choices=["A", "B", "~", "C"])
    p.add_argument("--port-rx", type=int, default=33740)
    p.add_argument("--port-tx", type=int, default=33739)

    # Coach flags.
    p.add_argument(
        "--provider",
        default=None,
        choices=["anthropic", "openai", "gemini", "ollama", "mock"],
        help=(
            "LLM provider for coaching advice. If omitted, picks whichever "
            "provider has an API key set in env (preferred order: anthropic, "
            "openai, gemini), or whatever config.yaml says."
        ),
    )
    p.add_argument("--model", default=None, help="Override provider's default model")
    p.add_argument("--api-key", default=None, help="Override env-var API key")
    p.add_argument(
        "--driver-style",
        default="smooth",
        choices=["smooth", "aggressive", "learning"],
    )
    p.add_argument(
        "--cooldown",
        type=float,
        default=4.0,
        help="Global rate-limit between advices, seconds (default: 4)",
    )
    p.add_argument(
        "--car-class",
        default=None,
        help='Free-form car descriptor (e.g. "Gr.3 RWD") fed into every prompt',
    )
    p.add_argument(
        "--track",
        default=None,
        help='Force a known track id (skips auto-detection). e.g. "deep_forest"',
    )

    # Voice flags.
    p.add_argument(
        "--voice",
        default="pyttsx3",
        help='Voice engine: "pyttsx3" or "null" (no audio, log only)',
    )
    p.add_argument(
        "--voice-rate",
        type=int,
        default=200,
        help="TTS rate in words/minute (default 200). Default lowered from 230 "
        "after operator feedback that 230 felt rushed in cockpit audio.",
    )

    # Session logging.
    p.add_argument(
        "--log-dir",
        type=Path,
        default=Path("./sessions"),
        help="Where to write per-run session logs (default: ./sessions)",
    )
    p.add_argument(
        "--no-log",
        action="store_true",
        help="Disable session logging entirely",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="At end of session, call the LLM once for a 3-5 sentence debrief",
    )
    p.add_argument(
        "--no-summary",
        action="store_true",
        help="Disable end-of-session summary even if config.yaml enables it",
    )

    # Config.
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ./config.yaml if it exists)",
    )

    # Misc.
    p.add_argument("--env-file", type=Path, default=None, help="Path to .env (default: ./.env)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


_NOISY_LOGGER_PREFIXES = ("comtypes", "httpx", "httpcore", "google_genai", "urllib3", "asyncio")


class _NoiseFilter(logging.Filter):
    """Drop log records from noisy third-party libraries (their DEBUG/INFO
    chatter is overwhelming and not useful to the user)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_NOISY_LOGGER_PREFIXES)


def _attach_session_debug_log(session_dir: Path) -> Path:
    """Attach a DEBUG-level FileHandler that writes every gt7coach.* log to
    ``<session_dir>/debug.log``. The third-party noise filter applies here
    too. The root logger is raised to DEBUG so DEBUG records can reach this
    handler; the stderr handler must therefore set its own level explicitly
    or it'll start showing DEBUG chatter on the console.
    """
    debug_path = session_dir / "debug.log"
    fh = logging.FileHandler(debug_path, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    fh.addFilter(_NoiseFilter())
    root = logging.getLogger()
    if root.level > logging.DEBUG:
        root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    return debug_path


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Configure root + a single stderr handler we own. Don't use basicConfig
    # because we need to pin the stderr handler's level explicitly (the file
    # handler attached later raises root to DEBUG, which would otherwise let
    # third-party DEBUG chatter through to the console).
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    stderr_handler.addFilter(_NoiseFilter())
    root.addHandler(stderr_handler)
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    _load_env(args.env_file)

    cfg_path = args.config
    if cfg_path is None and Path("config.yaml").is_file():
        # Only adopt a config we didn't ask for if it's actually ours.
        # "config.yaml" is a common filename — Home Assistant, Docker
        # Compose and friends all use it — and the frozen .exe runs with
        # whatever directory Explorer hands it as the cwd.
        if looks_like_gt7_config("config.yaml"):
            cfg_path = Path("config.yaml")
        else:
            log.warning(
                "ignoring %s — it doesn't look like a gt7coach config; "
                "using defaults (pass --config to point somewhere else)",
                Path("config.yaml").resolve(),
            )
    try:
        cfg = load_config(cfg_path)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    provider_name, provider_reason = _select_provider(
        args.provider, cfg.coach_provider, args.api_key
    )
    # The model name in config.yaml is provider-specific (e.g. "claude-haiku-4-5"
    # only makes sense for Anthropic). If we fell back to a different provider,
    # ignore the config's model and let the chosen provider use its own default.
    if args.model:
        model: str | None = args.model
    elif provider_name == cfg.coach_provider:
        model = cfg.coach_model
    else:
        model = None
        if cfg.coach_model:
            log.warning(
                "ignoring config.coach.model=%r because provider switched from %r to %r",
                cfg.coach_model,
                cfg.coach_provider,
                provider_name,
            )
    api_key = _resolve_api_key(provider_name, args.api_key)
    log.info("provider: %s (%s) model=%s", provider_name, provider_reason, model or "<default>")
    try:
        provider = make_provider(provider_name, api_key=api_key, model=model)
    except Exception as exc:
        log.error("provider setup failed: %s", exc)
        hint = _providers_with_keys()
        if hint:
            log.error("hint: detected keys for %s; try --provider %s", hint, hint[0])
        else:
            log.error(
                "hint: no API keys found in env. Set GEMINI_API_KEY / "
                "ANTHROPIC_API_KEY / OPENAI_API_KEY in .env, "
                "or use --provider ollama / mock"
            )
        return 2

    voice_name = args.voice if args.voice != "pyttsx3" else cfg.voice.engine
    if voice_name == "system":  # documented alias: pyttsx3 IS the system TTS
        voice_name = "pyttsx3"
    voice_kwargs: dict[str, object] = {}
    if voice_name == "pyttsx3":
        # CLI default is 200; an explicit --voice-rate wins over YAML.
        voice_kwargs["rate"] = args.voice_rate if args.voice_rate != 200 else cfg.voice.speed
    elif voice_name == "piper":
        voice_kwargs["voice"] = cfg.voice.piper_voice
        if cfg.voice.piper_model_path:
            voice_kwargs["model_path"] = cfg.voice.piper_model_path
    try:
        voice = make_voice(voice_name, **voice_kwargs)
    except Exception as exc:
        log.error("voice setup failed: %s", exc)
        return 2

    rate_limiter_cfg = RateLimiterConfig(
        global_cooldown_s=args.cooldown
        if args.cooldown != 4.0
        else cfg.rate_limiter.global_cooldown_s,
        duplicate_window_s=cfg.rate_limiter.duplicate_window_s,
    )
    car_class = args.car_class if args.car_class is not None else cfg.coach_car_class
    advisor_cfg = AdvisorConfig(
        driver_style=args.driver_style
        if args.driver_style != "smooth"
        else cfg.advisor.driver_style,
        car_class=car_class,
    )
    if args.source == "live":
        # Precedence: defaults < config.yaml < explicit CLI flag. The YAML
        # network section is the base; a non-default CLI value overrides it
        # (this is what makes the setup wizard's saved ps5_ip actually work).
        net = cfg.network
        if args.ip != "auto":
            net.ps5_ip = args.ip
        if args.port_rx != 33740:
            net.port_rx = args.port_rx
        if args.port_tx != 33739:
            net.port_tx = args.port_tx
        if args.format != "B":
            net.packet_format = args.format
        stream, rx = _stream_live(net)
    else:
        stream, rx = _stream_replay(args)

    session: SessionLogger | None = None
    if not args.no_log:
        log_dir = args.log_dir if args.log_dir != Path("./sessions") else Path(cfg.session.log_dir)
        session = SessionLogger(log_dir, cli_args=vars(args).copy())
        for k, v in list(session._cli_args.items()):
            if isinstance(v, Path):
                session._cli_args[k] = str(v)
        debug_log_path = _attach_session_debug_log(session.dir)
        log.info(
            "debug log: %s (every detail; share this file when reporting issues)", debug_log_path
        )

    # SessionLogger registers as the Advisor's on_result callback so the
    # async worker's final AdvisorResult / IncidentResult is what hits
    # coach.jsonl — not the synchronous "queued" stub.
    # Duration-aware cue timing (Phase F): the advisor's worker delays
    # voice.speak() so an utterance finishes before the next braking zone.
    # Armed once a track is detected/forced; inert (always clear) until then.
    cue_scheduler = CueScheduler(cfg.cue_timing) if cfg.cue_timing.enabled else None

    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(rate_limiter_cfg),
        config=advisor_cfg,
        on_result=session.on_advisor_result if session is not None else None,
        cue_scheduler=cue_scheduler,
    )

    # Audible startup confirmation.
    if voice_name != "null":
        try:
            voice.speak("Coach ready.")
        except Exception:  # pragma: no cover — non-fatal
            pass

    # Shutdown coordination so Ctrl+C is responsive even when the post-session
    # summary call is blocking on a slow LLM. First Ctrl+C: cleanly stop the
    # receiver and let the finally block drain the voice. Second Ctrl+C:
    # immediately exit the process — don't wait for the summary HTTP call.
    shutdown_state = {"count": 0}

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        shutdown_state["count"] += 1
        log.info("shutdown signal — draining (#%d)", shutdown_state["count"])
        if shutdown_state["count"] >= 2:
            log.warning("second Ctrl+C — exiting now without post-session summary")
            os._exit(130)
        if rx is not None:
            rx.stop()
        # Do NOT stop the voice here. The finally block runs
        # advisor.flush() which waits for the worker's in-flight LLM
        # call to finish; the worker then voice.speak()s its result.
        # Stopping voice now would kill the TTS thread and drop that
        # last advice line on the floor.

    signal.signal(signal.SIGINT, _shutdown)

    seg = CornerSegmenter(cfg.corner)
    unknown_detectors = cfg.detectors_enabled - {"corner.segment", *(n for n, _ in _DETECTORS)}
    if unknown_detectors:
        log.warning("config detectors.enabled has unknown names: %s", sorted(unknown_detectors))
    incident_detector = IncidentDetector()
    track_detector = TrackDetector()
    vr_alert_detector = VRAlertDetector(cfg=cfg.vr_alerts)
    lap_tracker = LapTracker(
        provider=provider,
        voice=voice,
        driver_style=advisor_cfg.driver_style,
        announce_mode=cfg.session.lap_announce_mode,
    )
    # CLI / config track override happens once at startup.
    forced_track = args.track or cfg.coach_track
    if forced_track:
        try:
            track = track_detector.force(forced_track)
            advisor.set_track_shape(track.shape_description)
            if cue_scheduler is not None:
                cue_scheduler.set_track(track)
        except KeyError as exc:
            log.warning("--track override failed: %s", exc)

    corner_idx = 0
    try:
        for packet in stream:
            if session is not None:
                session.log_packet(packet)
            # GT7 keeps streaming packets in menus, replays and while paused —
            # skip those frames so detectors and lap timing don't chew on garbage.
            if _off_track_or_paused(packet):
                continue
            # Track detector wants every packet (it maintains a position
            # buffer for sequence matching).
            if track_detector.track is None:
                tr = track_detector.feed(packet)
                if tr is not None:
                    advisor.set_track_shape(tr.shape_description)
                    if cue_scheduler is not None:
                        cue_scheduler.set_track(tr)
                    status.emit("track", id=tr.id, name=tr.display_name)
            else:
                track_detector.feed(packet)  # keeps the sticky-release timer fresh
            if cue_scheduler is not None:
                cue_scheduler.feed(packet)
            lap_tracker.feed_packet(packet)
            # VR voice-HUD alerts (tire temp, fuel, coolant, shift, self-delta).
            # Per-packet, deterministic phrases, no LLM call.
            for vr_event in vr_alert_detector.feed(packet):
                phrase = advisor.on_vr_alert(vr_event)
                if phrase is not None:
                    log.info("vr alert %s: %s", vr_event.type, phrase)
                    status.emit(
                        "vr_alert",
                        alert_type=vr_event.type,
                        severity=vr_event.severity,
                        message=phrase,
                    )
            # Incidents fire on a single packet; check before corner detection
            # so a spin during a corner trace interrupts the planned advice.
            incident = incident_detector.feed(packet)
            if incident is not None:
                log.info(
                    "incident: %s sev=%.2f speed=%.1f km/h",
                    incident.type,
                    incident.severity,
                    incident.evidence.get("speed_kmh", 0.0),
                )
                status.emit(
                    "incident",
                    incident_type=incident.type,
                    severity=incident.severity,
                    speed_kmh=incident.evidence.get("speed_kmh", 0.0),
                )
                advisor.on_incident(incident)
            trace = seg.feed(packet)
            if trace is None:
                continue
            corner_idx += 1
            events = _run_detectors(trace, cfg.detectors_enabled, cfg.detector_configs)
            log.info(
                "corner #%d: %.2fs entry=%.0f->min=%.0f->exit=%.0f km/h peak=%.2fg events=%d",
                corner_idx,
                trace.duration_s,
                trace.entry_speed_kmh,
                trace.min_speed_kmh,
                trace.exit_speed_kmh,
                trace.peak_lat_g,
                len(events),
            )
            status.emit(
                "corner",
                corner_idx=corner_idx,
                duration_s=trace.duration_s,
                entry_speed_kmh=trace.entry_speed_kmh,
                min_speed_kmh=trace.min_speed_kmh,
                exit_speed_kmh=trace.exit_speed_kmh,
                peak_lat_g=trace.peak_lat_g,
                event_count=len(events),
            )
            if session is not None:
                session.log_corner(corner_idx, trace, events)
            lap_tracker.feed_events(events)
            advisor.on_corner(trace, events, corner_idx=corner_idx)
        trailing = seg.flush()
        if trailing is not None:
            corner_idx += 1
            trailing_events = _run_detectors(trailing, cfg.detectors_enabled, cfg.detector_configs)
            if session is not None:
                session.log_corner(corner_idx, trailing, trailing_events)
            advisor.on_corner(trailing, trailing_events, corner_idx=corner_idx)
    finally:
        if rx is not None:
            rx.stop()
        # Let the async coach worker drain any in-flight LLM call before we
        # tear the voice down, so the last corner's advice still gets spoken.
        advisor.flush()
        advisor.stop()
        # advisor.flush() returns once the worker has voice.speak()'d its
        # result, but the TTS engine still needs time to actually play it.
        # Poll voice.is_idle() with a bounded timeout before tearing down.
        deadline = time.monotonic() + 5.0
        while not voice.is_idle() and time.monotonic() < deadline:
            time.sleep(0.05)
        voice.stop()
        if session is not None:
            session.close()

    want_summary = args.summary or (cfg.session.generate_summary and not args.no_summary)
    # Generate the summary on a graceful stop (one Ctrl+C / one Stop click)
    # but skip it on a force-quit (two Ctrl+C / two Stop clicks). The second
    # SIGINT path hits os._exit(130) and never gets here anyway; this is a
    # safety net in case the count was bumped elsewhere.
    if shutdown_state["count"] >= 2:
        log.info("skipping post-session summary (force-quit requested)")
    elif want_summary and session is not None:
        try:
            summary = summarise(
                session.dir, provider=provider, driver_style=advisor_cfg.driver_style
            )
            print("\n=== Session summary ===")
            print(summary)
            print(f"\n(saved to {session.dir / 'summary.txt'})")
        except Exception as exc:
            log.warning("session summary failed: %s", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
