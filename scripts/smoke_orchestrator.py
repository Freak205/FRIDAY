"""Orchestrator: explicit plans and the LLM-driven step loop, entirely against
mock tools/providers — deterministic, no real skills, no real Ollama.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.registry import SkillResult  # noqa: E402

MOCK_SPECS = [ToolSpec(name="mock.echo", description="Echo back some text")]

CALLS: list[tuple[str, dict]] = []


async def mock_runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, args))
    if tool == "mock.echo":
        return SkillResult(speech=f"echoed {args.get('text', '')}", data={"echo": args.get("text", "")})
    if tool == "mock.fail":
        return SkillResult(speech="that didn't work", ok=False)
    if tool == "mock.slow":
        await asyncio.sleep(5)
        return SkillResult(speech="finally done")
    raise KeyError(f"unknown mock tool: {tool}")


class ScriptedPlanner(LlmProvider):
    """Replays a fixed sequence of JSON decisions, one per call."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, request: LlmRequest) -> LlmResponse:
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


async def main() -> None:
    overall = True

    print("\n--- run_plan: happy path ---\n")
    CALLS.clear()
    orch = Orchestrator(tools=["mock.echo"], runner=mock_runner, max_steps=5)
    result = await orch.run_plan("say hi twice", [
        PlanStep("mock.echo", {"text": "hi"}),
        PlanStep("mock.echo", {"text": "there"}),
    ])
    ok = result.ok and result.stopped == "completed" and len(result.observations) == 2
    print(f"  {'OK  ' if ok else 'MISS'} two-step plan completes -> {result.summary}")
    overall &= ok

    print("\n--- run_plan: stops at first failure ---\n")
    orch = Orchestrator(tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=5)
    result = await orch.run_plan("do something that fails", [
        PlanStep("mock.echo", {"text": "before"}),
        PlanStep("mock.fail", {}),
        PlanStep("mock.echo", {"text": "never reached"}),
    ])
    ok = (not result.ok) and result.stopped == "failure" and len(result.observations) == 2
    print(f"  {'OK  ' if ok else 'MISS'} failing step halts the plan -> stopped={result.stopped}, steps run={len(result.observations)}")
    overall &= ok

    print("\n--- run_plan: tool not on the allow-list is refused ---\n")
    orch = Orchestrator(tools=["mock.echo"], runner=mock_runner, max_steps=5)
    result = await orch.run_plan("try a tool outside the allow-list", [PlanStep("mock.fail", {})])
    ok = (not result.ok) and result.stopped == "tool_not_allowed"
    print(f"  {'OK  ' if ok else 'MISS'} disallowed tool refused -> stopped={result.stopped}")
    overall &= ok

    print("\n--- run_plan: exceeds the step limit ---\n")
    orch = Orchestrator(tools=["mock.echo"], runner=mock_runner, max_steps=2)
    result = await orch.run_plan("too many steps", [PlanStep("mock.echo", {"text": str(i)}) for i in range(5)])
    ok = (not result.ok) and result.stopped == "step_limit" and len(result.observations) == 0
    print(f"  {'OK  ' if ok else 'MISS'} over-long plan refused up front -> stopped={result.stopped}")
    overall &= ok

    print("\n--- run_plan: per-step timeout ---\n")
    orch = Orchestrator(tools=["mock.slow"], runner=mock_runner, max_steps=2, step_timeout_s=0.2)
    result = await orch.run_plan("a tool that hangs", [PlanStep("mock.slow", {})])
    ok = (not result.ok) and result.stopped == "timeout"
    print(f"  {'OK  ' if ok else 'MISS'} slow tool times out -> stopped={result.stopped}")
    overall &= ok

    print("\n--- run_goal: LLM decides two calls then declares done ---\n")
    CALLS.clear()
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.echo", "args": {"text": "step one"}}',
        '{"action": "call", "tool": "mock.echo", "args": {"text": "step two"}}',
        '{"action": "done", "summary": "Said hi twice, goal complete."}',
    ])
    orch = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=6,
        llm_provider=planner, tool_specs=MOCK_SPECS,
    )
    result = await orch.run_goal("say hi twice via the model")
    ok = result.ok and result.stopped == "completed" and len(result.observations) == 2 and len(CALLS) == 2
    print(f"  {'OK  ' if ok else 'MISS'} LLM-driven loop completes -> {result.summary}")
    overall &= ok

    print("\n--- run_goal: malformed model output fails cleanly ---\n")
    planner = ScriptedPlanner(["not json at all, sorry"])
    orch = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=3,
        llm_provider=planner, tool_specs=MOCK_SPECS,
    )
    result = await orch.run_goal("a goal the model can't plan")
    ok = (not result.ok) and result.stopped == "planning_failed"
    print(f"  {'OK  ' if ok else 'MISS'} malformed JSON handled without crashing -> stopped={result.stopped}")
    overall &= ok

    print("\n--- run_goal: LLM picks a tool outside the allow-list ---\n")
    planner = ScriptedPlanner(['{"action": "call", "tool": "mock.fail", "args": {}}'])
    orch = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=3,
        llm_provider=planner, tool_specs=MOCK_SPECS,
    )
    result = await orch.run_goal("a goal that tries a disallowed tool")
    ok = (not result.ok) and result.stopped == "tool_not_allowed"
    print(f"  {'OK  ' if ok else 'MISS'} disallowed tool from the model refused -> stopped={result.stopped}")
    overall &= ok

    print("\n--- run_goal: repeating the identical call is blocked, then stops the plan ---\n")
    CALLS.clear()
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.echo", "args": {"text": "same"}}',
        '{"action": "call", "tool": "mock.echo", "args": {"text": "same"}}',
        '{"action": "call", "tool": "mock.echo", "args": {"text": "same"}}',
        '{"action": "done", "summary": "should never be reached"}',
    ])
    orch = Orchestrator(
        tools=["mock.echo"], runner=mock_runner, max_steps=8,
        llm_provider=planner, tool_specs=MOCK_SPECS,
    )
    result = await orch.run_goal("a model that loops on the same call")
    ran_once = len(CALLS) == 1
    stopped_ok = (not result.ok) and result.stopped == "repeated_action"
    blocked_obs = [o for o in result.observations if o.error == "repeated_call"]
    ok = ran_once and stopped_ok and len(blocked_obs) == 2
    print(
        f"  {'OK  ' if ok else 'MISS'} tool actually invoked {len(CALLS)}x (not 3x), "
        f"{len(blocked_obs)} blocked-repeat observation(s), stopped={result.stopped}"
    )
    overall &= ok

    print("\n--- run_goal: max_replans=0 (default) still stops at first failure ---\n")
    CALLS.clear()
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.fail", "args": {}}',
        '{"action": "done", "summary": "should never be reached"}',
    ])
    orch = Orchestrator(
        tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=6,
        llm_provider=planner, tool_specs=[*MOCK_SPECS, ToolSpec(name="mock.fail", description="Always fails")],
    )
    result = await orch.run_goal("a goal with a failing step, no replanning enabled")
    ok = (not result.ok) and result.stopped == "failure" and len(result.observations) == 1
    print(f"  {'OK  ' if ok else 'MISS'} default behavior unchanged -> stopped={result.stopped}, "
          f"steps={len(result.observations)}")
    overall &= ok

    print("\n--- run_goal: bounded replanning recovers from a failure and continues (Phase 10) ---\n")
    CALLS.clear()
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.fail", "args": {}}',
        '{"action": "call", "tool": "mock.echo", "args": {"text": "recovered"}}',
        '{"action": "done", "summary": "recovered after one replan"}',
    ])
    orch = Orchestrator(
        tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=6,
        llm_provider=planner, tool_specs=[*MOCK_SPECS, ToolSpec(name="mock.fail", description="Always fails")],
    )
    result = await orch.run_goal("a goal that fails once then recovers", max_replans=1)
    ok = (
        result.ok and result.stopped == "completed"
        and len(result.observations) == 2
        and not result.observations[0].ok and result.observations[1].ok
    )
    print(f"  {'OK  ' if ok else 'MISS'} failure forgiven once, plan continues and completes -> "
          f"stopped={result.stopped}, steps={len(result.observations)}")
    overall &= ok

    print("\n--- run_goal: replanning is itself bounded (repeated failures still stop) ---\n")
    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.fail", "args": {}}',
        '{"action": "call", "tool": "mock.fail", "args": {"attempt": 2}}',
        '{"action": "call", "tool": "mock.fail", "args": {"attempt": 3}}',
    ])
    orch = Orchestrator(
        tools=["mock.echo", "mock.fail"], runner=mock_runner, max_steps=6,
        llm_provider=planner, tool_specs=[*MOCK_SPECS, ToolSpec(name="mock.fail", description="Always fails")],
    )
    result = await orch.run_goal("a goal that keeps failing differently", max_replans=2)
    ok = (not result.ok) and result.stopped == "failure" and len(result.observations) == 3
    print(f"  {'OK  ' if ok else 'MISS'} replan budget (2) exhausted, third failure stops the plan -> "
          f"stopped={result.stopped}, steps={len(result.observations)}")
    overall &= ok

    print("\n--- run_goal: a permission denial is never replanned, regardless of max_replans ---\n")

    async def denying_runner(tool: str, args: dict, actor: str) -> SkillResult:
        from friday.permissions import PermissionError_

        if tool == "mock.denied":
            raise PermissionError_("policy denies mock.denied")
        return await mock_runner(tool, args, actor)

    planner = ScriptedPlanner([
        '{"action": "call", "tool": "mock.denied", "args": {}}',
        '{"action": "call", "tool": "mock.echo", "args": {"text": "should not run"}}',
    ])
    orch = Orchestrator(
        tools=["mock.echo", "mock.denied"], runner=denying_runner, max_steps=6,
        llm_provider=planner,
        tool_specs=[*MOCK_SPECS, ToolSpec(name="mock.denied", description="Always denied")],
    )
    result = await orch.run_goal("a goal that tries a denied tool", max_replans=3)
    ok = (
        not result.ok and result.stopped == "failure"
        and len(result.observations) == 1
        and result.observations[0].error == "PermissionError_"
    )
    print(f"  {'OK  ' if ok else 'MISS'} permission denial stops immediately, never replanned -> "
          f"stopped={result.stopped}, steps={len(result.observations)}, "
          f"error={result.observations[0].error if result.observations else None}")
    overall &= ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
