"""Per-corner self-delta. NOT WIRED IN — see the module docstring and the
collision test at the bottom for why."""

from __future__ import annotations

from gt7coach.coach.corner_history import CornerHistory, trace_is_clean
from gt7coach.detectors import CornerTrace
from tests._synth import make_packet


def _corner(min_speed=90.0, exit_speed=120.0, lap=1, n=40, jump_at=None, stop_at=None):
    pkts = []
    for i in range(n):
        v = min_speed if i < n // 2 else exit_speed
        x = float(i)
        if jump_at is not None and i >= jump_at:
            x += 500.0  # respawn
        if stop_at is not None and i >= stop_at:
            v = 0.5  # hit something
        pkts.append(make_packet(recv_time=i / 60, speed_kmh=v, pos=(x, 0.0, 0.0), lap_count=lap))
    return CornerTrace(packets=pkts)


def test_first_pass_sets_the_baseline_and_reports_nothing():
    hist = CornerHistory()
    assert hist.compare_and_record(3, _corner()) is None
    assert hist.known_turns() == 1


def test_second_pass_reports_the_difference():
    hist = CornerHistory()
    hist.compare_and_record(3, _corner(min_speed=90.0, lap=1))
    hist.compare_and_record(7, _corner())  # a different corner in between
    delta = hist.compare_and_record(3, _corner(min_speed=98.0, lap=2))
    assert delta is not None
    assert delta.min_speed_delta_kmh == 8.0
    assert "quicker" in delta.describe()


def test_a_slower_pass_names_the_lap_they_did_better():
    hist = CornerHistory()
    hist.compare_and_record(3, _corner(min_speed=100.0, lap=1))
    hist.compare_and_record(7, _corner())
    delta = hist.compare_and_record(3, _corner(min_speed=88.0, lap=4))
    assert "slower" in delta.describe()
    assert "lap 1" in delta.describe()


def test_noise_is_not_reported():
    hist = CornerHistory()
    hist.compare_and_record(3, _corner(min_speed=90.0, exit_speed=120.0))
    hist.compare_and_record(7, _corner())
    delta = hist.compare_and_record(3, _corner(min_speed=91.0, exit_speed=121.0))
    assert delta.is_meaningful is False
    assert delta.describe() is None


def test_the_best_is_kept_not_the_latest():
    hist = CornerHistory()
    hist.compare_and_record(3, _corner(min_speed=100.0, lap=1))
    hist.compare_and_record(7, _corner())
    hist.compare_and_record(3, _corner(min_speed=80.0, lap=2))  # a bad lap
    hist.compare_and_record(7, _corner())
    delta = hist.compare_and_record(3, _corner(min_speed=95.0, lap=3))
    assert delta.min_speed_delta_kmh == -5.0, "must still compare against the 100 km/h pass"


def test_a_teleport_is_not_a_lap():
    assert trace_is_clean(_corner(jump_at=20)) is False


def test_a_crash_is_not_a_lap():
    """The logged wall hit went 32.1 -> 0.5 km/h. A 40 km/h bar missed it."""
    assert trace_is_clean(_corner(min_speed=32.0, exit_speed=32.0, stop_at=25)) is False


def test_unclean_corners_never_become_a_personal_best():
    hist = CornerHistory()
    assert hist.compare_and_record(3, _corner(jump_at=10)) is None
    assert hist.known_turns() == 0, "a respawn must not poison the reference"


def test_unidentified_turns_are_skipped():
    hist = CornerHistory()
    assert hist.compare_and_record(None, _corner()) is None
    assert hist.known_turns() == 0


def test_a_split_corner_is_not_compared_against_itself():
    """The segmenter sometimes emits one corner as two segments; the reference
    session produced pairs at an identical 86 and 93 km/h. Two segments at the
    same place back-to-back are one corner, not two passes."""
    hist = CornerHistory()
    hist.compare_and_record(3, _corner(min_speed=86.0, lap=2))
    assert hist.compare_and_record(3, _corner(min_speed=86.0, lap=2)) is None
