"""Live-model validation of `plan.run` — Phase 5, Part 2.

Unlike every other smoke test, this one talks to a *real* Ollama instance
with a *real* pulled model. It is NOT part of the deterministic regression
suite (scripts/regression.py, scripts/smoke_plan.py already cover plan.run
against a scripted fake LLM) — this script exists to observe and record how
an actual small local model behaves: tool selection, step count, observation
quality, retries, failures.

If Ollama isn't reachable or the configured model isn't pulled, every case
prints SKIP rather than failing, exactly like smoke_llm.py's live-call case.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import knowledge, llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402


async def _run_plan(goal: str):
    """Call plan.run directly through the real EXECUTOR — guarantees the
    request actually takes the plan.run -> orchestrator -> LLM path being
    validated here, rather than depending on the L2 embedding matcher
    routing a particular phrasing to plan.run vs. directly to a single skill.
    """
    from friday.permissions import EXECUTOR

    return await EXECUTOR.run("plan.run", {"goal": goal}, actor="text")


async def _approve(skill, args, preview: str) -> bool:
    print(f"       [confirm] {preview} -> approved")
    return True


async def _check_live() -> bool:
    try:
        await llm.complete("Say OK.", model=CFG.llm.model)
        return True
    except llm.LlmError as exc:
        print(f"  SKIP all live tests: {exc}")
        return False


def _report(label: str, goal: str, result) -> None:
    print(f"\n=== {label} ===")
    print(f"  goal: {goal}")
    print(f"  stopped: {result.data.get('stopped')}  ok: {result.ok}")
    steps = result.data.get("steps", [])
    print(f"  steps taken: {len(steps)}")
    for i, s in enumerate(steps, 1):
        print(f"    {i}. {s['tool']}({s['args']}) -> {'ok' if s['ok'] else 'FAILED'}: {s['speech'][:120]}")
    print(f"  final: {result.speech[:300]}")


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    from friday.permissions import EXECUTOR

    EXECUTOR.set_confirm_handler(_approve)

    if not await _check_live():
        sys.exit(0)

    print(f"\nUsing model: {CFG.llm.model}\n")

    # --- Test 1: project inspection -----------------------------------------
    t0 = time.perf_counter()
    result = await _run_plan("Inspect the FRIDAY project and tell me its current state.")
    _report("Test 1: inspect FRIDAY project", "inspect FRIDAY project", result)
    print(f"  elapsed: {time.perf_counter() - t0:.1f}s")

    # --- Test 2: open the FRIDAY project in VS Code -------------------------
    t0 = time.perf_counter()
    result = await _run_plan("Open the FRIDAY project in VS Code.")
    _report("Test 2: open FRIDAY in VS Code", "open FRIDAY in VS Code", result)
    print(f"  elapsed: {time.perf_counter() - t0:.1f}s")

    # --- Test 3: open a local test webpage and read it ----------------------
    with tempfile.TemporaryDirectory(prefix="friday-webtest-") as tmp:
        page = Path(tmp) / "test.html"
        page.write_text(
            "<html><head><title>Friday Live Test Page</title></head>"
            "<body><h1>Hello Friday</h1><p>This is a local test page for Phase 5 validation.</p>"
            "</body></html>",
            encoding="utf-8",
        )
        url = page.as_uri()

        t0 = time.perf_counter()
        result = await _run_plan(f"Open {url} and tell me what it says.")
        _report("Test 3: open local webpage and read it", f"open {url}", result)
        print(f"  elapsed: {time.perf_counter() - t0:.1f}s")

    # --- Test 4: read indexed knowledge and answer a question ---------------
    with tempfile.TemporaryDirectory(prefix="friday-knowtest-") as tmp:
        doc = Path(tmp) / "team_note.md"
        doc.write_text(
            "# Team Note\n\nThe FRIDAY project's lead maintainer is Ivsai. "
            "The project has no other contributors yet.\n",
            encoding="utf-8",
        )
        knowledge.forget_document(str(Path(tmp)))
        from friday.permissions import EXECUTOR
        index_result = await EXECUTOR.run("knowledge.index", {"path": str(doc)}, actor="text")
        print(f"\n(setup) indexed test doc -> {index_result.speech}")

        t0 = time.perf_counter()
        result = await _run_plan("Read my indexed knowledge and tell me who the FRIDAY project's lead maintainer is.")
        _report("Test 4: answer from indexed knowledge", "who is the lead maintainer", result)
        print(f"  elapsed: {time.perf_counter() - t0:.1f}s")
        knowledge.forget_document(str(Path(tmp)))

    print("\n(done — this is an observational script, not a pass/fail gate; read the output above)")


if __name__ == "__main__":
    asyncio.run(main())
