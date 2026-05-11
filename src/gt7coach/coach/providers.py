"""Provider abstraction: Anthropic, OpenAI, Gemini, Ollama, Mock.

Each provider implements :meth:`CoachProvider.complete`, which takes already-
built system and user prompt strings and returns the model's response. The
advisor (not the provider) builds the prompt, so logging gets a single
verbatim view of what the LLM saw.

SDK imports are deferred to :meth:`__init__` so users only need the
dependency for the provider they actually use.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Wraps any LLM-side failure so callers handle one exception type."""


class CoachProvider(Protocol):
    """Per ARCHITECTURE.md section 7."""

    def complete(self, system: str, user: str) -> str: ...


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

    def complete(self, system: str, user: str) -> str:
        try:
            # Cache the stable system prompt so repeated calls stay cheap.
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
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

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            raise ProviderError(f"openai call failed: {exc}") from exc


class GeminiProvider:
    """Google Gemini via the new ``google-genai`` SDK.

    Safety thresholds are set permissive because the racing-coach corpus is
    benign and the default filter occasionally blocks helpful answers on
    racing verbs like "attack" / "punish".
    """

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
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError(
                "google-genai SDK not installed. pip install 'gt7coach[gemini]'"
            ) from exc
        self._client = genai.Client(api_key=api_key)
        self._types = types
        self.model = model or self.DEFAULT_MODEL
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Build the safety-settings list once; the SDK accepts strings here.
        self._safety = [
            types.SafetySetting(category=cat, threshold="BLOCK_NONE")
            for cat in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ]

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self._client.models.generate_content(
                model=self.model,
                contents=user,
                config=self._types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=self.max_tokens,
                    temperature=self.temperature,
                    safety_settings=self._safety,
                ),
            )
        except Exception as exc:
            raise ProviderError(f"gemini call failed: {exc}") from exc

        text = (getattr(resp, "text", None) or "").strip()
        if text:
            return text

        # If the SDK didn't fill resp.text the response was blocked or empty.
        finish_reason = None
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        raise ProviderError(f"gemini returned no text (finish_reason={finish_reason!r})")


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

    def complete(self, system: str, user: str) -> str:
        import requests

        try:
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
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
        responder: Callable[[str, str], str] | None = None,
    ) -> None:
        self._responder = responder or (lambda _sys, user: f"Mock advice for: {user[:32]}")
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self._responder(system, user)


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
