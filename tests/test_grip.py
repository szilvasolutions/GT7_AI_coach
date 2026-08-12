"""Grip utilisation — the absolute reference a beginner needs.

Numbers in these tests come from Ádám's Deep Forest session, which is the
regression fixture at /root/backups/gt7/session-2026-08-12/.
"""

from __future__ import annotations

import pytest

from gt7coach.coach.grip import (
    MIN_SAMPLES_FOR_ENVELOPE,
    GripEnvelope,
    achievable_speed_kmh,
    apex_position,
    balance,
    corner_grip,
    driven_radius_m,
    elevation_change_m,
)
from gt7coach.detectors import CornerTrace
from tests._synth import make_packet

G = 9.80665


def _trace(n=60, *, lat_g=1.5, lon_g=0.0, speed=120.0, radius=None):
    pkts = []
    for i in range(n):
        x, z = (0.0, float(i))
        if radius:  # lay the points on an arc of the requested radius
            import math

            th = (i / n) * 1.2
            x, z = radius * math.cos(th), radius * math.sin(th)
        pkts.append(
            make_packet(
                recv_time=i / 60,
                speed_kmh=speed,
                accel_lat=lat_g * G,
                accel_long=lon_g * G,
                pos=(x, 0.0, z),
            )
        )
    return CornerTrace(packets=pkts)


def _ready_envelope(limit_g=2.0):
    env = GripEnvelope()
    for i in range(MIN_SAMPLES_FOR_ENVELOPE + 50):
        env.feed(make_packet(accel_lat=limit_g * G if i % 10 == 0 else 0.5 * G))
    return env


def test_envelope_needs_evidence_before_it_reports():
    """A driver who has not leaned on the car has not shown us its limit."""
    env = GripEnvelope()
    for _ in range(10):
        env.feed(make_packet(accel_lat=1.0 * G))
    assert env.ready is False
    assert env.limit_g is None
    assert corner_grip(_trace(), env).overall is None


def test_envelope_learns_the_cars_limit():
    env = _ready_envelope(limit_g=2.0)
    assert env.ready
    assert env.limit_g == pytest.approx(2.0, abs=0.15)


def test_envelope_ignores_impact_spikes():
    """The wall hit registered ~45 g. That is not grip."""
    env = _ready_envelope(limit_g=1.8)
    before = env.limit_g
    env.feed(make_packet(accel_long=-45.0 * G))
    assert env.limit_g == pytest.approx(before, abs=0.01)


def test_utilisation_never_exceeds_one():
    """'You used 135% of the available grip' is worse than useless — it was
    the first version's output on the hairpin."""
    env = _ready_envelope(limit_g=1.5)
    hard = _trace(lat_g=3.0)  # beyond the learned envelope
    assert corner_grip(hard, env).overall == pytest.approx(1.0)


def test_utilisation_reports_a_timid_corner():
    env = _ready_envelope(limit_g=2.0)
    timid = _trace(lat_g=1.0)
    used = corner_grip(timid, env).overall
    assert 0.4 < used < 0.6, f"expected roughly half the grip used, got {used}"


def test_utilisation_depends_only_on_the_ratio():
    """Both sides read the same channel, so the scale cancels — which is why
    utilisation ships while achievable_speed_kmh (needing absolute mu) does
    not. Note the envelope's impact filter is itself scale-dependent, so this
    holds within the plausible range rather than universally."""
    half_of_one = corner_grip(_trace(lat_g=0.5), _ready_envelope(limit_g=1.0)).overall
    half_of_two = corner_grip(_trace(lat_g=1.0), _ready_envelope(limit_g=2.0)).overall
    assert half_of_one == pytest.approx(half_of_two, abs=0.02)


def test_weakest_phase_points_at_the_throttle():
    """Braking and mid-corner committed, timid getting back on the power."""
    env = _ready_envelope(limit_g=2.0)
    pkts = []
    for i in range(60):
        # V-shaped speed so minimum speed lands mid-corner and the phases split
        speed = 150.0 - 2.0 * i if i < 30 else 90.0 + 2.0 * (i - 30)
        phase_g = 0.6 if i > 30 else 1.9  # timid from minimum speed onward
        pkts.append(make_packet(recv_time=i / 60, speed_kmh=speed, accel_lat=phase_g * G))
    assert corner_grip(CornerTrace(packets=pkts), env).weakest_phase == "exit"


def test_driven_radius_survives_min_speed_at_either_end():
    """Anchoring the three sample points on the apex returned None for more
    than half the corners in the reference session."""
    for speed_profile in ("falling", "rising"):
        pkts = []
        for i in range(60):
            import math

            th = (i / 60) * 1.0
            v = 150.0 - i if speed_profile == "falling" else 90.0 + i
            pkts.append(
                make_packet(
                    recv_time=i / 60, speed_kmh=v, pos=(80 * math.cos(th), 0.0, 80 * math.sin(th))
                )
            )
        assert driven_radius_m(CornerTrace(packets=pkts)) is not None


def test_no_speed_target_for_a_chicane_or_a_flat_kink():
    """A chicane is not one arc; a long-radius kink is power-limited. The first
    pass produced a 421 km/h 'target' through a chicane."""
    env = _ready_envelope()
    chicane = _trace(radius=60)
    object.__setattr__(chicane, "packets", chicane.packets)
    assert achievable_speed_kmh(_ChicaneTrace(chicane.packets), env) is None
    assert achievable_speed_kmh(_trace(radius=900), env) is None


class _ChicaneTrace(CornerTrace):
    @property
    def corner_type(self) -> str:
        return "chicane"


def test_balance_flags_rotation_and_push():
    """omega*v/a_lat: >1 rotating, <1 pushing. Unit-free, no car data."""
    rotating = CornerTrace(
        packets=[make_packet(speed_kmh=108.0, accel_lat=1.0 * G, yaw_rate=0.4) for _ in range(30)]
    )
    pushing = CornerTrace(
        packets=[make_packet(speed_kmh=108.0, accel_lat=1.0 * G, yaw_rate=0.2) for _ in range(30)]
    )
    assert balance(rotating) > balance(pushing)


def test_apex_position_and_elevation():
    pkts = [
        make_packet(recv_time=i / 60, speed_kmh=150.0 - i, pos=(0.0, -i * 0.2, float(i)))
        for i in range(50)
    ]
    tr = CornerTrace(packets=pkts)
    assert apex_position(tr) == pytest.approx(1.0, abs=0.05)  # slowest at the end
    assert elevation_change_m(tr) == pytest.approx(-9.8, abs=0.2)  # downhill
