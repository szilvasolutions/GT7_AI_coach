"""Grip utilisation — how much of the car's available grip a corner used.

The coach needed an *absolute* reference. Comparing a driver against their own
best lap only measures consistency, and for a beginner their best lap is a
ceiling rather than a target: "be more like yourself" cannot make anyone fast.
Comparing against an ideal racing line fails differently — in traffic the ideal
line is not available, so advice based on it is unusable exactly when the
driver most needs help.

The traction circle solves both. At any instant the tyres deliver a combined
force, ``sqrt(lateral^2 + longitudinal^2)``. The car reveals its own limit
within a lap or two of committed driving, and the ratio of what was used to
what the car can do is:

* absolute — it is the car's limit, not the driver's history;
* self-calibrating — no per-car database, the car tells us;
* line-independent — it grades whatever arc was actually driven, so it still
  works when someone is alongside;
* actionable — "you had 40% more grip there" says what to do next.

Splitting it by phase says *where* the grip went unused: braking too gently,
timid mid-corner, or slow back to the throttle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from gt7coach.detectors import CornerTrace
from gt7coach.telemetry.packet import Packet

_G = 9.80665

# Below this the envelope is still guesswork — a driver who has not yet leaned
# on the car has not shown us its limit, and reporting "you used 98% of grip"
# from three timid corners would be worse than saying nothing.
MIN_SAMPLES_FOR_ENVELOPE = 600  # ~10 s at 60 Hz

# The envelope is a robust *maximum*, not a typical value: the question is
# "what is the most this car has shown it can do", and it gets compared against
# a corner's peak. A middling percentile here would make every corner that set
# the peak report over 100% utilisation, which is incoherent — that was the
# first version's bug. High enough to sit near the true peak, low enough to
# reject a handful of kerb strikes; the wall impact is already excluded by
# _MAX_PLAUSIBLE_G.
_ENVELOPE_PERCENTILE = 0.995
_MAX_PLAUSIBLE_G = 4.0


@dataclass
class GripEnvelope:
    """The car's demonstrated grip limit, learned from what it has done.

    Kept per car rather than per session so that a driver who spends the first
    laps warming up is not told they are at the limit when they are not.
    """

    _samples: list[float] = field(default_factory=list)
    _peak: float = 0.0

    def feed(self, packet: Packet) -> None:
        g = packet.combined_g
        # A wall impact reads as tens of g and is not grip.
        if 0.0 < g < _MAX_PLAUSIBLE_G:
            self._samples.append(g)
            self._peak = max(self._peak, g)

    @property
    def ready(self) -> bool:
        return len(self._samples) >= MIN_SAMPLES_FOR_ENVELOPE

    @property
    def limit_g(self) -> float | None:
        """High-percentile combined g — the car's usable limit."""
        if not self.ready:
            return None
        ordered = sorted(self._samples)
        return ordered[min(int(len(ordered) * _ENVELOPE_PERCENTILE), len(ordered) - 1)]

    def utilisation(self, used_g: float) -> float | None:
        """Fraction of the available grip that ``used_g`` represents, 0..1.

        Clamped, because you cannot use more grip than the car has. A reading
        above 1.0 means the envelope estimate is a little low, not that the
        driver broke physics — and "you used 135% of the available grip" is
        worse than useless as a coaching line. The corner that set the peak
        reads 100%, and the envelope keeps learning as the session goes on.
        """
        limit = self.limit_g
        if limit is None or limit <= 0.0:
            return None
        return min(1.0, used_g / limit)


@dataclass(frozen=True)
class CornerGrip:
    """Grip utilisation for one corner, whole and by phase."""

    overall: float | None
    entry: float | None  # braking phase
    mid: float | None  # from min speed, either side
    exit: float | None  # back on the throttle
    weakest_phase: str | None  # where the most grip went unused

    @property
    def headline(self) -> str | None:
        if self.overall is None:
            return None
        return f"{round(self.overall * 100)}% of available grip"


def _phase_slices(trace: CornerTrace) -> tuple[list[Packet], list[Packet], list[Packet]]:
    """Split a trace into braking / mid / throttle phases around min speed."""
    packets = trace.packets
    if len(packets) < 3:
        return [], list(packets), []
    speeds = [p.speed_kmh for p in packets]
    apex = speeds.index(min(speeds))
    # A tenth of the corner either side of minimum speed counts as "mid".
    pad = max(1, len(packets) // 10)
    lo, hi = max(0, apex - pad), min(len(packets), apex + pad + 1)
    return packets[:lo], packets[lo:hi], packets[hi:]


def _peak_utilisation(
    packets: list[Packet], envelope: GripEnvelope, *, min_speed_kmh: float = 25.0
) -> float | None:
    usable = [p.combined_g for p in packets if p.speed_kmh >= min_speed_kmh]
    usable = [g for g in usable if 0.0 < g < _MAX_PLAUSIBLE_G]
    if not usable:
        return None
    return envelope.utilisation(max(usable))


def corner_grip(trace: CornerTrace, envelope: GripEnvelope) -> CornerGrip:
    """How much of the car's grip this corner actually used, by phase."""
    if not envelope.ready:
        return CornerGrip(None, None, None, None, None)

    entry_p, mid_p, exit_p = _phase_slices(trace)
    entry = _peak_utilisation(entry_p, envelope)
    mid = _peak_utilisation(mid_p, envelope)
    exit_ = _peak_utilisation(exit_p, envelope)
    overall = _peak_utilisation(list(trace.packets), envelope)

    named = [(n, v) for n, v in (("entry", entry), ("mid", mid), ("exit", exit_)) if v is not None]
    weakest = min(named, key=lambda kv: kv[1])[0] if named else None
    return CornerGrip(overall=overall, entry=entry, mid=mid, exit=exit_, weakest_phase=weakest)


def driven_radius_m(trace: CornerTrace) -> float | None:
    """Radius of the arc the driver actually drove through the corner.

    Deliberately measured from the driver's own path rather than the track
    centreline: it is the line they were on, including a line compromised by
    another car, and it is what their speed has to be judged against.
    """
    packets = trace.packets
    if len(packets) < 9:
        return None
    # Sample at fixed fractions of the corner rather than around minimum
    # speed. Anchoring on the apex collapsed the three points whenever minimum
    # speed fell at either end of the trace, which is common — it silently
    # returned None for more than half the corners in the reference session.
    n = len(packets)
    i, j, k = n // 4, n // 2, (3 * n) // 4
    if i == j or j == k:
        return None
    a, b, c = packets[i], packets[j], packets[k]
    ab = math.dist((a.pos_x, a.pos_z), (b.pos_x, b.pos_z))
    bc = math.dist((b.pos_x, b.pos_z), (c.pos_x, c.pos_z))
    ac = math.dist((a.pos_x, a.pos_z), (c.pos_x, c.pos_z))
    area = (
        abs((b.pos_x - a.pos_x) * (c.pos_z - a.pos_z) - (c.pos_x - a.pos_x) * (b.pos_z - a.pos_z))
        / 2.0
    )
    if area < 1e-6:
        return None
    r = (ab * bc * ac) / (4.0 * area)
    return None if r > 2000.0 else r


# Beyond this the corner is not grip-limited — it is a kink you take flat, and
# sqrt(mu*g*r) returns a speed the car cannot reach anyway.
_MAX_GRIP_LIMITED_RADIUS_M = 150.0


def achievable_speed_kmh(trace: CornerTrace, envelope: GripEnvelope) -> float | None:
    """EXPERIMENTAL — not wired into coaching. See the unit caveat below.

    Speed the car could hold on the arc the driver actually drove.

    ``v = sqrt(mu * g * r)``, with mu from what this car has demonstrated and r
    from the driven path — so it answers "could I have carried more speed on
    the line I took", which still has a useful answer when the ideal line was
    not available because someone was alongside.

    Returns None where the question is meaningless: a chicane is not one arc,
    and a long-radius kink is limited by power rather than grip. Getting this
    wrong produced a 421 km/h "target" through a chicane on the first pass.

    **Why this is not spoken yet.** Unlike utilisation, which is a ratio of the
    same channel to itself and so is immune to the scale, this needs the
    absolute value of mu. ``accel_lat`` is read from ``_OFF_BODY_SWAY``, which
    the packet notes describe as normalised to -1..+1, yet the reference
    session carries values of 10-25. Until the scale is confirmed against a car
    of known grip, treating those numbers as m/s^2 could hand the driver a
    target that is confidently wrong — the reference lap already showed this
    disagreeing with utilisation, claiming 54 km/h was available through a
    hairpin that had already used 100% of the car's measured grip.
    """
    if trace.corner_type == "chicane":
        return None
    radius_m = driven_radius_m(trace)
    limit = envelope.limit_g
    if radius_m is None or limit is None or radius_m <= 0.0:
        return None
    if radius_m > _MAX_GRIP_LIMITED_RADIUS_M:
        return None
    return math.sqrt(limit * _G * radius_m) * 3.6


def balance(trace: CornerTrace) -> float | None:
    """Rotation versus grip: >1 the car is rotating more than the lateral load
    justifies (oversteer), <1 it is pushing (understeer).

    Uses ``omega * v / a_lat``, which is unit-free and needs no wheelbase or
    any other car parameter.
    """
    ratios = []
    for p in trace.packets:
        lat = abs(p.accel_lat or 0.0)
        if lat < 3.0 or p.speed_mps < 5.0:  # too little load to mean anything
            continue
        ratios.append(abs(p.yaw_rate) * p.speed_mps / lat)
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def apex_position(trace: CornerTrace) -> float | None:
    """Where minimum speed fell, 0.0 at corner entry to 1.0 at exit.

    Reaching minimum speed early is the classic time-loser: it delays the
    throttle and costs speed all the way down the following straight.
    """
    packets = trace.packets
    if len(packets) < 3:
        return None
    speeds = [p.speed_kmh for p in packets]
    return speeds.index(min(speeds)) / (len(packets) - 1)


def elevation_change_m(trace: CornerTrace) -> float:
    """Net height change through the corner. Negative is downhill."""
    packets = trace.packets
    if len(packets) < 2:
        return 0.0
    return packets[-1].pos_y - packets[0].pos_y


def axle_slip(trace: CornerTrace) -> tuple[float | None, float | None]:
    """(worst front slip under braking, worst rear slip under power).

    Front below ~0.9 is locking; rear above ~1.05 is spinning.
    """
    fronts = [p.front_slip for p in trace.packets if p.brake > 150 and p.front_slip is not None]
    rears = [p.rear_slip for p in trace.packets if p.throttle > 150 and p.rear_slip is not None]
    return (min(fronts) if fronts else None, max(rears) if rears else None)
