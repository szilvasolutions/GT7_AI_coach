"""Coach: turns detected Events into spoken coaching utterances.

The coach layer never sees raw telemetry — only :class:`Event` objects from
the detector layer. That's the whole point: the LLM can't talk about
throttle if no throttle event was detected.
"""

from gt7coach.coach.advisor import Advisor, AdvisorConfig, CornerContext
from gt7coach.coach.prompt import SYSTEM_PROMPT, build_user_prompt
from gt7coach.coach.providers import (
    AnthropicProvider,
    CoachProvider,
    GeminiProvider,
    MockProvider,
    OllamaProvider,
    OpenAIProvider,
    ProviderError,
    make_provider,
)
from gt7coach.coach.rate_limiter import RateLimiter, RateLimiterConfig

__all__ = [
    "SYSTEM_PROMPT",
    "Advisor",
    "AdvisorConfig",
    "AnthropicProvider",
    "CoachProvider",
    "CornerContext",
    "GeminiProvider",
    "MockProvider",
    "OllamaProvider",
    "OpenAIProvider",
    "ProviderError",
    "RateLimiter",
    "RateLimiterConfig",
    "build_user_prompt",
    "make_provider",
]
