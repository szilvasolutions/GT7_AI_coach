"""Corner segmentation state machine.

Reads a stream of :class:`Packet` objects and emits :class:`CornerTrace`
objects spanning each cornering event. Hysteresis on the exit prevents the
state machine from oscillating across the threshold the way the legacy V62
state machine did (8 entries / 100 ms in its logs).

A ``max_corner_duration_s`` cap also prevents a single CornerTrace from
covering an entire track section (the live capture had 10 s and 18 s
"corners" — useless for coaching). When the cap is hit, the segmenter
finalises at the best split point inside the buffer (steering zero-cross
preferred, else local min-speed) and keeps the remainder as the start of
the next corner.
"""

from __future__ import annotations

import math
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
    # Force-finalise the running buffer if it exceeds this duration; long
    # "corners" are typically merged sections that are unhelpful to coach as
    # one unit. 0 disables the cap.
    max_corner_duration_s: float = 8.0


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

    @property
    def total_yaw_deg(self) -> float:
        """Total absolute heading change across the trace (degrees).

        Numerically integrated from ``yaw_rate`` (rad/s) so it gives a stable
        measure of "how much the car turned" regardless of trace length.
        """
        if len(self.packets) < 2:
            return 0.0
        total = 0.0
        for i in range(1, len(self.packets)):
            dt = self.packets[i].recv_time - self.packets[i - 1].recv_time
            if dt <= 0:
                continue
            total += abs(self.packets[i].yaw_rate) * dt
        return math.degrees(total)

    @property
    def yaw_sign_flips(self) -> int:
        """Number of times yaw_rate sign flips with magnitude above a deadband.

        Useful for spotting chicanes (one flip) and S-curves (two+).
        """
        deadband = 0.05  # rad/s
        last_sign = 0
        flips = 0
        for p in self.packets:
            if abs(p.yaw_rate) < deadband:
                continue
            sign = 1 if p.yaw_rate > 0 else -1
            if last_sign != 0 and sign != last_sign:
                flips += 1
            last_sign = sign
        return flips

    @property
    def corner_type(self) -> str:
        """Coarse classification used in the LLM prompt for context.

        Order matters: more specific tags first.
        """
        if self.yaw_sign_flips >= 1:
            return "chicane"
        min_s = self.min_speed_kmh
        yaw = self.total_yaw_deg
        if min_s < 65 and yaw > 100:
            return "hairpin"
        if min_s >= 160 and yaw < 60:
            return "fast_corner"
        if min_s >= 100 and self.duration_s > 4.0:
            return "sweeper"
        if min_s < 100:
            return "slow_corner"
        return "medium_corner"


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

        # Force-split if the running trace is getting too long.
        cap = self.config.max_corner_duration_s
        if cap > 0:
            running = self._buffer[-1].recv_time - self._buffer[0].recv_time
            if running >= cap:
                return self._force_split()
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

    def _force_split(self) -> CornerTrace | None:
        """Cap the running trace at ``max_corner_duration_s`` and keep the tail.

        Pick the cleanest place to cut inside the buffer:

        1. The latest steering zero-crossing (chicane / S-curve apex) — most
           natural physical boundary.
        2. Otherwise the latest local min-speed point (apex of a single corner).
        3. Otherwise just split at the current packet.

        The chosen index is included in the emitted trace; the remainder
        (starting at the next index) is retained as the seed of the next
        corner so we don't lose data.
        """
        split_idx = self._find_split_index()
        head = self._buffer[: split_idx + 1]
        tail = self._buffer[split_idx + 1 :]
        self._buffer = list(tail)
        # Reset the exit-pending state since we just cut; the tail starts fresh.
        self._exit_pending_since = None
        self._last_active_idx = max(0, len(self._buffer) - 1)
        if len(head) < 2:
            return None
        trace = CornerTrace(packets=head)
        if trace.duration_s >= self.config.min_corner_duration_s:
            return trace
        return None

    def _find_split_index(self) -> int:
        """Pick a split point inside the current buffer (see _force_split)."""
        # Search the last 70% of the buffer for a cleanish boundary.
        start = max(1, int(len(self._buffer) * 0.30))

        # 1. Latest steering zero-crossing in the search window.
        last_zero_cross = -1
        for i in range(start, len(self._buffer)):
            prev = self._buffer[i - 1].steer_angle
            cur = self._buffer[i].steer_angle
            if prev is None or cur is None:
                continue
            if (prev >= 0) != (cur >= 0):
                last_zero_cross = i
        if last_zero_cross > 0:
            return last_zero_cross

        # 2. Local minimum speed in the search window.
        speeds = [p.speed_kmh for p in self._buffer]
        local_min_idx = start
        for i in range(start + 1, len(self._buffer)):
            if speeds[i] < speeds[local_min_idx]:
                local_min_idx = i
        if 0 < local_min_idx < len(self._buffer) - 1:
            return local_min_idx

        # 3. Fallback: just cut at the most recent packet.
        return len(self._buffer) - 1

    def _cornering_signal(self, p: Packet) -> bool:
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        return p.brake > self.config.entry_brake or lat_g > self.config.entry_lat_g

    def _exit_signal(self, p: Packet) -> bool:
        lat_g = abs(p.accel_lat or 0.0) / G_MS2
        return p.brake < self.config.exit_brake and lat_g < self.config.exit_lat_g
