"""Phase 18.0 — evidence-driven investigation & efficient reasoning.

Entirely deterministic — every LLM call in this file goes through a scripted
`LlmProvider`, exactly like `scripts/smoke_open_ended.py` (which this file
mirrors structurally; several scenarios below directly extend patterns
established there — correction, clarification, safety boundary, experience
retrieval). No real Ollama; every discovery tool call is either a harmless
`test.er_*` mock skill registered below (tier="L0") or, for the safety
boundary scenario, a real but never-actually-invoked `apps.open`.

Nothing here is a second planner/orchestrator/evaluator: every scenario
drives the real `friday.skills.plan.run`, the real
`friday.orchestrator.Orchestrator.run_goal` (discovery_mode=True, same
method main execution uses), the real
`friday.intelligence.discovery.assess_sufficiency` (the new Phase 18.0
gate), and the real `friday.intelligence.evaluator`/`goals` persistence.

Section 0 is direct unit checks against `discovery`'s new pure gate helpers
(`is_relevant`, `has_contradiction`, `_current_evidence`, `_is_conclusive`,
`assess_sufficiency`) — cheap, precise, and exercises the exact five brief
scenarios (Cases A-E) this phase was chartered to fix. Sections 1-13 are
full scripted-planner scenarios through the real pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import context_memory  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.intelligence import episodes  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.goals import GoalMode, GoalStatus  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

# -- shared scaffolding, same conventions as smoke_open_ended.py ------------

CHECKS: list[tuple[str, bool]] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    CHECKS.append((label, cond))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}")
    return bool(cond)


class ScriptedPlanner(LlmProvider):
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []
        # friday.llm.complete always puts system (when given) at messages[0]
        # and the user prompt last — captured separately so a scenario can
        # assert on the Phase 18.0 evidence_hint text, which orchestrator.py
        # appends to `system`, never to the user prompt `self.prompts` holds.
        self.system_prompts: list[str] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.prompts.append(request.messages[-1].content)
        system_msg = request.messages[0] if request.messages and request.messages[0].role == "system" else None
        self.system_prompts.append(system_msg.content if system_msg else "")
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return LlmResponse(text=reply, model="scripted", provider=self.name)


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


def done(summary: str) -> str:
    return json.dumps({"action": "done", "summary": summary})


def reset_between_scenarios() -> None:
    INTEL.reset()
    context_memory.CONTEXT.reset()
    EXECUTOR.set_confirm_handler(SESSION._confirm)  # noqa: SLF001
    SESSION.pending = None


def obs(tool: str, args: dict | None, ok: bool, speech: str, error: str = "") -> Observation:
    return Observation(PlanStep(tool=tool, args=args or {}), ok, speech, error=error)


def _register_mock_skills() -> None:
    """Harmless, deterministic L0 stand-ins for discovery-phase tool calls —
    same precedent as `scripts/smoke_open_ended.py`'s `_register_mock_skills`
    (different tool names, `test.er_*`, to avoid colliding with that file if
    both are ever imported in the same process)."""

    @skill(name="test.er_next_task", tier="L0", description="mock probe (an explicit next task)")
    def _next_task() -> SkillResult:
        return SkillResult(speech="NEXT: Implement the evidence-sufficiency gate.")

    @skill(name="test.er_module_error", tier="L0", description="mock probe (a strong diagnostic signal)")
    def _module_error() -> SkillResult:
        # ok=True: the probe itself ran fine — what it *found* is the bad
        # news, same convention as smoke_open_ended.py's test.oe_probe_diag.
        # ok=False here would make Orchestrator.run_goal's own pre-existing
        # replan-or-stop logic (max_replans=0 for discovery) end the whole
        # pass immediately, before the Phase 18.0 gate ever runs.
        return SkillResult(ok=True, speech="ModuleNotFoundError: flask")

    @skill(name="test.er_weak_signal", tier="L0", description="mock probe (an inconclusive failure)")
    def _weak_signal() -> SkillResult:
        return SkillResult(ok=True, speech="Command exited with code 1.")

    @skill(name="test.er_weak_signal_b", tier="L0", description="mock probe (a second inconclusive read)")
    def _weak_signal_b() -> SkillResult:
        return SkillResult(ok=True, speech="Still exited non-zero on retry.")

    @skill(name="test.er_server_start", tier="L0", description="mock probe (server start report)")
    def _server_start() -> SkillResult:
        return SkillResult(speech="Server started successfully.")

    @skill(name="test.er_net_check", tier="L0", description="mock probe (connectivity check)")
    def _net_check(host: str = "") -> SkillResult:
        return SkillResult(speech="Connection refused.")

    @skill(name="test.er_neutral", tier="L0", description="mock probe (unrelated but successful)")
    def _neutral() -> SkillResult:
        return SkillResult(speech="Checked configuration; formatting looks standard.")

    @skill(name="test.er_cancel_then_report", tier="L0", description="mock probe that also requests cancellation")
    def _cancel_then_report() -> SkillResult:
        INTEL.request_cancel()
        return SkillResult(speech="Started looking into it.")

    @skill(name="test.er_readme_a", tier="L0", description="mock probe (file A content)")
    def _readme_a() -> SkillResult:
        return SkillResult(speech="This project is FRIDAY, a local voice assistant.")

    @skill(name="test.er_readme_b", tier="L0", description="mock probe (file B, duplicate content)")
    def _readme_b() -> SkillResult:
        return SkillResult(speech="This project is FRIDAY, a local voice assistant.")


async def main() -> None:
    # Isolated from the real, shared friday.db — same reasoning as
    # scripts/smoke_open_ended.py's use of store.use_temp_db().
    db_cm = store.use_temp_db()
    db_cm.__enter__()
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    _register_mock_skills()
    reset_between_scenarios()
    overall = True

    # ========================================================================
    # Section 0a — is_relevant: permissive by default, explicit off-topic
    #   phrasing excluded, a strong signal is always relevant.
    # ========================================================================
    print("\n--- 0a: is_relevant ---\n")
    o_wallpaper = obs("desktop.wallpaper", {}, True, "The current wallpaper is a mountain scene.")
    overall &= check(
        "an explicit off-topic observation is never relevant",
        not discovery.is_relevant("why isn't the app starting", o_wallpaper),
    )
    o_error = obs("shell.run", {}, False, "", error="ModuleNotFoundError: flask")
    overall &= check(
        "a strong error signal is relevant regardless of literal wording overlap",
        discovery.is_relevant("why won't my script run", o_error),
    )
    o_empty = obs("noop.probe", {}, True, "")
    overall &= check("an empty observation is never relevant", not discovery.is_relevant("anything", o_empty))
    o_unrelated_ok = obs("dir.list", {}, True, "Found 12 files in the directory.")
    overall &= check(
        "a successful but topically unrelated read is not relevant to a specific diagnostic goal",
        not discovery.is_relevant("why isn't the app starting", o_unrelated_ok),
    )

    # ========================================================================
    # Section 0b — has_contradiction: paired positive/negative state markers.
    # ========================================================================
    print("\n--- 0b: has_contradiction ---\n")
    pos = obs("server.start", {}, True, "Server started successfully.")
    neg = obs("net.check", {"host": "localhost"}, True, "Connection refused.")
    overall &= check("conflicting state observations are flagged contradictory", discovery.has_contradiction([pos, neg]))
    overall &= check("a single observation is never self-contradictory", not discovery.has_contradiction([pos]))
    neutral = obs("dir.list", {}, True, "Found 12 files.")
    overall &= check("unrelated evidence with no state markers isn't flagged", not discovery.has_contradiction([neutral]))

    # ========================================================================
    # Section 0c — _current_evidence: staleness, duplicate collapsing, and
    #   the repeat-guard's own warning observations are never evidence.
    # ========================================================================
    print("\n--- 0c: _current_evidence (staleness / duplicates / repeat-guard exclusion) ---\n")
    page_a = obs("chrome.current_page", {}, True, "You are on page A.")
    navigate = obs("chrome.navigate", {"url": "http://example.com/b"}, True, "Navigated to page B.")
    page_b = obs("chrome.current_page", {}, True, "You are on page B.")
    current = discovery._current_evidence([page_a, navigate, page_b])  # noqa: SLF001
    pages = [o.speech for o in current if o.step.tool == "chrome.current_page"]
    overall &= check("a later re-observation of the same tool+args supersedes the stale one", pages == ["You are on page B."])
    overall &= check("an unrelated intervening call stays independent", any(o.step.tool == "chrome.navigate" for o in current))

    read_a = obs("file.read", {"path": "a.txt"}, True, "Contents of A.")
    read_b = obs("file.read", {"path": "b.txt"}, True, "Contents of B.")
    diff_args = discovery._current_evidence([read_a, read_b])  # noqa: SLF001
    overall &= check("same tool, different args, stays independent (not treated as stale)", len(diff_args) == 2)

    dup_a = obs("file.read", {"path": "README.md"}, True, "This is FRIDAY, an AI assistant.")
    dup_b = obs("file.read", {"path": "README2.md"}, True, "This is FRIDAY, an AI assistant.")
    deduped = discovery._current_evidence([dup_a, dup_b])  # noqa: SLF001
    overall &= check("identical content from two different calls collapses to one", len(deduped) == 1)

    blocked = obs("file.read", {"path": "x"}, False, "Blocked: already called", error="repeated_call")
    overall &= check(
        "the repeat-guard's own warning observation is never current evidence",
        discovery._current_evidence([blocked]) == [],  # noqa: SLF001
    )

    # ========================================================================
    # Section 0d — assess_sufficiency: the five brief scenarios (Cases A-E)
    #   plus mode-aware conclusiveness (diagnostic needs more than "it ran
    #   fine and said something relevant").
    # ========================================================================
    print("\n--- 0d: assess_sufficiency (Cases A-E + mode-aware conclusiveness) ---\n")
    case_a = obs("file.read", {"path": "PLAN.md"}, True, "NEXT: Implement X.")
    overall &= check(
        "Case A: an explicit next-task answer is SUFFICIENT",
        discovery.assess_sufficiency("What's the next thing I should work on in my project?", [case_a])
        is discovery.Sufficiency.SUFFICIENT,
    )
    case_b = obs("shell.run", {"cmd": "python app.py"}, False, "", error="ModuleNotFoundError: flask")
    overall &= check(
        "Case B: a directly observed failure is SUFFICIENT for a diagnostic goal",
        discovery.assess_sufficiency("Figure out why the app isn't starting.", [case_b])
        is discovery.Sufficiency.SUFFICIENT,
    )
    case_c = obs("shell.run", {"cmd": "python app.py"}, False, "Application exited with code 1.")
    overall &= check(
        "Case C: a bare exit code names no cause -> not SUFFICIENT",
        discovery.assess_sufficiency("Figure out why the app isn't starting.", [case_c])
        is not discovery.Sufficiency.SUFFICIENT,
    )
    overall &= check(
        "Case C's insufficient evidence never regresses to CONTRADICTORY either",
        discovery.assess_sufficiency("Figure out why the app isn't starting.", [case_c])
        in (discovery.Sufficiency.INSUFFICIENT, discovery.Sufficiency.UNCERTAIN),
    )
    case_d = [pos, neg]
    overall &= check(
        "Case D: conflicting evidence is CONTRADICTORY, never a confident guess",
        discovery.assess_sufficiency("Why isn't the app reachable?", case_d) is discovery.Sufficiency.CONTRADICTORY,
    )
    case_e = obs("desktop.wallpaper", {}, True, "The current wallpaper is a mountain scene.")
    overall &= check(
        "Case E: purely off-topic evidence is INSUFFICIENT",
        discovery.assess_sufficiency("Why isn't the app starting?", [case_e]) is discovery.Sufficiency.INSUFFICIENT,
    )
    overall &= check(
        "Sufficiency has exactly the 4 states the brief calls for — no extra state machine",
        {s.value for s in discovery.Sufficiency} == {"sufficient", "insufficient", "contradictory", "uncertain"},
    )
    overall &= check("no observations at all is INSUFFICIENT, not a crash", discovery.assess_sufficiency("anything", []) is discovery.Sufficiency.INSUFFICIENT)

    diag_context = obs("test.er_probe", {}, True, "This appears to be a Flask app (found app.py, requirements.txt with Flask).")
    overall &= check(
        "a relevant but merely-contextual read does NOT satisfy a diagnostic goal",
        discovery.assess_sufficiency("why isn't my flask app starting", [diag_context]) is not discovery.Sufficiency.SUFFICIENT,
    )
    invest_answer = obs("test.er_todo", {}, True, "TODO.md lists: add rate limiting, add audit logging.")
    overall &= check(
        "the SAME shape of relevant, successful read DOES satisfy an investigative goal",
        discovery.assess_sufficiency(
            "find out what needs attention in my todo work", [invest_answer]
        ) is discovery.Sufficiency.SUFFICIENT,
    )

    # ========================================================================
    # 0e — _is_conclusive directly, by GoalMode: the diagnostic bar is
    #   strictly higher than every other mode's.
    # ========================================================================
    print("\n--- 0e: _is_conclusive is mode-aware ---\n")
    contextual = obs("test.er_probe", {}, True, "This appears to be a Flask app.")
    overall &= check(
        "DIAGNOSTIC: a merely contextual, non-causal finding is not conclusive",
        not discovery._is_conclusive(GoalMode.DIAGNOSTIC, contextual),  # noqa: SLF001
    )
    overall &= check(
        "INFORMATION_SEEKING: the identical finding IS conclusive for a non-diagnostic goal",
        discovery._is_conclusive(GoalMode.INFORMATION_SEEKING, contextual),  # noqa: SLF001
    )
    overall &= check(
        "INVESTIGATIVE: the identical finding IS conclusive for a non-diagnostic goal",
        discovery._is_conclusive(GoalMode.INVESTIGATIVE, contextual),  # noqa: SLF001
    )
    causal = obs("test.er_probe", {}, True, "The service failed to start because port 5000 is already in use.")
    overall &= check(
        "DIAGNOSTIC: explicit causal wording IS conclusive even without an Error/Exception token",
        discovery._is_conclusive(GoalMode.DIAGNOSTIC, causal),  # noqa: SLF001
    )
    strong = obs("test.er_probe", {}, False, "", error="PermissionError: access denied")
    overall &= check(
        "DIAGNOSTIC: a strong error signal is conclusive even when ok=False",
        discovery._is_conclusive(GoalMode.DIAGNOSTIC, strong),  # noqa: SLF001
    )
    unsuccessful_and_vague = obs("test.er_probe", {}, False, "Something went wrong.")
    overall &= check(
        "no mode treats a vague, unsuccessful, non-causal observation as conclusive",
        not any(
            discovery._is_conclusive(m, unsuccessful_and_vague)  # noqa: SLF001
            for m in (GoalMode.DIAGNOSTIC, GoalMode.INVESTIGATIVE, GoalMode.INFORMATION_SEEKING, GoalMode.OPEN_ENDED)
        ),
    )

    # ========================================================================
    # 0f — classify_mode stays correct through _run_discovery's wrapped
    #   "Investigate before acting: {goal}..." instructional prompt (the
    #   actual `goal` string assess_sufficiency sees at runtime — see
    #   friday.skills.plan._run_discovery) — search-based, not anchored.
    # ========================================================================
    print("\n--- 0f: classify_mode is robust to the discovery-pass prompt wrapper ---\n")
    wrapped = (
        "Investigate before acting: why isn't my flask app starting. Gather evidence only "
        "— do not attempt to fix or change anything yet. Once you have enough evidence, "
        "respond done with a summary of what you found and what remains unknown."
    )
    overall &= check(
        "the wrapped discovery prompt still classifies as DIAGNOSTIC",
        discovery.classify_mode(wrapped) is GoalMode.DIAGNOSTIC,
    )
    overall &= check(
        "the wrapper's own vocabulary ('investigate', 'evidence', 'found', ...) never spuriously "
        "overlaps with an unrelated tool name",
        not discovery.is_relevant(wrapped, obs("test.oe_investigate", {}, True, "Some unrelated finding text.")),
    )

    # ========================================================================
    # 1 — direct action skips discovery entirely (no Phase 18.0 gate code
    #     path is even reachable — it's guarded by discovery_mode=True, and
    #     DIRECT_ACTION goals never enter a discovery pass at all).
    # ========================================================================
    print("\n--- 1: direct action skips discovery ---\n")
    overall &= check(
        "'turn the volume up' classifies as DIRECT_ACTION",
        discovery.classify_mode("turn the volume up") is GoalMode.DIRECT_ACTION,
    )
    reset_between_scenarios()
    planner1 = ScriptedPlanner([done("Done.")])
    with scripted_provider(planner1):
        result1 = await EXECUTOR.run("plan.run", {"goal": "turn the volume up"}, actor="test")
    overall &= check(
        "a direct-action goal needed only the main loop's own decision — no separate discovery call",
        planner1.calls == 1,
    )
    overall &= check("result reports ok", result1.ok)

    # ========================================================================
    # 2 — answer immediately available: one relevant, successful read fully
    #     answers an investigative goal (brief §11, Case A shape) — the gate
    #     stops after 1 call instead of a needless second one.
    # ========================================================================
    print("\n--- 2: answer immediately available ---\n")
    reset_between_scenarios()
    planner2 = ScriptedPlanner([
        call("test.er_next_task"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner2):
        result2 = await EXECUTOR.run(
            "plan.run", {"goal": "what should I work on next in my project"}, actor="test"
        )
    overall &= check("Phase 18.0: an already-observable answer stops discovery after 1 call", planner2.calls == 1)
    overall &= check("result reports ok", result2.ok)
    overall &= check("status settles as succeeded", result2.data.get("status") == "succeeded")
    overall &= check(
        "the reported speech is grounded in the real observation, not invented",
        result2.speech == "NEXT: Implement the evidence-sufficiency gate.",
    )

    # ========================================================================
    # 3 — diagnostic immediately answerable: a directly observed failure
    #     (ModuleNotFoundError) is conclusive on its own.
    # ========================================================================
    print("\n--- 3: diagnostic immediately answerable ---\n")
    reset_between_scenarios()
    planner3 = ScriptedPlanner([
        call("test.er_module_error"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner3):
        result3 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
    overall &= check("Phase 18.0: a directly observed failure stops discovery after 1 call", planner3.calls == 1)
    overall &= check("result reports ok (a concrete diagnosis is a successful diagnostic outcome)", result3.ok)
    overall &= check("ModuleNotFoundError is reported honestly, not paraphrased away", "ModuleNotFoundError" in result3.speech)

    # ========================================================================
    # 4 — insufficient evidence continues: a bare, cause-less failure does
    #     NOT stop discovery; a later, genuinely conclusive one does.
    # ========================================================================
    print("\n--- 4: insufficient evidence continues, then resolves ---\n")
    reset_between_scenarios()
    planner4 = ScriptedPlanner([
        call("test.er_weak_signal"),
        call("test.er_module_error"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner4):
        result4 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
    overall &= check(
        "a bare exit code alone did not stop discovery (evidence stayed insufficient)",
        planner4.calls >= 2,
    )
    overall &= check(
        "discovery stopped as soon as real conclusive evidence appeared, not a call later",
        planner4.calls == 2,
    )
    overall &= check("the final report reflects the real conclusive finding", "ModuleNotFoundError" in result4.speech)

    # ========================================================================
    # 5 — contradictory evidence: conflicting observations never produce a
    #     confident guess; after a bounded resolution attempt, stops honestly.
    # ========================================================================
    print("\n--- 5: contradictory evidence ---\n")
    reset_between_scenarios()
    planner5 = ScriptedPlanner([
        call("test.er_server_start"),
        call("test.er_net_check", {"host": "localhost"}),
        call("test.er_net_check", {"host": "127.0.0.1"}),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner5):
        result5 = await EXECUTOR.run("plan.run", {"goal": "why can't I connect to my server"}, actor="test")
    overall &= check(
        "the model got a bounded chance to resolve the conflict (3 real calls, not fewer)",
        planner5.calls == 3,
    )
    overall &= check(
        "an unresolved conflict stops deterministically rather than exhausting the whole budget",
        result5.data.get("stopped") == "evidence_exhausted",
    )
    overall &= check(
        "the report names the conflict explicitly, transparently showing both sides — not silently picking one",
        "conflicts with itself" in result5.speech
        and "started successfully" in result5.speech and "Connection refused" in result5.speech,
    )
    overall &= check(
        "before the conflict is even visible (1 observation), the model gets no conflict hint",
        len(planner5.system_prompts) >= 2 and "conflicts with itself" not in planner5.system_prompts[1],
    )
    overall &= check(
        "once a genuine conflict exists, the next planning call is nudged to resolve it",
        len(planner5.system_prompts) >= 3 and "conflicts with itself" in planner5.system_prompts[2],
    )
    overall &= check(
        "the report never states either side as a settled, confident conclusion",
        "is running" not in result5.speech and "is not running" not in result5.speech,
    )
    overall &= check(
        "real progress was still made, so status reflects partial, not a clean success",
        result5.data.get("status") in ("partial", "failed"),
    )

    # ========================================================================
    # 6 — discovery budget exhaustion: never fabricates an answer just
    #     because the step budget ran out.
    # ========================================================================
    print("\n--- 6: discovery budget exhaustion ---\n")
    reset_between_scenarios()
    original_max_discovery = CFG.planner.max_discovery_steps
    CFG.planner.max_discovery_steps = 2
    try:
        planner6 = ScriptedPlanner([
            call("test.er_weak_signal"),
            call("test.er_weak_signal_b"),
        ])
        with scripted_provider(planner6):
            result6 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
        overall &= check("both budgeted steps ran (the gate never artificially shortened the budget)", planner6.calls == 2)
        overall &= check("discovery stopped at the step limit, never fabricating a cause", result6.data.get("stopped") == "step_limit")
        overall &= check("the report never invents a root cause it never observed", "ModuleNotFoundError" not in result6.speech)
        overall &= check(
            "real (if inconclusive) progress still settles as partial, not a silent failure",
            result6.data.get("status") == "partial",
        )
    finally:
        CFG.planner.max_discovery_steps = original_max_discovery

    # ========================================================================
    # 7 — cancellation mid-discovery still takes priority over the gate —
    #     the existing cancel_check poll runs before the sufficiency gate,
    #     unaffected by it either way.
    # ========================================================================
    print("\n--- 7: cancellation mid-discovery ---\n")
    reset_between_scenarios()
    planner7 = ScriptedPlanner([
        call("test.er_cancel_then_report"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner7):
        result7 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
    overall &= check("cancellation requested mid-discovery stops the run", result7.data.get("stopped") == "cancelled")
    overall &= check("cancellation pre-empts the gate — no further decision call was made", planner7.calls == 1)

    # ========================================================================
    # 8 — safety boundary: the gate deciding evidence is sufficient/
    #     insufficient never touches tool allow-listing — an L1 tool request
    #     during discovery is still refused structurally.
    # ========================================================================
    print("\n--- 8: safety boundary unaffected by the gate ---\n")
    reset_between_scenarios()
    planner8 = ScriptedPlanner([call("apps.open", {"name": "chrome"})])
    with scripted_provider(planner8):
        result8 = await EXECUTOR.run("plan.run", {"goal": "why isn't chrome opening"}, actor="test")
    overall &= check("an L1 tool request during discovery is still refused, not executed", result8.data.get("stopped") == "tool_not_allowed")
    overall &= check("the refusal is reported honestly", "isn't an available tool" in result8.speech)

    # ========================================================================
    # 9 — experience-block text is never treated as current evidence: a past
    #     episode naming the real root cause doesn't let the gate skip a
    #     real observation this run.
    # ========================================================================
    print("\n--- 9: experience vs. current evidence ---\n")
    reset_between_scenarios()
    episodes.record(
        "why won't my script run",
        goal_id=None, context="",
        steps=[Observation(PlanStep(tool="test.er_module_error"), False, "", error="ModuleNotFoundError: flask")],
        stopped="completed", ok=True, duration_ms=100,
    )
    planner9 = ScriptedPlanner([
        call("test.er_neutral"),
        done("Checked configuration; nothing conclusive yet."),
    ])
    with scripted_provider(planner9):
        result9 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
    prompt9 = planner9.prompts[0] if planner9.prompts else ""
    overall &= check(
        "the past episode reached the prompt as guidance",
        "RELEVANT PAST EXPERIENCE" in prompt9 and "test.er_module_error" in prompt9,
    )
    overall &= check(
        "but the gate still required a real, current observation before stopping (2 real calls)",
        planner9.calls == 2,
    )

    # ========================================================================
    # 10 — context-memory text is never treated as current evidence: it can
    #     resolve *what* the goal refers to, but never *answers* it.
    # ========================================================================
    print("\n--- 10: context memory vs. current evidence ---\n")
    reset_between_scenarios()
    context_memory.CONTEXT.remember("project", "FRIDAY", turn_id="seed-er-10")
    planner10 = ScriptedPlanner([
        call("test.er_neutral"),
        done("Checked configuration; nothing conclusive yet."),
    ])
    with scripted_provider(planner10):
        result10 = await EXECUTOR.run("plan.run", {"goal": "why won't my script run"}, actor="test")
    overall &= check(
        "remembered context entities don't short-circuit the gate — a real observation was still required",
        planner10.calls == 2,
    )
    context_memory.CONTEXT.reset()

    # ========================================================================
    # 11 — user correction still supersedes an in-flight discovery-mode goal,
    #     unaffected by the new gate.
    # ========================================================================
    print("\n--- 11: user correction still supersedes ---\n")
    reset_between_scenarios()
    planner11 = ScriptedPlanner([call("test.er_weak_signal")])
    with scripted_provider(planner11):
        result11 = await EXECUTOR.run("plan.run", {"goal": "why isn't my printer working"}, actor="test")
    goal_id_11 = result11.data.get("goal_id")
    before_count = len(goals_mod.recent(limit=50))
    correction_result = await SESSION.handle("no, I meant the scanner instead", actor="text")
    after_count = len(goals_mod.recent(limit=50))
    overall &= check("a correction after a discovery-mode goal doesn't raise", correction_result is not None)
    overall &= check("the correction never creates a duplicate/second Goal row", after_count == before_count)
    overall &= check("the original goal's objective is untouched by the correction", goals_mod.get(goal_id_11).objective == "why isn't my printer working")

    # ========================================================================
    # 12 — clarification still resumes the same goal; the resumed run can
    #     itself be short-circuited by the gate once real evidence exists.
    # ========================================================================
    print("\n--- 12: clarification continuation, gate applies after resuming ---\n")
    reset_between_scenarios()
    planner12 = ScriptedPlanner([
        json.dumps({"action": "ask", "question": "Which script — the daemon or the GUI?"}),
        call("test.er_module_error"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner12):
        result12a = await SESSION._run(  # noqa: SLF001
            "plan.run", {"goal": "why won't my script run"}, actor="text"
        )
        goal_id_12 = result12a.data.get("goal_id")
        overall &= check("plan.run pauses and asks instead of guessing", bool(result12a.data.get("awaiting_clarification")))
        overall &= check("the goal is marked BLOCKED, not FAILED", goals_mod.get(goal_id_12).status == GoalStatus.BLOCKED)

        result12b = await SESSION.handle("the daemon", actor="text")
    overall &= check("resuming clears the pending clarification", SESSION.pending is None)
    overall &= check("the SAME goal_id is resumed, never a second goal", result12b.data.get("goal_id") == goal_id_12)
    overall &= check(
        "Phase 18.0: the resumed run's own conclusive evidence still stops it early (3 total calls: ask + call + none)",
        planner12.calls == 2,
    )

    # ========================================================================
    # 13 — duplicate-content evidence from two different tool calls doesn't
    #     falsely inflate confidence beyond what one relevant read already
    #     established.
    # ========================================================================
    print("\n--- 13: duplicate evidence doesn't inflate confidence ---\n")
    reset_between_scenarios()
    planner13 = ScriptedPlanner([
        call("test.er_readme_a"),
        call("test.er_readme_b"),
        done("(should never be consumed)"),
    ])
    with scripted_provider(planner13):
        result13 = await EXECUTOR.run("plan.run", {"goal": "find out what this project is about"}, actor="test")
    overall &= check(
        "the first relevant, substantive read alone was already conclusive (1 call, not 2)",
        planner13.calls == 1,
    )
    overall &= check("result reports ok", result13.ok)
    overall &= check(
        "test.er_readme_b was never actually invoked (only 1 evidence step recorded)",
        len(result13.data.get("steps") or []) == 1
        and result13.data["steps"][0]["tool"] == "test.er_readme_a",
    )

    # ========================================================================
    print(f"\n{'=' * 60}")
    passed = sum(1 for _, c in CHECKS if c)
    print(f"SCORE: {passed}/{len(CHECKS)} deterministic assertions passed")
    if overall and passed == len(CHECKS):
        print("ALL CHECKS PASSED")
    else:
        print("SOME CHECKS FAILED")
    db_cm.__exit__(None, None, None)
    sys.exit(0 if (overall and passed == len(CHECKS)) else 1)


if __name__ == "__main__":
    asyncio.run(main())
