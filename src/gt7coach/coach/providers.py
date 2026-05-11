"""Provider abstraction: Anthropic, OpenAI, Gemini, Ollama, Mock.

Each provider implements the :class:`CoachProvider` protocol. SDK imports
are deferred to :meth:`__init__` so users only need the dependency for the
provider they actually use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Protocol

from gt7coach.coach.prompt import SYSTEM_PROMPT, build_user_prompt

if TYPE_CHECKING:
    from gt7coach.coach.advisor import CornerContext
    from gt7coach.detectors import Event

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Wraps any LLM-side failure so callers handle one exception type."""


class CoachProvider(Protocol):
    """Per ARCHITECTURE.md section 7."""

    def advise(
        self,
        events: Iterable[Event],
        context: CornerContext,
        driver_style: str,
    ) -> str: ...


# ---- Concrete implementations ----------------------------------------------


class AnthropicProvider:
    """Default provider. Uses claude-haiku-4-5 for latency (see spec §7)."""

    DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        max_tokens: int = 64,
        temperature: float = 0.4,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError(
                "anthropic SDK not installed. pip install 'gt7coach[anthropic]'"
            ) from exc
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature

    def advise(self, events, context, driver_style):
        user_text = build_user_prompt(events, context, driver_style)
        try:
            # Cache the stable system prompt so repeated calls stay cheap.
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_text}],
            )
            return _extract_text_from_anthropic(resp).strip()
        except Exception as exc:
            raise ProviderError(f"anthropic call failed: {exc}") from exc


def _extract_text_from_anthropic(resp) -> str:
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


class OpenAIProvider:
    """OpenAI Chat Completions. Defaults to a small, fast model."""

    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        max_tokens: int = 64,
        temperature: float = 0.4,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError("openai SDK not installed. pip install 'gt7coach[openai]'") from exc
        self._client = OpenAI(api_key=api_key)
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature

    def advise(self, events, context, driver_style):
        user_text = build_user_prompt(events, context, driver_style)
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            raise ProviderError(f"openai call failed: {exc}") from exc


class GeminiProvider:
    """Google Gemini (via the google-generativeai SDK)."""

    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        max_tokens: int = 64,
        temperature: float = 0.4,
    ) -> None:
        try:
            import google.generativeai as genai
        except ImportError as exc:
            raise ProviderError(
                "google-generativeai SDK not installed. pip install 'gt7coach[gemini]'"
            ) from exc
        genai.configure(api_key=api_key)
        self._genai = genai
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = genai.GenerativeModel(
            model_name=self.model,
            system_instruction=SYSTEM_PROMPT,
        )

    def advise(self, events, context, driver_style):
        user_text = build_user_prompt(events, context, driver_style)
        try:
            resp = self._client.generate_content(
                user_text,
                generation_config=self._genai.types.GenerationConfig(
                    max_output_tokens=self.max_tokens,
                    temperature=self.temperature,
                ),
            )
            return (resp.text or "").strip()
        except Exception as exc:
            raise ProviderError(f"gemini call failed: {exc}") from exc


class OllamaProvider:
    """Local Ollama daemon. Free, no API key. Slower."""

    DEFAULT_MODEL = "llama3.1:8b"
    DEFAULT_URL = "http://localhost:11434"

    def __init__(
        self,
        model: str | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        try:
            import requests  # noqa: F401  (verify availability now)
        except ImportError as exc:
            raise ProviderError("requests not installed. pip install 'gt7coach[ollama]'") from exc
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_URL).rstrip("/")
        self.timeout_s = timeout_s

    def advise(self, events, context, driver_style):
        import requests

        user_text = build_user_prompt(events, context, driver_style)
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_text},
                    ],
                    "stream": False,
                },
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            return (resp.json().get("message", {}).get("content") or "").strip()
        except Exception as exc:
            raise ProviderError(f"ollama call failed: {exc}") from exc


class MockProvider:
    """In-memory provider for tests and dry-runs.

    Records every call so tests can assert on the prompt that was built.
    """

    def __init__(
        self,
        responder: Callable[[list[Event], CornerContext, str], str] | None = None,
    ) -> None:
        self._responder = responder or (
            lambda events, _ctx, _style: f"Mock advice: {next(iter(events)).type}"
        )
        self.calls: list[tuple[list[Event], CornerContext, str]] = []

    def advise(self, events, context, driver_style):
        events_list = list(events)
        self.calls.append((events_list, context, driver_style))
        return self._responder(events_list, context, driver_style)


# ---- Factory ----------------------------------------------------------------


def make_provider(
    name: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
) -> CoachProvider:
    """Build a provider by name. Useful for the CLI."""
    key = (name or "").lower()
    if key == "anthropic":
        if not api_key:
            raise ProviderError("anthropic provider needs ANTHROPIC_API_KEY")
        return AnthropicProvider(api_key=api_key, model=model)
    if key == "openai":
        if not api_key:
            raise ProviderError("openai provider needs OPENAI_API_KEY")
        return OpenAIProvider(api_key=api_key, model=model)
    if key == "gemini":
        if not api_key:
            raise ProviderError("gemini provider needs GEMINI_API_KEY")
        return GeminiProvider(api_key=api_key, model=model)
    if key == "ollama":
        return OllamaProvider(model=model)
    if key == "mock":
        return MockProvider()
    raise ProviderError(f"unknown provider {name!r}")
