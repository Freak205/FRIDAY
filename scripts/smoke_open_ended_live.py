"""Live-model validation of Phase 17.0 open-ended goal handling.

Unlike `scripts/smoke_open_ended.py` (deterministic, scripted LLM), this
talks to a *real* Ollama instance with the real, configured local model
(`config.yaml`'s `llm.model`, `qwen2.5:3b` in this project) — same
precedent as `scripts/smoke_plan_live.py`, which this file mirrors
structurally. Not part of the deterministic regression suite; it exists to
observe and record how the actual small local model behaves for
diagnostic/investigative/information-seeking/open-ended goals: mode
classification, discovery steps, actions, questions, final evidence, final
status, and latency.

If Ollama isn't reachable or the configured model isn't pulled, every case
prints SKIP rather than failing, exactly like `smoke_plan_live.py`.

Safety: every scenario's confirmation handler DECLINES (never approves) —
this script must never actually delete, overwrite, publish, send, or
install anything on the real machine. A scenario that reaches a mutating
step still completes (declined, not blocked-forever) so the resulting
speech/evidence can be observed and recorded.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402


async def _decline(skill, args, preview: str) -> bool:
    print(f"       [confirm] {preview} -> DECLINED (this script never approves a mutation)")
    return False


async def _check_live() -> bool:
    try:
        await llm.complete("Say OK.", model=CFG.llm.model)
        return True
    except llm.LlmError as exc:
        print(f"  SKIP all live tests: {exc}")
        return False


def _report(label: str, goal: str, result, elapsed_s: float, model_calls: int) -> None:
    print(f"\n=== {label} ===")
    print(f"  goal: {goal}")
    print(f"  classify_mode: {discovery.classify_mode(goal).value}")
    print(f"  stopped: {result.data.get('stopped')}  ok: {result.ok}  status: {result.data.get('status', '(main-loop finish)')}")
    steps = result.data.get("steps") or []
    evidence = result.data.get("evidence") or []
    n_steps = len(steps) or len(evidence)
    print(f"  discovery/execution steps: {n_steps}")
    for i, s in enumerate(evidence or steps, 1):
        tool = s.get("tool", "?")
        ok = s.get("ok")
        label_or_speech = s.get("label", s.get("speech", ""))
        print(f"    {i}. {tool} -> {'ok' if ok else 'FAILED'}: {str(label_or_speech)[:120]}")
    unknowns = result.data.get("unknowns") or []
    if unknowns:
        print(f"  unknowns: {unknowns}")
    if result.data.get("awaiting_clarification"):
        print(f"  ASKED: {result.speech}")
    print(f"  final speech: {result.speech[:300]}")
    print(f"  model calls: {model_calls}  elapsed: {elapsed_s:.1f}s")


async def _run_scenario(label: str, goal: str) -> None:
    from friday.permissions import EXECUTOR

    calls = {"n": 0}
    original_complete = llm.complete

    async def _counting_complete(*args, **kwargs):
        calls["n"] += 1
        return await original_complete(*args, **kwargs)

    llm.complete = _counting_complete
    t0 = time.perf_counter()
    try:
        result = await EXECUTOR.run("plan.run", {"goal": goal}, actor="text")
    finally:
        llm.complete = original_complete
    elapsed = time.perf_counter() - t0
    _report(label, goal, result, elapsed, calls["n"])


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    from friday.permissions import EXECUTOR

    EXECUTOR.set_confirm_handler(_decline)

    if not await _check_live():
        sys.exit(0)

    print(f"\nUsing model: {CFG.llm.model}\n")

    # 1. Diagnostic — read-only, never falls through to a mutating main loop.
    await _run_scenario(
        "1: diagnostic on the real FRIDAY project",
        "Why might my FRIDAY project's automated checks be failing?",
    )

    # 2. Investigative — explicit evidence from the real repo only.
    await _run_scenario(
        "2: investigative — what needs attention",
        "Take a look at my FRIDAY project and tell me what needs attention.",
    )

    # 3. Information seeking — real desktop observation, no action.
    await _run_scenario(
        "3: information seeking — what's on screen",
        "What's happening on my screen right now?",
    )

    # 4. Investigative/information-seeking — real README lookup.
    await _run_scenario(
        "4: README lookup",
        "Find the README in my FRIDAY project and tell me what the project is supposed to do.",
    )

    # 5. Open-ended — may fall through to the main loop; every confirmation
    #    is declined (see _decline above), so nothing real is ever mutated.
    await _run_scenario(
        "5: open-ended — make it better",
        "Make my FRIDAY project better.",
    )

    print("\n(done — this is an observational script, not a pass/fail gate; read the output above)")


if __name__ == "__main__":
    asyncio.run(main())
