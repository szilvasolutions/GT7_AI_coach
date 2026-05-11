"""Corner segmentation state machine.

Reads a stream of :class:`Packet` objects and emits :class:`CornerTrace`
objects spanning each cornering event. Hysteresis on the exit prevents the
state machine from oscillating across the threshold the way the legacy V62
state machine did (8 entries / 100 ms in its logs).
"""

from __future__ import annotations

from dataclasses import dataclass

from gt7coach.detectors.base import G_MS2
from gt7coach.telemetry.packet import Packet


@dataclass(slots=True)
class CornerSegmenterConfig:
    """Tunable thresholds for the segmenter.

    Defaults mirror ``config.example.yaml`` (section 10 of ARCHITECTURE.md).
    """

    min_speed_kmh: float = 45.0
    entry_brake: int = 65  # 0..255
    entry_lat_g: float = 0.95  # g
    exit_brake: int = 20  # 0..255
    exit_lat_g: float = 0.50  # g
    min_dwell_s: float = 0.5  # exit conditions must hold this long
    min_corner_duration_s: float = 0.7  # discard sub-threshold blips


@dataclass(slots=True)
class CornerTrace:
    """A list of consecutive packets covering one cornering event.

    Convenience properties expose corner-level summaries that the detector
    functions and coach prompt builder all need.
    """

    packets: list[Packet]

    def __len__(self) -> int:
        return len(self.packets)

    @property
    def duration_s(self) -> float:
        if len(self.packets) < 2:
            return 0.0
        return self.packets[-1].recv_time - self.packets[0].recv_time

    @property
    def entry_speed_kmh(self) -> float:
        return self.packets[0].speed_kmh

    @property
    def exit_speed_kmh(self) -> float:
        return self.packets[-1].speed_kmh

    @property
    def min_speed_kmh(self) -> float:
        return min(p.speed_kmh for p in self.packets)

    @property
    def peak_lat_g(self) -> float:
        return max(abs(p.accel_lat or 0.0) for p in self.packets) / G_MS2

    @property
    def start_time(self) -> float:
        return self.packets[0].recv_time


class CornerSegmenter:
    """State machine: STRAIGHT → CORNERING → STRAIGHT with exit hysteresis.

    Usage::

        seg = CornerSegmenter()
        for pkt in stream:
            trace = seg.feed(pkt)
            if trace is not None:
                # one corner just finalised
                ...
        leftover = seg.flush()  # at end-of-stream
    """

    def __init__(self, config: CornerSegmenterConfig | None = None) -> None:
        self.config = config or CornerSegmenterConfig()
        self._buffer: list[Packet] = []
        self._exit_pending_since: float | None = None
        self._last_active_idx: int = 0  # last index where exit_signal was False

    # ---- public ----------------------------------------------------------

    def feed(self, packet: Packet) -> CornerTrace | None:
        """Append a packet to the running segment; return a trace if one ends."""
        if not self._buffer:
            if self._cornering_signal(packet) and packet.speed_kmh > self.config.min_speed_kmh:
                self._buffer.append(packet)
                self._last_active_idx = 0
            return None

        self._buffer.append(packet)
        is_exit = self._exit_signal(packet)

        if is_exit:
            if self._exit_pending_since is None:
                self._exit_pending_since = packet.recv_time
            elif (packet.recv_time - self._exit_pending_since) >= self.config.min_dwell_s:
                return self._finalise()
        else:
            self._exit_pending_since = None
            self._last_active_idx = len(self._buffer) - 1
        return None

    def flush(self) -> CornerTrace | None:
        """Force-finalise any in-progress corner (used at end-of-stream)."""
        if self._buffer:
            return self._finalise()
        return None

    # ---- internals -------------------------------------------------------

    def _finalise(self) -> CornerTrace | None:
        # Trim the trailing exit-dwell tail so durations reflect actual cornering.
        active = self._buffer[: self._last_active_idx + 1]
        self._buffer = []
        self._exit_pending_since = None
        self._last_active_idx = 0
        if not active:
            return None
        trace = CornerTrace(packets=active)
        if trace.duration_s >= self.config.min_corner_duration_s:
            return trace
        return None

    def _cornering_signal(self, p: Packet) -> bool:
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        return p.brake > self.config.entry_brake or lat_g > self.config.entry_lat_g

    def _exit_signal(self, p: Packet) -> bool:
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        return p.brake < self.config.exit_brake and lat_g < self.config.exit_lat_g
