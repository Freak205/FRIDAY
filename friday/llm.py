"""Local LLM abstraction — a provider-agnostic escape hatch, not a vendor lock-in.

Deliberately small: a request/response shape plus one provider (Ollama, local,
free, no API key). Nothing else in FRIDAY should import `httpx` and talk to
Ollama directly — go through `complete()` so a future second provider (or a
smarter routing rule) only has to be added here.

This module never assumes a model is installed. Every failure mode — Ollama
not running, model not pulled, request timeout — comes back as a typed
`LlmError` with an actionable message, never a raw exception from the network
stack, so callers (friday.orchestrator, skills) can degrade cleanly instead of
crashing when no local model is set up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(slots=True)
class LlmMessage:
    role: Role
    content: str


@dataclass(slots=True)
class LlmRequest:
    messages: list[LlmMessage]
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    # Phase 19.0: optional provider-native structured-output constraint —
    # "json" or a JSON Schema dict (Ollama's `format`). None (every caller
    # before this phase, and the default now) sends nothing extra.
    response_format: dict | str | None = None
    # Phase 21.0: the context window (tokens) to ask the provider for. Ollama
    # otherwise loads whatever its own default is (4096 on this machine) and
    # SILENTLY drops the head of an over-long prompt. None/0 sends nothing.
    num_ctx: int | None = None


@dataclass(slots=True)
class LlmResponse:
    text: str
    model: str
    provider: str
    raw: dict = field(default_factory=dict)


class LlmError(Exception):
    """Base class for LLM provider failures."""


class ProviderUnavailable(LlmError):
    """The provider's service isn't reachable (not running, wrong URL, ...)."""


class ModelUnavailable(LlmError):
    """The provider is reachable but doesn't have the requested model."""


class LlmProvider:
    """Interface every provider implements. Not meant to be instantiated directly."""

    name = "base"

    async def complete(self, request: LlmRequest) -> LlmResponse:
        raise NotImplementedError


class OllamaProvider(LlmProvider):
    """Talks to a local Ollama daemon over its REST API. No API key, no network egress."""

    name = "ollama"

    def __init__(self, base_url: str, timeout_s: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    async def _client(self):
        import httpx

        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout_s)

    async def complete(self, request: LlmRequest) -> LlmResponse:
        import httpx

        if not request.model:
            raise ModelUnavailable(
                "No model was specified. Pull one with `ollama pull <model>` "
                "and pass its name, or set `llm.model` in config.yaml."
            )

        payload = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": False,
            "options": {},
        }
        if request.temperature is not None:
            payload["options"]["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["options"]["num_predict"] = request.max_tokens
        if request.num_ctx:
            payload["options"]["num_ctx"] = int(request.num_ctx)
        if request.response_format is not None:
            payload["format"] = request.response_format

        try:
            async with await self._client() as client:
                response = await client.post("/api/chat", json=payload)
                if response.status_code in (400, 422) and "format" in payload:
                    # Phase 19.0: a server/model that can't honor a schema `format`
                    # (older Ollama accepted only "json") rejects the request. Retry
                    # ONCE without it rather than failing the whole planning step —
                    # structured output is an optimization, never a requirement.
                    payload.pop("format")
                    response = await client.post("/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise ProviderUnavailable(
                "Ollama isn't running (or isn't reachable at "
                f"{self.base_url}). Install it from https://ollama.com/download "
                "and run `ollama serve`."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                f"Ollama didn't respond within {self.timeout_s:.0f}s."
            ) from exc

        if response.status_code == 404:
            raise ModelUnavailable(
                f"Model '{request.model}' isn't pulled. Run: ollama pull {request.model}"
            )
        if response.status_code != 200:
            raise LlmError(f"Ollama returned {response.status_code}: {response.text[:200]}")

        data = response.json()
        text = (data.get("message") or {}).get("content", "")
        return LlmResponse(text=text, model=request.model, provider=self.name, raw=data)


async def ping(*, timeout_s: float = 1.5) -> bool:
    """Cheap reachability check — hits Ollama's model-list endpoint rather
    than running a real completion, so callers (e.g. the desktop GUI's
    telemetry panel) can show a genuine BRAIN/OLLAMA status without loading
    a model or paying inference latency every poll.
    """
    import httpx

    from friday.config import CFG

    if CFG.llm.provider != "ollama":
        return False
    try:
        async with httpx.AsyncClient(base_url=CFG.llm.base_url, timeout=timeout_s) as client:
            response = await client.get("/api/tags")
        return response.status_code == 200
    except Exception:
        return False


_PROVIDERS = {"ollama": OllamaProvider}


def get_provider(name: str | None = None) -> LlmProvider:
    """Instantiate the configured provider. Raises LlmError for an unknown name."""
    from friday.config import CFG

    provider_name = name or CFG.llm.provider
    cls = _PROVIDERS.get(provider_name)
    if cls is None:
        raise LlmError(
            f"Unknown LLM provider '{provider_name}'. Available: {', '.join(_PROVIDERS)}"
        )
    return cls(base_url=CFG.llm.base_url, timeout_s=CFG.llm.timeout_s)


async def complete(
    prompt: str,
    *,
    system: str = "",
    model: str = "",
    temperature: float | None = None,
    provider: LlmProvider | None = None,
    response_format: dict | str | None = None,
    num_ctx: int | None = None,
) -> LlmResponse:
    """Convenience entry point: a plain prompt in, an LlmResponse out.

    `num_ctx` defaults to the one central `CFG.llm.num_ctx`: every caller shares
    a single window size on purpose — Ollama reloads the model whenever a request
    asks for a different one, so a per-caller value would thrash a 4 GB GPU."""
    from friday.config import CFG

    messages = []
    if system:
        messages.append(LlmMessage(role="system", content=system))
    messages.append(LlmMessage(role="user", content=prompt))

    request = LlmRequest(
        messages=messages,
        model=model or CFG.llm.model,
        temperature=temperature if temperature is not None else CFG.llm.temperature,
        response_format=response_format,
        num_ctx=(num_ctx if num_ctx is not None else CFG.llm.num_ctx) or None,
    )
    active = provider or get_provider()
    return await active.complete(request)
