"""Track detection: matches the car's world position to a known GT7 circuit.

The track name itself never reaches the spoken coaching — it's a context tag
that shapes the LLM's advice priorities (a slow uphill hairpin gets different
coaching than a flat-out kink). See PHASE_6_NOTES for the database scope.
"""

from gt7coach.tracks.database import DEFAULT_TRACKS, Track
from gt7coach.tracks.detector import TrackDetector

__all__ = ["DEFAULT_TRACKS", "Track", "TrackDetector"]
