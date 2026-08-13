"""Rate limiting for the coach.

Implements section 7's two rules:
    * Global cooldown — at most one advice every ``global_cooldown_s`` seconds.
    * Per-type cooldown — suppress duplicate advice types within
      ``duplicate_window_s`` seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic


@dataclass(slots=True)
class RateLimiterConfig:
    # 4.0 s read as too quiet in testing and 2.5 s as slightly busy, so split
    # the difference. This is the single biggest lever on how the coach feels.
    global_cooldown_s: float = 3.5
    duplicate_window_s: float = 30.0


@dataclass(slots=True)
class RateLimiter:
    config: RateLimiterConfig = field(default_factory=RateLimiterConfig)
    # Initialise far in the past so the first call after construction is always
    # allowed, regardless of whether the caller uses monotonic() (>0) or a
    # zero-based fake clock in tests.
    _last_advice_t: float = -1e9
    _last_type_t: dict[str, float] = field(default_factory=dict)

    def allow(self, event_type: str, *, now: float | None = None) -> bool:
        """Return True if a new advice of ``event_type`` may fire right now."""
        t = monotonic() if now is None else now
        if t - self._last_advice_t < self.config.global_cooldown_s:
            return False
        last_same = self._last_type_t.get(event_type)
        if last_same is not None and (t - last_same) < self.config.duplicate_window_s:
            return False
        return True

    def record(self, event_type: str, *, now: float | None = None) -> None:
        """Record that an advice of ``event_type`` was just emitted."""
        t = monotonic() if now is None else now
        self._last_advice_t = t
        self._last_type_t[event_type] = t
