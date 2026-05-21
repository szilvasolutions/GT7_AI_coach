"""Provider abstraction: Anthropic, OpenAI, Gemini, Ollama, Mock.

Each provider implements :meth:`CoachProvider.complete`, which takes already-
built system and user prompt strings and returns the model's response. The
advisor (not the provider) builds the prompt, so logging gets a single
verbatim view of what the LLM saw.

SDK imports are deferred to :meth:`__init__` so users only need the
dependency for the provider they actually use.

``complete()`` takes an optional per-call ``max_tokens`` override so that the
session summarizer can request a longer response (3-5 sentences) than the
per-corner advisor (4-12 words).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

# Default per-call output budget. 200 leaves enough headroom even for models
# that emit internal "thinking" tokens before producing visible text (Gemini
# 2.5 family). The advisor's 12-word constraint comes from the system prompt;
# this token cap is just a safety ceiling.
DEFAULT_MAX_TOKENS = 200


class ProviderError(RuntimeError):
    """Wraps any LLM-side failure so callers handle one exception type."""


class CoachProvider(Protocol):
    """Per ARCHITECTURE.md section 7."""

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str: ...


# ---- Concrete implementations ----------------------------------------------


class AnthropicProvider:
    """Default provider. Uses claude-haiku-4-5 for latency (see spec §7)."""

    DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
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

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        try:
            # Cache the stable system prompt so repeated calls stay cheap.
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
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
        max_tokens: int = DEFAULT_MAX_TOKENS,
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

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens if max_tokens is not None else self.max_tokens,
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

    Four non-obvious things we configure:

    * **Default model is ``gemini-2.5-flash-lite``.** Both ``flash`` and
      ``flash-lite`` share the same free-tier limit of 20 requests/day, so
      switching to plain ``flash`` doesn't help when you've already burned
      the day's quota — it just adds latency. ``flash-lite`` is meaningfully
      faster at the prompt sizes we send and is what the legacy V23 script
      used. When you blow through quota, the advisor falls back to canned
      phrases (see ``_FALLBACK_PHRASES`` in advisor.py).
    * **Permissive safety thresholds.** The racing-coach corpus is benign and
      the default filter occasionally blocks helpful answers on racing verbs
      like "attack" / "punish".
    * **Thinking tokens disabled.** Gemini 2.5 family models can emit
      internal reasoning tokens BEFORE the visible answer; that reasoning
      counts against ``max_output_tokens``. With ``thinking_budget=0`` the
      model replies immediately. Lower latency too.
    * **One automatic retry on 5xx / 429.** Gemini occasionally returns
      "503 UNAVAILABLE" or "429 RESOURCE_EXHAUSTED" under load. A single
      ~1 s retry catches the transient case; persistent failures bubble up
      to the canned-phrase fallback in the advisor.
    """

    DEFAULT_MODEL = "gemini-2.5-flash-lite"
    _RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
    _RETRY_DELAY_S = 1.0

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
        self._safety = [
            types.SafetySetting(category=cat, threshold="BLOCK_NONE")
            for cat in (
                "HARM_CATEGORY_HARASSMENT",
                "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                "HARM_CATEGORY_DANGEROUS_CONTENT",
            )
        ]
        # Only Gemini 2.5+ models support thinking_config; for older models
        # the SDK ignores the field, so passing it is safe.
        self._thinking = types.ThinkingConfig(thinking_budget=0)

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        budget = max_tokens if max_tokens is not None else self.max_tokens
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=budget,
            temperature=self.temperature,
            safety_settings=self._safety,
            thinking_config=self._thinking,
        )
        last_exc: Exception | None = None
        for attempt in range(2):  # one retry on transient 5xx / 429
            try:
                resp = self._client.models.generate_content(
                    model=self.model,
                    contents=user,
                    config=config,
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and self._is_retryable(exc):
                    log.warning(
                        "gemini transient error (%s); retrying in %.1fs", exc, self._RETRY_DELAY_S
                    )
                    import time

                    time.sleep(self._RETRY_DELAY_S)
                    continue
                raise ProviderError(f"gemini call failed: {exc}") from exc
        else:  # pragma: no cover — for loop exhausted without break
            raise ProviderError(f"gemini call failed after retries: {last_exc}")

        text = (getattr(resp, "text", None) or "").strip()
        if text:
            return text

        # If the SDK didn't fill resp.text the response was blocked or empty.
        finish_reason = None
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            finish_reason = getattr(candidates[0], "finish_reason", None)
        raise ProviderError(f"gemini returned no text (finish_reason={finish_reason!r})")

    @classmethod
    def _is_retryable(cls, exc: Exception) -> bool:
        """Heuristic: status_code attr or 'XXX' substring in the message."""
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status, int) and status in cls._RETRY_STATUS_CODES:
            return True
        msg = str(exc)
        return any(f"{code}" in msg for code in cls._RETRY_STATUS_CODES)


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

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
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
                    "options": {"num_predict": max_tokens} if max_tokens is not None else {},
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

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        # max_tokens is honoured by real providers; the mock records the value
        # for tests but doesn't otherwise enforce it.
        self.calls.append((system, user))
        _ = max_tokens
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
