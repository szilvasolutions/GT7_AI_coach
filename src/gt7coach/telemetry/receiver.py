"""UDP receiver for GT7 telemetry.

Responsibilities:
    * Bind a UDP socket on the local receive port.
    * Send a periodic heartbeat byte to the PS5 so it keeps streaming.
    * Discover the PS5's IP by broadcast on first contact; allow an explicit
      override via config; fall back to a subnet scan if broadcast fails.
    * Decrypt + parse incoming packets and hand :class:`Packet` objects to a
      caller-supplied callback (or generator).

References:
    * zetetos/gt-telemetry, internal/reader/udpreader.go (heartbeat semantics)
    * snipem/gt7dashboard, gt7dashboard/gt7communication.py (10s heartbeat cadence,
      16s game-side timeout)
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
DEFAULT_HEARTBEAT_SECONDS = 10.0  # game disconnects at 16s silence
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


class Receiver:
    """Synchronous-friendly UDP receiver.

    Use as a context manager, or call :meth:`packets` to iterate. Heartbeats
    run on a background thread; the iterator runs on the calling thread.
    """

    def __init__(self, cfg: ReceiverConfig | None = None) -> None:
        self.cfg = cfg or ReceiverConfig()
        self._sock: socket.socket | None = None
        self._target_ip: str | None = None
        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

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
        sock.bind(("0.0.0.0", self.cfg.port_rx))
        sock.settimeout(1.0)
        self._sock = sock

        self._stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="gt7-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()
        log.info(
            "receiver started on port %d, heartbeat -> %s:%d (format %r)",
            self.cfg.port_rx,
            self._target_ip,
            self.cfg.port_tx,
            self.cfg.packet_format,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
            self._heartbeat_thread = None

    # ---- main loop ----------------------------------------------------------

    def packets(self) -> Iterator[Packet]:
        """Yield :class:`Packet` objects until :meth:`stop` is called."""
        for _raw, pkt in self.frames():
            yield pkt

    def frames(self) -> Iterator[tuple[bytes, Packet]]:
        """Yield ``(decrypted_bytes, Packet)`` pairs until :meth:`stop` is called.

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
                continue
            except OSError:
                if self._stop.is_set():
                    break
                raise
            if len(data) < expected:
                log.debug("ignoring short packet (%d bytes)", len(data))
                continue
            try:
                decrypted = decrypt_packet(data, fmt=self.cfg.packet_format)
                pkt = parse_packet(decrypted, recv_time=monotonic())
            except (ValueError, IndexError) as exc:
                log.warning("packet parse failed: %s", exc)
                continue
            yield decrypted, pkt

    def run(self, on_packet: Callable[[Packet], None]) -> None:
        """Drive the receive loop, invoking ``on_packet`` for each frame."""
        for pkt in self.packets():
            on_packet(pkt)

    # ---- heartbeat ----------------------------------------------------------

    def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats from the bound receive socket.

        GT7's protocol: the PS5 sends telemetry back to the source port of the
        most recent heartbeat it received. If we use a fresh unbound socket
        here, the OS picks a random source port and the PS5 switches its
        telemetry target away from port_rx (33740) — so the receive loop goes
        deaf within seconds, even though the heartbeat thread is still firing.

        Reference implementations (gt7dashboard, gt-telemetry) all send
        heartbeats from the receive socket for exactly this reason.
        """
        assert self._target_ip is not None
        heartbeat = self.cfg.packet_format.encode("ascii")
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                sock.sendto(heartbeat, (self._target_ip, self.cfg.port_tx))
            except OSError as exc:
                # Socket closed during shutdown is expected; anything else is real.
                if not self._stop.is_set():
                    log.warning("heartbeat send failed: %s", exc)
            if self._stop.wait(self.cfg.heartbeat_seconds):
                break


@dataclass(slots=True)
class _NullSink:
    """Used internally when callers want :meth:`Receiver.run` without a callback."""

    sink: list[Packet] = field(default_factory=list)

    def __call__(self, pkt: Packet) -> None:  # pragma: no cover
        self.sink.append(pkt)
