"""LLM abstraction: request/response shape, a fake provider, and Ollama's
failure modes — all without requiring Ollama to actually be installed.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm  # noqa: E402


class FakeProvider(llm.LlmProvider):
    """Deterministic stand-in so this test never touches the network."""

    name = "fake"

    def __init__(self, reply: str = "42") -> None:
        self.reply = reply
        self.last_request: llm.LlmRequest | None = None

    async def complete(self, request: llm.LlmRequest) -> llm.LlmResponse:
        self.last_request = request
        return llm.LlmResponse(text=self.reply, model=request.model or "fake-model", provider=self.name)


async def main() -> None:
    overall = True

    print("\n--- fake provider round trip ---\n")
    fake = FakeProvider(reply="the answer is 42")
    response = await llm.complete("what is the answer", system="be terse", provider=fake)
    ok = response.text == "the answer is 42" and fake.last_request is not None
    ok = ok and fake.last_request.messages[0].role == "system"
    ok = ok and fake.last_request.messages[-1].content == "what is the answer"
    print(f"  {'OK  ' if ok else 'MISS'} complete() round-trips through an injected provider")
    overall &= ok

    print("\n--- ollama: no model specified ---\n")
    provider = llm.get_provider("ollama")
    is_ollama = isinstance(provider, llm.OllamaProvider)
    print(f"  {'OK  ' if is_ollama else 'MISS'} get_provider('ollama') returns OllamaProvider")
    overall &= is_ollama

    try:
        await provider.complete(llm.LlmRequest(messages=[llm.LlmMessage(role="user", content="hi")], model=""))
        print("  FAIL expected ModelUnavailable, none raised")
        overall = False
    except llm.ModelUnavailable as exc:
        print(f"  OK   blank model rejected cleanly before any network call: {exc}")
    except Exception as exc:
        print(f"  FAIL wrong exception type: {type(exc).__name__}: {exc}")
        overall = False

    print("\n--- ollama: service unreachable ---\n")
    # Deliberately not the configured port — nothing should be listening here
    # whether or not Ollama is installed, so this exercises the connection-
    # refused path deterministically.
    unreachable = llm.OllamaProvider(base_url="http://127.0.0.1:1", timeout_s=3.0)
    try:
        await unreachable.complete(
            llm.LlmRequest(messages=[llm.LlmMessage(role="user", content="hi")], model="llama3")
        )
        print("  WARN expected ProviderUnavailable, none raised (something IS listening on :1?)")
    except llm.ProviderUnavailable as exc:
        clean = "ollama" in str(exc).lower() and ("install" in str(exc).lower() or "serve" in str(exc).lower())
        print(f"  {'OK  ' if clean else 'MISS'} unreachable service fails with an actionable message:\n       {exc}")
        overall &= clean
    except Exception as exc:
        print(f"  FAIL wrong exception type: {type(exc).__name__}: {exc}")
        overall = False

    print("\n--- unknown provider name ---\n")
    try:
        llm.get_provider("gpt-mega-3000")
        print("  FAIL expected LlmError, none raised")
        overall = False
    except llm.LlmError as exc:
        print(f"  OK   unknown provider rejected cleanly: {exc}")

    print("\n--- real ollama, if actually installed and running ---\n")
    import httpx

    from friday.config import CFG

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{CFG.llm.base_url}/api/tags")
        reachable = resp.status_code == 200
    except Exception:
        reachable = False

    if not reachable:
        print(f"  SKIP Ollama isn't running at {CFG.llm.base_url} — install/start it to exercise this live")
    else:
        models = resp.json().get("models", [])
        if not models:
            print("  SKIP Ollama is running but no model is pulled (`ollama pull <model>`)")
        else:
            model = models[0]["name"]
            real = llm.get_provider("ollama")
            result = await real.complete(
                llm.LlmRequest(messages=[llm.LlmMessage(role="user", content="Say OK and nothing else.")], model=model)
            )
            print(f"  OK   live call to {model} -> {result.text[:80]!r}")

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
