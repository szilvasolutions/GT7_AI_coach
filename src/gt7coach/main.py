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
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

from gt7coach.coach import (
    Advisor,
    AdvisorConfig,
    RateLimiter,
    RateLimiterConfig,
    make_provider,
)
from gt7coach.coach.laps import LapTracker
from gt7coach.config import load as load_config
from gt7coach.detectors import (
    CornerSegmenter,
    CornerTrace,
    Event,
    IncidentDetector,
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


def _stream_live(args: argparse.Namespace) -> tuple[Iterator[Packet], Receiver | None]:
    cfg = ReceiverConfig(
        ps5_ip=args.ip,
        port_rx=args.port_rx,
        port_tx=args.port_tx,
        packet_format=args.format,
    )
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


def _run_detectors(trace: CornerTrace) -> list[Event]:
    events: list[Event] = []
    events += detect_late_brake(trace)
    events += detect_lockup(trace)
    events += detect_trail_off_too_fast(trace)
    events += detect_wheelspin(trace)
    events += detect_sawing(trace)
    events += detect_early_lift(trace)
    events += detect_understeer(trace)
    events += detect_oversteer(trace)
    events += detect_late_apex(trace)
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
    p.add_argument("--voice-rate", type=int, default=230)

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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    _load_env(args.env_file)

    cfg_path = args.config
    if cfg_path is None and Path("config.yaml").is_file():
        cfg_path = Path("config.yaml")
    cfg = load_config(cfg_path)

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
    voice_kwargs: dict[str, object] = {}
    if voice_name == "pyttsx3":
        voice_kwargs["rate"] = args.voice_rate
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
        stream, rx = _stream_live(args)
    else:
        stream, rx = _stream_replay(args)

    session: SessionLogger | None = None
    if not args.no_log:
        session = SessionLogger(args.log_dir, cli_args=vars(args).copy())
        for k, v in list(session._cli_args.items()):
            if isinstance(v, Path):
                session._cli_args[k] = str(v)

    # SessionLogger registers as the Advisor's on_result callback so the
    # async worker's final AdvisorResult / IncidentResult is what hits
    # coach.jsonl — not the synchronous "queued" stub.
    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(rate_limiter_cfg),
        config=advisor_cfg,
        on_result=session.on_advisor_result if session is not None else None,
    )

    # Audible startup confirmation.
    if voice_name != "null":
        try:
            voice.speak("Coach ready.")
        except Exception:  # pragma: no cover — non-fatal
            pass

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown signal — draining")
        if rx is not None:
            rx.stop()
        voice.stop()

    signal.signal(signal.SIGINT, _shutdown)

    seg = CornerSegmenter()
    incident_detector = IncidentDetector()
    track_detector = TrackDetector()
    lap_tracker = LapTracker(
        provider=provider,
        voice=voice,
        driver_style=advisor_cfg.driver_style,
    )
    # CLI / config track override happens once at startup.
    forced_track = args.track or cfg.coach_track
    if forced_track:
        try:
            track = track_detector.force(forced_track)
            advisor.set_track_shape(track.shape_description)
        except KeyError as exc:
            log.warning("--track override failed: %s", exc)

    corner_idx = 0
    try:
        for packet in stream:
            if session is not None:
                session.log_packet(packet)
            # Track detector wants every packet (it maintains a position
            # buffer for sequence matching).
            if track_detector.track is None:
                tr = track_detector.feed(packet)
                if tr is not None:
                    advisor.set_track_shape(tr.shape_description)
            else:
                track_detector.feed(packet)  # keeps the sticky-release timer fresh
            lap_tracker.feed_packet(packet)
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
                advisor.on_incident(incident)
            trace = seg.feed(packet)
            if trace is None:
                continue
            corner_idx += 1
            events = _run_detectors(trace)
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
            if session is not None:
                session.log_corner(corner_idx, trace, events)
            lap_tracker.feed_events(events)
            advisor.on_corner(trace, events, corner_idx=corner_idx)
        trailing = seg.flush()
        if trailing is not None:
            corner_idx += 1
            trailing_events = _run_detectors(trailing)
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
        voice.stop()
        if session is not None:
            session.close()

    want_summary = args.summary or (cfg.session.generate_summary and not args.no_summary)
    if want_summary and session is not None:
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
