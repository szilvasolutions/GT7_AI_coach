"""Tests for the incident (spin / crash) pipeline."""

from __future__ import annotations

from gt7coach.coach import Advisor, AdvisorConfig, MockProvider, RateLimiter, RateLimiterConfig
from gt7coach.coach.advisor import (
    _INCIDENT_FALLBACKS,
    incident_fallback_phrase,
)
from gt7coach.coach.providers import ProviderError
from gt7coach.detectors.incident import Incident, IncidentDetector, IncidentDetectorConfig
from gt7coach.voice import NullVoiceEngine
from tests._synth import make_packet

G = 9.80665


# ---- IncidentDetector ------------------------------------------------------


def _feed(detector: IncidentDetector, packets) -> list[Incident]:
    out: list[Incident] = []
    for p in packets:
        inc = detector.feed(p)
        if inc is not None:
            out.append(inc)
    return out


def test_detector_emits_spin_at_high_yaw_rate() -> None:
    det = IncidentDetector(IncidentDetectorConfig(spin_min_duration_s=0.10))
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=80, yaw_rate=3.0) for i in range(20)
    ]
    out = _feed(det, pkts)
    assert len(out) == 1
    assert out[0].type == "spin"
    assert out[0].evidence["peak_yaw_rate_rad_s"] >= 2.5


def test_detector_ignores_high_yaw_at_low_speed() -> None:
    det = IncidentDetector()
    pkts = [
        make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=10, yaw_rate=3.0) for i in range(20)
    ]
    assert _feed(det, pkts) == []


def test_detector_emits_crash_on_g_spike() -> None:
    det = IncidentDetector()
    pkts = [
        make_packet(
            packet_id=0,
            recv_time=0.0,
            speed_kmh=120,
            accel_long=-5.5 * G,  # 5.5 g deceleration -- impact
            accel_lat=0.0,
        )
    ]
    out = _feed(det, pkts)
    assert len(out) == 1
    assert out[0].type == "crash"
    assert out[0].evidence["peak_g"] >= 4.0


def test_detector_respects_cooldown() -> None:
    det = IncidentDetector(IncidentDetectorConfig(spin_min_duration_s=0.05, cooldown_s=2.0))
    pkts = []
    # First spin at t=0..0.4s
    for i in range(20):
        pkts.append(make_packet(packet_id=i, recv_time=i * 0.02, speed_kmh=80, yaw_rate=3.0))
    # Brief calm, then second spin at t=1.0..1.4s -- WITHIN cooldown
    for i in range(20):
        pkts.append(
            make_packet(packet_id=20 + i, recv_time=1.0 + i * 0.02, speed_kmh=80, yaw_rate=3.0)
        )
    # Third spin at t=3.0..3.4s -- AFTER cooldown
    for i in range(20):
        pkts.append(
            make_packet(packet_id=40 + i, recv_time=3.0 + i * 0.02, speed_kmh=80, yaw_rate=3.0)
        )
    out = _feed(det, pkts)
    assert len(out) == 2  # second spin suppressed by cooldown


def test_detector_quiet_in_clean_corner() -> None:
    """Normal hard cornering at 1.5 g lat must NOT trigger a crash."""
    det = IncidentDetector()
    pkts = [
        make_packet(
            packet_id=i,
            recv_time=i * 0.02,
            speed_kmh=120,
            accel_lat=1.5 * G,  # hard but normal
            yaw_rate=0.6,
        )
        for i in range(60)
    ]
    assert _feed(det, pkts) == []


# ---- fallback phrases ------------------------------------------------------


def test_incident_fallback_phrase_returns_one_of_each_pool() -> None:
    assert incident_fallback_phrase("spin") in _INCIDENT_FALLBACKS["spin"]
    assert incident_fallback_phrase("crash") in _INCIDENT_FALLBACKS["crash"]


def test_incident_fallback_unknown_type_returns_empty() -> None:
    assert incident_fallback_phrase("nope") == ""


# ---- Advisor.on_incident ---------------------------------------------------


def _make_advisor(provider):
    return Advisor(
        provider=provider,
        voice=NullVoiceEngine(),
        rate_limiter=RateLimiter(RateLimiterConfig(global_cooldown_s=0.0, duplicate_window_s=0.0)),
        config=AdvisorConfig(driver_style="smooth", min_severity=0.0),
    )


def test_advisor_speaks_llm_response_on_incident() -> None:
    provider = MockProvider(responder=lambda _s, _u: "Well, that was elegant.")
    advisor = _make_advisor(provider)
    inc = Incident(type="spin", severity=0.8, recv_time=0.0)
    res = advisor.on_incident(inc)
    assert res.advice == "Well, that was elegant."
    assert res.suppressed_reason is None
    assert advisor.voice.spoken == ["Well, that was elegant."]


def test_advisor_falls_back_when_provider_fails_on_incident() -> None:
    def boom(_s, _u):
        raise ProviderError("nope")

    provider = MockProvider(responder=boom)
    advisor = _make_advisor(provider)
    inc = Incident(type="crash", severity=1.0, recv_time=0.0)
    res = advisor.on_incident(inc)
    assert res.advice in _INCIDENT_FALLBACKS["crash"]
    assert res.suppressed_reason is not None and "provider-error" in res.suppressed_reason
    assert len(advisor.voice.spoken) == 1


def test_advisor_strips_quotes_from_llm_response() -> None:
    provider = MockProvider(responder=lambda _s, _u: '"Hope you brought the spare."')
    advisor = _make_advisor(provider)
    inc = Incident(type="crash", severity=0.7, recv_time=0.0)
    res = advisor.on_incident(inc)
    assert res.advice == "Hope you brought the spare."


def test_advisor_rejects_single_word_remark_and_uses_fallback() -> None:
    provider = MockProvider(responder=lambda _s, _u: "Oops")
    advisor = _make_advisor(provider)
    inc = Incident(type="spin", severity=0.6, recv_time=0.0)
    res = advisor.on_incident(inc)
    assert res.advice in _INCIDENT_FALLBACKS["spin"]


# ---- Voice.interrupt -------------------------------------------------------


def test_null_voice_interrupt_just_records() -> None:
    v = NullVoiceEngine()
    v.speak("first")
    v.interrupt("URGENT")
    assert v.spoken == ["first", "URGENT"]
