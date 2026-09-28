"""Live-model validation of Phase 18.0's evidence-sufficiency gate.

Unlike `scripts/smoke_evidence_reasoning.py` (deterministic, scripted LLM),
this talks to a *real* Ollama instance with the real, configured local model
(`config.yaml`'s `llm.model`, `qwen2.5:3b` in this project) — same
precedent as `scripts/smoke_open_ended_live.py`, which this file mirrors
structurally and extends.

For each scenario, this runs the SAME goal against the SAME real model
twice:

  - "gate ON"  — normal behavior (friday.intelligence.discovery.
    assess_sufficiency active, the Phase 18.0 change).
  - "gate OFF" — discovery.assess_sufficiency monkeypatched to always return
    INSUFFICIENT, i.e. the pre-Phase-18.0 behavior: the *only* stop signal
    is the model itself choosing {"action": "done"}.

...and reports discovery steps, model calls, and elapsed time side by side.
This single script satisfies both this phase's live local-LLM validation
(does qwen2.5:3b's own behavior, combined with the deterministic gate,
avoid unnecessary calls?) and its before/after performance benchmark —
avoiding a third script for what is really one measurement taken twice.

If Ollama isn't reachable or the configured model isn't pulled, every case
prints SKIP rather than failing, exactly like `smoke_open_ended_live.py`.

Safety: every scenario's confirmation handler DECLINES (never approves) —
this script must never actually delete, overwrite, publish, send, or
install anything on the real machine.
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
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402


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


def _register_contradiction_skills() -> None:
    """Two tiny, harmless, deterministic L0 skills that always report
    conflicting state — so the real model's *handling* of a genuine
    contradiction is observable and reproducible, rather than hoping real
    desktop/network state happens to conflict on this machine."""

    @skill(name="test.erl_service_start", tier="L0", description="check whether the demo service reports started")
    def _start() -> SkillResult:
        return SkillResult(speech="The demo service reports it started successfully.")

    @skill(name="test.erl_service_ping", tier="L0", description="ping the demo service")
    def _ping() -> SkillResult:
        return SkillResult(speech="Ping to the demo service was refused — connection refused.")


async def _run_once(goal: str, *, gate_enabled: bool) -> tuple[object, float, int]:
    from friday.permissions import EXECUTOR

    original_assess = discovery.assess_sufficiency
    if not gate_enabled:
        discovery.assess_sufficiency = lambda goal, observations: discovery.Sufficiency.INSUFFICIENT  # noqa: E731

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
        discovery.assess_sufficiency = original_assess
    elapsed = time.perf_counter() - t0
    return result, elapsed, calls["n"]


def _steps_of(result) -> list[dict]:
    return result.data.get("steps") or result.data.get("evidence") or []


async def _run_scenario(label: str, goal: str) -> None:
    print(f"\n=== {label} ===")
    print(f"  goal: {goal}")
    print(f"  classify_mode: {discovery.classify_mode(goal).value}")

    on_result, on_elapsed, on_calls = await _run_once(goal, gate_enabled=True)
    off_result, off_elapsed, off_calls = await _run_once(goal, gate_enabled=False)

    on_steps, off_steps = len(_steps_of(on_result)), len(_steps_of(off_result))
    print(f"  {'':14}{'gate ON (Phase 18.0)':<28}{'gate OFF (pre-Phase-18.0)':<28}")
    print(f"  {'stopped':14}{str(on_result.data.get('stopped')):<28}{str(off_result.data.get('stopped')):<28}")
    print(f"  {'status':14}{str(on_result.data.get('status', '(main-loop)')):<28}{str(off_result.data.get('status', '(main-loop)')):<28}")
    print(f"  {'steps':14}{on_steps:<28}{off_steps:<28}")
    print(f"  {'model calls':14}{on_calls:<28}{off_calls:<28}")
    print(f"  {'elapsed (s)':14}{on_elapsed:<28.1f}{off_elapsed:<28.1f}")
    delta = off_calls - on_calls
    if delta > 0:
        print(f"  -> gate saved {delta} model call(s) this run")
    elif delta < 0:
        print(f"  -> gate used {-delta} MORE model call(s) this run (model didn't need it, or genuinely kept investigating)")
    else:
        print("  -> no difference this run (the model itself stopped just as early either way)")
    print(f"  gate-ON final speech: {on_result.speech[:220]}")


async def main() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    _register_contradiction_skills()
    from friday.permissions import EXECUTOR

    EXECUTOR.set_confirm_handler(_decline)

    if not await _check_live():
        sys.exit(0)

    print(f"\nUsing model: {CFG.llm.model}\n")
    print("Each scenario runs TWICE against the real model: once with the Phase 18.0")
    print("evidence-sufficiency gate active, once with it forced off (simulating")
    print("pre-Phase-18.0 behavior) — same goal, same model, so the difference is")
    print("attributable to the gate alone.")

    # 1 & 2 — diagnostic.
    await _run_scenario(
        "1 (diagnostic): real FRIDAY project, likely-failing checks",
        "Why might my FRIDAY project's automated checks be failing?",
    )
    await _run_scenario(
        "2 (diagnostic): a specific, narrower question",
        "Why might the FRIDAY orchestrator's discovery pass fail to stop early?",
    )

    # 3 & 4 — investigative / open-ended.
    await _run_scenario(
        "3 (investigative): what needs attention",
        "Take a look at my FRIDAY project and tell me what needs attention.",
    )
    await _run_scenario(
        "4 (open-ended): may fall through to the main loop; every confirmation is "
        "declined (see _decline above), so nothing real is ever mutated",
        "Make my FRIDAY project better.",
    )

    # 5 — information seeking.
    await _run_scenario(
        "5 (information-seeking): real README lookup",
        "Find the README in my FRIDAY project and tell me what the project is supposed to do.",
    )

    # 6 — contradiction / uncertainty, forced deterministic via the two mock
    #     skills above so the real model's handling is observable at all.
    await _run_scenario(
        "6 (contradiction/uncertainty): a deliberately conflicting demo service",
        "Use test.erl_service_start and test.erl_service_ping to figure out whether "
        "the demo service is actually reachable, and explain your reasoning.",
    )

    print(
        "\n(done — this is an observational script, not a pass/fail gate; read the "
        "output above. Pay particular attention to whether qwen2.5:3b itself would "
        "have recognized sufficiency on its own (gate OFF column) vs. whether the "
        "deterministic gate caught it regardless (gate ON column).)"
    )


if __name__ == "__main__":
    asyncio.run(main())
