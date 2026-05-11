"""NullVoiceEngine: records utterances, never makes a sound.

Used by tests and by dry-run mode of the coach CLI.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class NullVoiceEngine:
    spoken: list[str] = field(default_factory=list)
    _busy: bool = False

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def is_idle(self) -> bool:
        return not self._busy

    def stop(self) -> None:  # pragma: no cover — nothing to clean up
        self._busy = False

    def set_busy(self, busy: bool) -> None:
        """Test helper: simulate a non-idle queue."""
        self._busy = busy
