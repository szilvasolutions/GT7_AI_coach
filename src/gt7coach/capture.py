"""Capture a live GT7 telemetry session to disk for offline analysis.

Run on a machine on the same LAN as the PS5. Produces three artefacts in the
output directory (default ``./sessions/``):

* ``capture_<ts>.bin``   — length-prefixed decrypted packets (raw bytes), so
  unknown offsets can be analysed offline without another live session.
* ``capture_<ts>.csv``   — one row per packet in the same schema
  :func:`gt7coach.telemetry.replay.replay_csv` reads, so the recording can be
  replayed end-to-end through the rest of the pipeline.
* ``capture_<ts>.json``  — sidecar metadata: host info, packet-size histogram,
  format used, first/last sequence id, packet rate.

Usage::

    gt7coach-capture                     # auto-discover PS5, format B, ./sessions/
    gt7coach-capture --ip 192.168.1.120  # explicit PS5
    gt7coach-capture --format A          # 296-byte legacy format
    gt7coach-capture --duration 60       # stop after 60 seconds
    gt7coach-capture --out ./mycaps      # custom output dir

The .bin format is documented in :data:`BIN_HEADER_MAGIC`.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import platform
import signal
import struct
import sys
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from time import monotonic
from types import FrameType

from gt7coach import __version__
from gt7coach.telemetry import (
    FORMAT_PACKET_SIZES,
    Packet,
    Receiver,
    ReceiverConfig,
)

log = logging.getLogger(__name__)

# .bin layout:
#   8 bytes  ASCII magic ("GT7CAP01")
#   1 byte   format byte (e.g. b'B')
#   ... repeated frames:
#       8 bytes  recv_time (f8, monotonic seconds since capture start)
#       2 bytes  packet size (u16, little-endian)
#       N bytes  decrypted packet
BIN_HEADER_MAGIC = b"GT7CAP01"
_FRAME_HEADER = struct.Struct("<dH")


def _csv_field_names() -> list[str]:
    return [f.name for f in fields(Packet)]


def _packet_row(pkt: Packet) -> dict[str, str]:
    """Serialise a Packet to CSV-safe strings (empty string for None)."""
    row: dict[str, str] = {}
    for name, value in asdict(pkt).items():
        row[name] = "" if value is None else repr(value) if isinstance(value, float) else str(value)
    return row


def _format_status(pkt: Packet, pps: float, elapsed: float) -> str:
    steer = f"{pkt.steer_angle:+.2f}" if pkt.steer_angle is not None else "  n/a"
    lat = f"{pkt.accel_lat:+.2f}" if pkt.accel_lat is not None else "  n/a"
    lon = f"{pkt.accel_long:+.2f}" if pkt.accel_long is not None else "  n/a"
    return (
        f"[{elapsed:6.1f}s] {pps:5.1f} pps | "
        f"spd={pkt.speed_kmh:6.1f} km/h | gear={pkt.gear} | rpm={pkt.rpm:6.0f} | "
        f"thr={pkt.throttle:3d} | brk={pkt.brake:3d} | "
        f"steer={steer} | lat={lat}g | lon={lon}g | lap={pkt.lap_count}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gt7coach-capture",
        description="Capture a live GT7 telemetry session to disk for offline analysis.",
    )
    p.add_argument(
        "--ip",
        default="auto",
        help='PS5 IP, or "auto" to broadcast-discover (default: auto)',
    )
    p.add_argument(
        "--format",
        default="B",
        choices=sorted(FORMAT_PACKET_SIZES.keys()),
        help=(
            "Heartbeat byte / packet format. 'B' (316 bytes) gives steering + "
            "body forces; 'A' (296 bytes) is the legacy format. (default: B)"
        ),
    )
    p.add_argument(
        "--out",
        default="./sessions",
        type=Path,
        help="Output directory (default: ./sessions)",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Auto-stop after N seconds. 0 = run until Ctrl-C (default: 0)",
    )
    p.add_argument(
        "--port-rx",
        type=int,
        default=33740,
        help="Local UDP port to receive on (default: 33740)",
    )
    p.add_argument(
        "--port-tx",
        type=int,
        default=33739,
        help="PS5 heartbeat target port (default: 33739)",
    )
    p.add_argument(
        "--status-interval",
        type=float,
        default=1.0,
        help="Seconds between live status lines (default: 1.0)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG-level logging on stderr",
    )
    return p.parse_args(argv)


def _open_outputs(out_dir: Path, fmt: str) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    stem = out_dir / f"capture_{ts}"
    return (
        stem.with_suffix(".bin"),
        stem.with_suffix(".csv"),
        stem.with_suffix(".json"),
    )


def _install_sigint_handler(receiver: Receiver) -> None:
    """Make Ctrl-C stop the receiver cleanly instead of raising mid-write."""

    def _handler(_signum: int, _frame: FrameType | None) -> None:
        log.info("caught SIGINT, stopping...")
        receiver.stop()

    signal.signal(signal.SIGINT, _handler)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = ReceiverConfig(
        ps5_ip=args.ip,
        port_rx=args.port_rx,
        port_tx=args.port_tx,
        packet_format=args.format,
    )
    expected_size = cfg.expected_size()
    bin_path, csv_path, json_path = _open_outputs(args.out, args.format)

    log.info("output: %s (+ .csv, .json)", bin_path)
    log.info("packet format: %r (expected size %d bytes)", args.format, expected_size)

    size_counter: Counter[int] = Counter()
    first_pkt: Packet | None = None
    last_pkt: Packet | None = None
    frames_written = 0
    rx_start: float | None = None

    receiver = Receiver(cfg)
    _install_sigint_handler(receiver)

    last_status_at = monotonic()
    pkts_since_status = 0

    # Open files in binary / text mode; we flush periodically below.
    with bin_path.open("wb") as bin_fh, csv_path.open("w", newline="", encoding="utf-8") as csv_fh:
        bin_fh.write(BIN_HEADER_MAGIC)
        bin_fh.write(args.format.encode("ascii"))

        writer = csv.DictWriter(csv_fh, fieldnames=_csv_field_names())
        writer.writeheader()

        try:
            receiver.start()
        except RuntimeError as exc:
            log.error("%s", exc)
            log.error("hint: pass --ip <PS5 IP> if broadcast discovery is blocked")
            return 2

        try:
            for raw_bytes, pkt in receiver.frames():
                if rx_start is None:
                    rx_start = pkt.recv_time
                    first_pkt = pkt
                last_pkt = pkt
                t_rel = pkt.recv_time - rx_start

                bin_fh.write(_FRAME_HEADER.pack(t_rel, len(raw_bytes)))
                bin_fh.write(raw_bytes)
                writer.writerow(_packet_row(pkt))

                size_counter[len(raw_bytes)] += 1
                frames_written += 1
                pkts_since_status += 1

                if args.duration > 0 and t_rel >= args.duration:
                    log.info("reached --duration %.1fs, stopping", args.duration)
                    receiver.stop()
                    break

                now = monotonic()
                if now - last_status_at >= args.status_interval:
                    pps = pkts_since_status / (now - last_status_at)
                    print(_format_status(pkt, pps, t_rel), flush=True)
                    last_status_at = now
                    pkts_since_status = 0
                    # Force on-disk durability so a hard kill doesn't lose data.
                    bin_fh.flush()
                    csv_fh.flush()
        finally:
            receiver.stop()
            bin_fh.flush()
            csv_fh.flush()

    duration = (last_pkt.recv_time - first_pkt.recv_time) if first_pkt and last_pkt else 0.0
    metadata = {
        "gt7coach_version": __version__,
        "captured_at_utc": datetime.utcnow().isoformat() + "Z",
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "node": platform.node(),
        },
        "config": {
            "ps5_ip": args.ip,
            "packet_format": args.format,
            "expected_packet_size": expected_size,
            "port_rx": args.port_rx,
            "port_tx": args.port_tx,
        },
        "stats": {
            "frames": frames_written,
            "duration_seconds": round(duration, 3),
            "average_pps": round(frames_written / duration, 2) if duration > 0 else None,
            "packet_size_histogram": dict(sorted(size_counter.items())),
            "first_packet_id": first_pkt.packet_id if first_pkt else None,
            "last_packet_id": last_pkt.packet_id if last_pkt else None,
            "last_lap": last_pkt.lap_count if last_pkt else None,
        },
    }
    json_path.write_text(json.dumps(metadata, indent=2))

    print()
    print(f"capture complete: {frames_written} frames in {duration:.1f}s")
    print(f"  bin:  {bin_path}")
    print(f"  csv:  {csv_path}")
    print(f"  json: {json_path}")
    if not frames_written:
        print("WARNING: no packets received. Check that GT7 is running, you're on", file=sys.stderr)
        print("the same LAN as the PS5, and Windows Firewall isn't blocking", file=sys.stderr)
        print(f"inbound UDP on port {args.port_rx}.", file=sys.stderr)
        return 1
    return 0


def iter_bin_capture(path: Path) -> Iterator[tuple[float, bytes]]:
    """Yield ``(t_rel, decrypted_bytes)`` from a ``.bin`` capture file.

    Inverse of the writer in :func:`main`; lets future analysis code consume a
    capture without needing the ``.csv`` sidecar.
    """
    with path.open("rb") as fh:
        magic = fh.read(len(BIN_HEADER_MAGIC))
        if magic != BIN_HEADER_MAGIC:
            raise ValueError(f"{path}: not a GT7 capture (magic was {magic!r})")
        _fmt = fh.read(1)  # format byte; not needed for raw iteration
        while True:
            hdr = fh.read(_FRAME_HEADER.size)
            if not hdr:
                return
            if len(hdr) < _FRAME_HEADER.size:
                raise ValueError(f"{path}: truncated frame header")
            t_rel, size = _FRAME_HEADER.unpack(hdr)
            payload = fh.read(size)
            if len(payload) < size:
                raise ValueError(f"{path}: truncated frame payload")
            yield t_rel, payload


if __name__ == "__main__":
    raise SystemExit(main())
