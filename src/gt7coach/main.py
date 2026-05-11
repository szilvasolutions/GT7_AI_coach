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
from gt7coach.detectors import (
    CornerSegmenter,
    CornerTrace,
    Event,
    detect_late_brake,
    detect_understeer,
    detect_wheelspin,
)
from gt7coach.telemetry import Packet, Receiver, ReceiverConfig, replay_csv
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


def _resolve_api_key(provider: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    return {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
        "gemini": os.environ.get("GEMINI_API_KEY"),
    }.get(provider)


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
    path = Path(args.source)
    if not path.is_file():
        raise FileNotFoundError(f"replay source not found: {path}")
    return replay_csv(path, realtime=args.realtime), None


def _run_detectors(trace: CornerTrace) -> list[Event]:
    events: list[Event] = []
    events += detect_late_brake(trace)
    events += detect_wheelspin(trace)
    events += detect_understeer(trace)
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
        default="anthropic",
        choices=["anthropic", "openai", "gemini", "ollama", "mock"],
        help="LLM provider for coaching advice (default: anthropic)",
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

    # Voice flags.
    p.add_argument(
        "--voice",
        default="pyttsx3",
        help='Voice engine: "pyttsx3" or "null" (no audio, log only)',
    )
    p.add_argument("--voice-rate", type=int, default=230)

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

    api_key = _resolve_api_key(args.provider, args.api_key)
    try:
        provider = make_provider(args.provider, api_key=api_key, model=args.model)
    except Exception as exc:
        log.error("provider setup failed: %s", exc)
        return 2

    try:
        voice = (
            make_voice(args.voice, rate=args.voice_rate)
            if args.voice == "pyttsx3"
            else make_voice(args.voice)
        )
    except Exception as exc:
        log.error("voice setup failed: %s", exc)
        return 2

    advisor = Advisor(
        provider=provider,
        voice=voice,
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=args.cooldown)),
        config=AdvisorConfig(driver_style=args.driver_style),
    )

    if args.source == "live":
        stream, rx = _stream_live(args)
    else:
        stream, rx = _stream_replay(args)

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown signal — draining")
        if rx is not None:
            rx.stop()
        voice.stop()

    signal.signal(signal.SIGINT, _shutdown)

    seg = CornerSegmenter()
    corner_idx = 0
    try:
        for packet in stream:
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
            advisor.on_corner(trace, events)
        trailing = seg.flush()
        if trailing is not None:
            advisor.on_corner(trailing, _run_detectors(trailing))
    finally:
        if rx is not None:
            rx.stop()
        voice.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
