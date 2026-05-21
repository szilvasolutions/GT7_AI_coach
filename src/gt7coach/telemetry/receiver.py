"""UDP receiver for GT7 telemetry.

Responsibilities:
    * Bind a UDP socket on the local receive port (local IP first, falls back
      to 0.0.0.0).
    * Discover the PS5's IP by broadcast on first contact; allow an explicit
      override via config; fall back to a subnet scan if broadcast fails.
    * Drive a single-threaded send + receive loop. Heartbeats fire from the
      same socket as recvfrom, on three triggers: once at bind, every
      ``_HEARTBEAT_EVERY_N_PACKETS`` (~1.7s at 60Hz), and on every
      ``recvfrom`` timeout (1s default).
    * Decrypt + parse incoming packets and hand :class:`Packet` objects to a
      caller-supplied callback (or generator).
    * Run a pure-diagnostic stats sampler thread that logs packet rate every
      5 s and raises a 16s-disconnect alarm to make protocol-level dropouts
      obvious in debug.log.

References:
    * zetetos/gt-telemetry, internal/reader/udpreader.go (heartbeat semantics)
    * snipem/gt7dashboard, gt7dashboard/gt7communication.py (heartbeat cadence,
      16s game-side timeout)
    * legacy/gt_coach_AI_V23_goodbuttoomuchthrottlefocus.py lines 683-793 —
      this module mirrors that script's single-socket / single-thread design.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from time import monotonic

from gt7coach.telemetry.decrypt import FORMAT_PACKET_SIZES, decrypt_packet
from gt7coach.telemetry.packet import Packet, parse_packet

log = logging.getLogger(__name__)

DEFAULT_PORT_RX = 33740  # local listen
DEFAULT_PORT_TX = 33739  # PS5 heartbeat target
# Game disconnects at 16s silence. Send heartbeats every ~1.7s to leave
# margin for UDP drops — a single missed heartbeat at 10s cadence used to
# blow past the 16s timeout. The legacy reference implementation pinged
# every 100 packets (~1.7s at 60Hz) plus on every recvfrom timeout; we
# do the same here.
DEFAULT_HEARTBEAT_SECONDS = 1.7
DEFAULT_DISCOVERY_TIMEOUT = 3.0


@dataclass(slots=True)
class ReceiverConfig:
    """Receiver configuration.

    ``ps5_ip`` may be:
        * ``"auto"`` (or ``None``) — broadcast-discover the PS5.
        * an explicit dotted-quad string — heartbeat goes only to that address.
    """

    ps5_ip: str | None = "auto"
    port_rx: int = DEFAULT_PORT_RX
    port_tx: int = DEFAULT_PORT_TX
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS
    discovery_timeout: float = DEFAULT_DISCOVERY_TIMEOUT
    packet_format: str = "B"  # heartbeat byte; "B" enables steering + envelope
    broadcast_addr: str = "255.255.255.255"

    def expected_size(self) -> int:
        return FORMAT_PACKET_SIZES[self.packet_format]


def _local_ipv4() -> str:
    """Best-effort local IPv4 by opening a throwaway UDP socket to a public IP."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def discover_ps5(cfg: ReceiverConfig) -> str | None:
    """Try to learn the PS5's IP.

    Sends a heartbeat to the subnet broadcast address and waits for any UDP
    packet on ``port_rx``. The sender's address is the PS5.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", cfg.port_rx))
        sock.settimeout(cfg.discovery_timeout)
        sock.sendto(cfg.packet_format.encode("ascii"), (cfg.broadcast_addr, cfg.port_tx))
        log.info("broadcast discovery: heartbeat sent to %s:%d", cfg.broadcast_addr, cfg.port_tx)
        try:
            _, addr = sock.recvfrom(4096)
            log.info("discovered PS5 at %s", addr[0])
            return addr[0]
        except TimeoutError:
            log.warning("broadcast discovery: no reply within %.1fs", cfg.discovery_timeout)
            return _subnet_scan(cfg)
    finally:
        sock.close()


def _subnet_scan(cfg: ReceiverConfig) -> str | None:
    """Fallback: send a heartbeat to every host on the local /24."""
    local = _local_ipv4()
    if local == "127.0.0.1":
        log.error("subnet scan: no usable local IPv4")
        return None
    prefix = ".".join(local.split(".")[:-1])
    log.info("subnet scan: probing %s.1-254", prefix)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("0.0.0.0", cfg.port_rx))
        sock.settimeout(cfg.discovery_timeout)
        for i in range(1, 255):
            host = f"{prefix}.{i}"
            if host == local:
                continue
            try:
                sock.sendto(cfg.packet_format.encode("ascii"), (host, cfg.port_tx))
            except OSError:
                continue
        try:
            _, addr = sock.recvfrom(4096)
            log.info("subnet scan: PS5 at %s", addr[0])
            return addr[0]
        except TimeoutError:
            log.error("subnet scan: no PS5 found")
            return None
    finally:
        sock.close()


_HEARTBEAT_EVERY_N_PACKETS = 100  # ≈1.7s at 60 Hz; matches legacy V23


class Receiver:
    """Synchronous-friendly UDP receiver.

    Single-threaded send + receive on one socket: heartbeats fire from the
    main receive loop (every ~100 decoded packets AND on every recvfrom
    timeout), exactly matching the legacy V23 reference script. The only
    background thread is a pure-diagnostic stats sampler.
    """

    def __init__(self, cfg: ReceiverConfig | None = None) -> None:
        self.cfg = cfg or ReceiverConfig()
        self._sock: socket.socket | None = None
        self._target_ip: str | None = None
        self._stop = threading.Event()
        self._stats_thread: threading.Thread | None = None
        self._packets_decoded = 0
        self._packets_short = 0
        self._packets_failed = 0
        self._recv_timeouts = 0
        self._heartbeats_sent = 0
        self._heartbeats_failed = 0
        self._last_packet_at: float | None = None
        self._disconnect_alarm_fired = False

    # ---- lifecycle ----------------------------------------------------------

    def __enter__(self) -> Receiver:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        if self._sock is not None:
            return
        if self.cfg.ps5_ip in (None, "", "auto"):
            self._target_ip = discover_ps5(self.cfg)
            if self._target_ip is None:
                raise RuntimeError("PS5 not discovered; set network.ps5_ip in config to override.")
        else:
            self._target_ip = self.cfg.ps5_ip

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Legacy V23 (lines 686-687) tries the local IP first and falls back
        # to 0.0.0.0. On multi-NIC Windows laptops this pins egress + ingress
        # to the same interface so the PS5 sees a consistent source IP.
        bound_addr = self._bind_with_local_ip_preference(sock)
        sock.settimeout(1.0)
        self._sock = sock

        # Initial heartbeat from the bound socket — legacy V23 line 690.
        # Closes the timing gap between bind and the receive loop's first
        # heartbeat-on-timeout, so the PS5 immediately learns where to send.
        self._send_heartbeat(reason="initial")

        self._stop.clear()
        self._stats_thread = threading.Thread(
            target=self._stats_loop, name="gt7-rx-stats", daemon=True
        )
        self._stats_thread.start()
        log.info(
            "receiver started: bound=%s, heartbeat -> %s:%d (format %r, every %d pkts + on timeout)",
            bound_addr,
            self._target_ip,
            self.cfg.port_tx,
            self.cfg.packet_format,
            _HEARTBEAT_EVERY_N_PACKETS,
        )

    def _bind_with_local_ip_preference(self, sock: socket.socket) -> str:
        """Try local-IP bind first, fall back to 0.0.0.0. Returns the address
        actually bound, for logging.
        """
        local = _local_ipv4()
        if local != "127.0.0.1":
            try:
                sock.bind((local, self.cfg.port_rx))
                return f"{local}:{self.cfg.port_rx}"
            except OSError as exc:
                log.debug(
                    "local-IP bind %s:%d failed (%s); using 0.0.0.0", local, self.cfg.port_rx, exc
                )
        sock.bind(("0.0.0.0", self.cfg.port_rx))
        return f"0.0.0.0:{self.cfg.port_rx}"

    def _send_heartbeat(self, *, reason: str) -> None:
        """Send one heartbeat from the bound socket; bump counters; log at DEBUG."""
        sock = self._sock
        if sock is None or self._target_ip is None:
            return
        try:
            sock.sendto(
                self.cfg.packet_format.encode("ascii"),
                (self._target_ip, self.cfg.port_tx),
            )
            self._heartbeats_sent += 1
            log.debug(
                "heartbeat #%d (%s) -> %s:%d",
                self._heartbeats_sent,
                reason,
                self._target_ip,
                self.cfg.port_tx,
            )
        except OSError as exc:
            self._heartbeats_failed += 1
            if not self._stop.is_set():
                log.warning("heartbeat (%s) failed: %s", reason, exc)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._stats_thread is not None:
            self._stats_thread.join(timeout=2.0)
            self._stats_thread = None
        log.info(
            "receiver stopped. totals: packets=%d short=%d failed=%d timeouts=%d heartbeats=%d hb_failed=%d",
            self._packets_decoded,
            self._packets_short,
            self._packets_failed,
            self._recv_timeouts,
            self._heartbeats_sent,
            self._heartbeats_failed,
        )

    # ---- main loop ----------------------------------------------------------

    def packets(self) -> Iterator[Packet]:
        """Yield :class:`Packet` objects until :meth:`stop` is called."""
        for _raw, pkt in self.frames():
            yield pkt

    def frames(self) -> Iterator[tuple[bytes, Packet]]:
        """Yield ``(decrypted_bytes, Packet)`` pairs until :meth:`stop` is called.

        Single-threaded send + receive on one socket — heartbeats fire from
        this loop, not a side thread. Two trigger points (legacy V23 parity):

        * Every ``_HEARTBEAT_EVERY_N_PACKETS`` decoded packets (~1.7s at 60Hz)
        * On every ``recvfrom`` timeout (1s default)

        Useful for capture tooling that wants to persist the raw decrypted
        payload alongside the parsed view (so unknown offsets can be analysed
        later without another live session).
        """
        if self._sock is None:
            self.start()
        assert self._sock is not None
        expected = self.cfg.expected_size()
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(4096)
            except TimeoutError:
                # GT7 disconnects after 16s of silence. Whenever recvfrom
                # times out (1s default), re-prod the PS5 from this socket so
                # it remembers where to send telemetry. Mirrors legacy V23
                # line 793.
                self._recv_timeouts += 1
                self._send_heartbeat(reason="timeout")
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if len(data) < expected:
                self._packets_short += 1
                log.debug("ignoring short packet (%d bytes)", len(data))
                continue
            try:
                decrypted = decrypt_packet(data, fmt=self.cfg.packet_format)
                pkt = parse_packet(decrypted, recv_time=monotonic())
            except (ValueError, IndexError) as exc:
                self._packets_failed += 1
                log.warning("packet parse failed: %s", exc)
                continue
            self._packets_decoded += 1
            self._last_packet_at = monotonic()
            self._disconnect_alarm_fired = False  # PS5 is alive again
            # Periodic in-loop heartbeat — legacy V23 line 700.
            if self._packets_decoded % _HEARTBEAT_EVERY_N_PACKETS == 0:
                self._send_heartbeat(reason="periodic")
            yield decrypted, pkt

    def run(self, on_packet: Callable[[Packet], None]) -> None:
        """Drive the receive loop, invoking ``on_packet`` for each frame."""
        for pkt in self.packets():
            on_packet(pkt)

    def _stats_loop(self) -> None:
        """Log packet-rate stats every 5 seconds so a session log shows when
        the PS5 stopped sending and exactly when it stopped.
        """
        interval = 5.0
        last_decoded = 0
        last_timeouts = 0
        silent_intervals = 0
        while not self._stop.is_set():
            if self._stop.wait(interval):
                break
            now = monotonic()
            decoded = self._packets_decoded
            delta_decoded = decoded - last_decoded
            delta_timeouts = self._recv_timeouts - last_timeouts
            last_decoded = decoded
            last_timeouts = self._recv_timeouts
            since_last_pkt = (
                (now - self._last_packet_at) if self._last_packet_at is not None else None
            )
            if delta_decoded == 0:
                silent_intervals += 1
                last_pkt_str = (
                    f"{since_last_pkt:.1f}s ago" if since_last_pkt is not None else "never"
                )
                silent_for = silent_intervals * interval
                log.warning(
                    "rx stats: 0 packets in last %.0fs (timeouts=%d, hb_sent=%d, last_pkt=%s, silent_for=%.0fs)",
                    interval,
                    delta_timeouts,
                    self._heartbeats_sent,
                    last_pkt_str,
                    silent_for,
                )
                if silent_for >= 16.0 and not self._disconnect_alarm_fired:
                    self._disconnect_alarm_fired = True
                    log.error(
                        "GT7 disconnect timer (16s) tripped — PS5 has stopped streaming. "
                        "Pause/resume the race on the PS5 to restart telemetry, or restart "
                        "gt7coach-coach. Heartbeats are still firing (%d sent).",
                        self._heartbeats_sent,
                    )
            else:
                silent_intervals = 0
                log.info(
                    "rx stats: %d packets/%ds (%.1f Hz, total=%d, timeouts=%d, hb=%d)",
                    delta_decoded,
                    int(interval),
                    delta_decoded / interval,
                    decoded,
                    delta_timeouts,
                    self._heartbeats_sent,
                )


@dataclass(slots=True)
class _NullSink:
    """Used internally when callers want :meth:`Receiver.run` without a callback."""

    sink: list[Packet] = field(default_factory=list)

    def __call__(self, pkt: Packet) -> None:  # pragma: no cover
        self.sink.append(pkt)
