"""plan.run: the bridge from a natural-language goal to `Orchestrator.run_goal`,
driven end to end through the real skill registry, brain, and EXECUTOR.

Entirely deterministic — a scripted fake LLM provider stands in for Ollama
throughout, exactly like `scripts/smoke_orchestrator.py`. No test here depends
on Ollama being installed; a separate section notes the real-Ollama path.
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import browser, llm  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.brain.engine import Action  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

_PAGE_HTML = """
<html><head><title>FRIDAY Plan Test Page</title></head>
<body><h1>Plan Run Says Hello</h1><p>This page exists only for plan.run to read.</p></body></html>
""".strip()


class ScriptedPlanner(LlmProvider):
    """Replays fixed JSON decisions, one per call, and records every prompt sent."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


class FailingProvider(LlmProvider):
    """Stands in for Ollama not being installed/running."""

    name = "failing"

    async def complete(self, request: LlmRequest) -> LlmResponse:
        raise llm.ProviderUnavailable("Ollama isn't running (test double).")


@contextlib.contextmanager
def scripted_provider(provider: LlmProvider):
    """Make `friday.llm.get_provider()` hand back `provider` for the duration."""
    original = llm.get_provider
    llm.get_provider = lambda name=None: provider
    try:
        yield
    finally:
        llm.get_provider = original


def call(tool: str, args: dict | None = None) -> str:
    return json.dumps({"action": "call", "tool": tool, "args": args or {}})


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


def no_subgoals() -> str:
    """A decomposition reply meaning 'doesn't split into more than one
    subgoal' — see friday.orchestrator.Orchestrator.decompose_goal. Prepend
    this to a ScriptedPlanner's replies for any goal text containing a
    connector word (looks_decomposable) so the one extra bounded call
    Phase 11.2 adds before run_goal starts doesn't eat into the replies the
    test scripted for run_goal's own decisions."""
    return json.dumps({"subgoals": []})


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    print("\n--- A: registration ---\n")
    plan_skill = REGISTRY.get("plan.run")
    ok = plan_skill is not None and plan_skill.tier == "L1" and any(
        p.name == "goal" and p.required for p in plan_skill.params
    )
    print(f"  {'OK  ' if ok else 'MISS'} plan.run registered, tier={getattr(plan_skill, 'tier', None)}, "
          f"params={[p.name for p in plan_skill.params] if plan_skill else None}")
    overall &= ok

    print("\n--- B: intent matching + whole-utterance goal extraction ---\n")
    MATCH_CASES = [
        "just handle this whole thing for me, figure out the steps yourself",
        "work through this task on your own and get it done",
        "put together a plan and carry it out for me",
    ]
    match_ok = 0
    for utterance in MATCH_CASES:
        u = BRAIN.understand(utterance)
        # normalize() strips filler words/punctuation before extraction, so the
        # goal won't be byte-identical to the utterance — just non-empty and
        # substantially it.
        good = u.action == Action.ACT and u.skill == "plan.run" and len(u.args.get("goal", "")) > 20
        match_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance:58} -> {u.skill} ({u.score:.2f}) goal={u.args.get('goal')!r}")
    overall &= match_ok == len(MATCH_CASES)
    print(f"\n  {match_ok}/{len(MATCH_CASES)} correct")

    print("\n--- C/D/E: scripted planner drives one real tool through the real EXECUTOR ---\n")
    planner = ScriptedPlanner([call("meta.capabilities"), done("Reported FRIDAY's capabilities.")])
    # Phase 21.0 fixture note: "tell me what skills you have" authorizes nothing but looking, and
    # meta.capabilities answers it, so the new evidence-driven stop ends the run WITHOUT a second
    # planner call — there is then no "next planning prompt" to inspect. What this scenario proves
    # (the executed step's observation is fed to the following planning prompt) is unchanged and
    # asserted exactly as before; only the Phase 21 stop is switched off here so the second turn
    # exists. Its own behaviour is pinned by scripts/smoke_goal_coverage.py section D.
    original_goal_coverage = CFG.planner.goal_coverage
    CFG.planner.goal_coverage = False
    try:
        with scripted_provider(planner):
            result = await EXECUTOR.run("plan.run", {"goal": "tell me what skills you have"}, actor="test")
    finally:
        CFG.planner.goal_coverage = original_goal_coverage
    ok = (
        result.ok and result.data.get("stopped") == "completed"
        and len(result.data.get("steps", [])) == 1
        and result.data["steps"][0]["tool"] == "meta.capabilities"
        and result.data["steps"][0]["ok"]
    )
    print(f"  {'OK  ' if ok else 'MISS'} real meta.capabilities call executed through EXECUTOR -> {result.speech[:90]}")
    overall &= ok

    first_speech = result.data["steps"][0]["speech"] if result.data.get("steps") else ""
    ok = (
        len(planner.prompts) == 2
        and "meta.capabilities" in planner.prompts[1]
        and first_speech[:20] in planner.prompts[1]
    )
    print(f"  {'OK  ' if ok else 'MISS'} the first step's observation appears in the next planning prompt")
    overall &= ok

    print("\n--- F: malformed LLM output fails cleanly ---\n")
    planner = ScriptedPlanner(["not json at all, sorry"])
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "a goal the model can't plan"}, actor="test")
    ok = (not result.ok) and result.data.get("stopped") == "planning_failed" \
        and result.speech.startswith("Local planning is unavailable")
    print(f"  {'OK  ' if ok else 'MISS'} malformed JSON handled -> {result.speech}")
    overall &= ok

    print("\n--- F2: real progress before a LATER malformed decision is reported, not hidden ---\n")
    # Phase 15.0: found on the real machine against the real qwen2.5:3b model
    # — a goal can genuinely execute one or more real steps before a LATER
    # decision (often the final "done") comes back malformed, and the old
    # code discarded that real progress entirely: same generic "Local
    # planning is unavailable. The plan came back malformed." regardless of
    # what had already happened, and no `steps` in `data` at all. Scenario F
    # above (immediate malformed output, zero real steps) is unaffected —
    # this covers the partial-progress case it didn't.
    planner = ScriptedPlanner([call("meta.capabilities"), "not json at all, sorry"])
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "a goal that stalls partway through"}, actor="test")
    ok = (
        not result.ok and result.data.get("stopped") == "planning_failed"
        and len(result.data.get("steps", [])) == 1
        and result.data["steps"][0]["tool"] == "meta.capabilities"
        and result.data["steps"][0]["ok"]
        and "stalled" in result.speech.lower()
        and not result.speech.startswith("Local planning is unavailable")
    )
    print(f"  {'OK  ' if ok else 'MISS'} real partial progress surfaced, not discarded -> {result.speech[:100]}")
    overall &= ok

    print("\n--- G: LLM provider unavailable gives an actionable message ---\n")
    with scripted_provider(FailingProvider()):
        result = await EXECUTOR.run("plan.run", {"goal": "do something"}, actor="test")
    ok = (not result.ok) and result.data.get("stopped") == "planning_failed" \
        and "unavailable" in result.speech.lower() and "ollama" in result.speech.lower()
    print(f"  {'OK  ' if ok else 'MISS'} provider-down handled without crashing -> {result.speech}")
    overall &= ok

    print("\n--- H: max-step limit halts a plan that never says done ---\n")
    original_max_steps = CFG.planner.max_steps
    CFG.planner.max_steps = 2
    planner = ScriptedPlanner([call("meta.capabilities")])  # always the same call, never "done"
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "loop forever"}, actor="test")
    ok = (not result.ok) and result.data.get("stopped") == "step_limit" \
        and len(result.data.get("steps", [])) == 2
    print(f"  {'OK  ' if ok else 'MISS'} step limit enforced -> stopped={result.data.get('stopped')}, "
          f"steps run={len(result.data.get('steps', []))}")
    overall &= ok
    CFG.planner.max_steps = original_max_steps

    print("\n--- I: a single failed tool call halts the plan when replanning is off "
          "(no repeated-failure loop is possible) ---\n")
    # Phase 16.0 raised CFG.planner.max_replans's default from 0 to 2 (see
    # PLAN.md Phase 16.0 / scripts/smoke_long_horizon.py scenario I) — this
    # test's own guarantee is specifically about the max_replans=0 case (the
    # original Phase 10 "stop dead on the very first failure" contract),
    # so it now pins that explicitly rather than relying on whatever the
    # module default happens to be. The max_replans>0 case (a failure IS
    # forgiven, up to a bound, and can never loop unboundedly) is covered by
    # scripts/smoke_long_horizon.py scenarios G/H/I.
    original_max_replans = CFG.planner.max_replans
    CFG.planner.max_replans = 0
    planner = ScriptedPlanner([
        call("project.inspect", {"name": "a-project-that-definitely-does-not-exist-anywhere-xyz"}),
        call("meta.capabilities"),  # must never be reached
    ])
    with scripted_provider(planner):
        result = await EXECUTOR.run(
            "plan.run", {"goal": "inspect a project that doesn't exist"}, actor="test"
        )
    CFG.planner.max_replans = original_max_replans
    ok = (
        not result.ok and result.data.get("stopped") == "failure"
        and len(result.data.get("steps", [])) == 1 and planner.calls == 1
    )
    print(f"  {'OK  ' if ok else 'MISS'} first failure stops planning before a second step is even requested "
          f"-> stopped={result.data.get('stopped')}, planner calls={planner.calls}")
    overall &= ok

    print("\n--- J: permission/confirmation enforcement survives plan.run ---\n")

    async def always_decline(skill, args, preview) -> bool:
        return False

    EXECUTOR.set_confirm_handler(always_decline)
    planner = ScriptedPlanner([call("knowledge.forget", {"query": "nonexistent-doc"})])
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "forget a document"}, actor="test")
    ok = (not result.ok) and result.data.get("stopped") == "failure" \
        and result.data["steps"][0]["error"] != "PermissionError_"  # declined, not denied outright
    print(f"  {'OK  ' if ok else 'MISS'} an L2 tool declined at confirmation stops the plan cleanly "
          f"-> stopped={result.data.get('stopped')}")
    overall &= ok

    async def always_confirm(skill, args, preview) -> bool:
        return True

    EXECUTOR.set_confirm_handler(always_confirm)
    planner = ScriptedPlanner([call("knowledge.forget", {"query": "nonexistent-doc"})])
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "forget a document"}, actor="scheduler")
    ok = (
        not result.ok and result.data.get("stopped") == "failure"
        and result.data["steps"][0]["error"] == "PermissionError_"
    )
    print(f"  {'OK  ' if ok else 'MISS'} an unattended actor stays capped by the tier ceiling *inside* "
          f"plan.run, even though a confirm handler would have said yes -> "
          f"error={result.data['steps'][0].get('error') if result.data.get('steps') else None}")
    overall &= ok

    EXECUTOR.set_confirm_handler(SESSION._confirm)  # restore the real channel

    print("\n--- K: end-to-end use case 1 — inspect the FRIDAY project ---\n")
    repo_root = Path(__file__).resolve().parent.parent
    planner = ScriptedPlanner([
        no_subgoals(),  # this goal's "and" makes it decomposable (Phase 11.2)
        call("project.inspect", {"name": str(repo_root)}),
        done("FRIDAY is a Python project; the inspected step reported its git and stack state."),
    ])
    with scripted_provider(planner):
        result = await EXECUTOR.run(
            "plan.run", {"goal": "inspect the FRIDAY project and tell me its current development state"},
            actor="test",
        )
    ok = (
        result.ok and result.data.get("stopped") == "completed"
        and len(result.data.get("steps", [])) == 1
        and result.data["steps"][0]["tool"] == "project.inspect"
        and result.data["steps"][0]["ok"]
    )
    print(f"  {'OK  ' if ok else 'MISS'} plan.run -> project.inspect -> final answer -> {result.speech[:100]}")
    overall &= ok

    print("\n--- L: end-to-end use case 2 — open a local page, read it, report back ---\n")
    CFG.browser.headless = True
    tmp_profile = tempfile.mkdtemp(prefix="friday-plan-test-")
    CFG.browser.profile_dir = tmp_profile
    data_url = "data:text/html," + quote(_PAGE_HTML)
    try:
        planner = ScriptedPlanner([
            no_subgoals(),  # this goal's "and"/"then" makes it decomposable (Phase 11.2)
            call("browser.open", {"url": data_url}),
            call("browser.read", {}),
            done("The page says: Plan Run Says Hello."),
        ])
        with scripted_provider(planner):
            result = await EXECUTOR.run(
                "plan.run", {"goal": "open this local test page and read it, then tell me what it says"},
                actor="test",
            )
        ok = (
            result.ok and result.data.get("stopped") == "completed"
            and [s["tool"] for s in result.data.get("steps", [])] == ["browser.open", "browser.read"]
            and all(s["ok"] for s in result.data["steps"])
        )
        print(f"  {'OK  ' if ok else 'MISS'} plan.run -> browser.open -> browser.read -> final answer -> {result.speech[:100]}")
        overall &= ok
    finally:
        await browser.close()
        shutil.rmtree(tmp_profile, ignore_errors=True)

    print("\n--- planner disabled via config ---\n")
    CFG.planner.enabled = False
    result = await EXECUTOR.run("plan.run", {"goal": "anything"}, actor="test")
    ok = (not result.ok) and "disabled" in result.speech.lower()
    print(f"  {'OK  ' if ok else 'MISS'} disabled planner refuses cleanly -> {result.speech}")
    overall &= ok
    CFG.planner.enabled = True

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
