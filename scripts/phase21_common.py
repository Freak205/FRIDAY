"""Shared scaffolding for the Phase 21.0 smoke suites (goal coverage, pagination,
context budget, scope expansion): the check/scenario counters, a scripted planner
that plugs into the existing `llm.get_provider` seam, and the throwaway-state /
hard-deny helpers. Nothing here talks to Ollama or runs a real side effect."""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from friday import llm  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
SCENARIOS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), label))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}")
    return bool(cond)


def scenario(title: str) -> None:
    SCENARIOS.append(title)
    print(f"\n--- {title} ---\n")


class ScriptedPlanner(LlmProvider):
    """A model that replays canned replies (the last one repeats). Records every request."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        i = self.calls
        self.calls += 1
        self.requests.append(request)
        return LlmResponse(text=self.replies[min(i, len(self.replies) - 1)], model="scripted", provider=self.name)

    def system_of(self, i: int) -> str:
        return next(m.content for m in self.requests[i].messages if m.role == "system")

    def prompt_of(self, i: int) -> str:
        return [m for m in self.requests[i].messages if m.role == "user"][-1].content


@contextlib.contextmanager
def scripted_provider(provider: LlmProvider):
    original = llm.get_provider
    llm.get_provider = lambda name=None: provider
    try:
        yield
    finally:
        llm.get_provider = original


def call(tool: str, args: dict | None = None) -> str:
    return json.dumps({"action": "call", "tool": tool, "args": args or {}})


def done(summary: str = "ok") -> str:
    return json.dumps({"action": "done", "summary": summary})


def lock_down_real_tools(REGISTRY) -> dict:
    """Process-wide: every real non-L0 skill (except plan.run itself and test.* fixtures)
    is hard-denied by a per-tool policy override, so nothing real can act."""
    saved = dict(CFG.permissions.overrides)
    denied = {
        s.name: "deny" for s in REGISTRY.all()
        if s.tier != "L0" and s.name != "plan.run" and not s.name.startswith("test.")
    }
    CFG.permissions.overrides = {**saved, **denied}
    return saved


def finish(title: str, elapsed: float, *, min_assertions: int, min_scenarios: int) -> int:
    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"{title}  ({elapsed:.1f}s)")
    print("=" * 72)
    print(f"  assertions : {passed}/{len(RESULTS)} passed")
    print(f"  scenarios  : {len(SCENARIOS)}")
    gate = len(RESULTS) >= min_assertions and len(SCENARIOS) >= min_scenarios
    print(f"  gate >={min_assertions} assertions / >={min_scenarios} scenarios: {'OK' if gate else 'MISS'}")
    if failed:
        print("\n  FAILED:")
        for label in failed:
            print(f"    - {label}")
        print("\nFAILURES ABOVE")
        return 1
    print("\nALL OK" if gate else "\nGATES MISSED")
    return 0 if gate else 1
