"""Phase 10 — the intelligence core: goals, working memory, episodes,
evaluator, corrections, self-state, and the training-data exporter.

Entirely deterministic — no Ollama required except where a `ScriptedPlanner`
stands in for it (same pattern as scripts/smoke_plan.py). Uses the real
SQLite store, the real brain/registry/executor, exactly like the other
smoke_*.py scripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from friday import llm, store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import corrections, episodes, evaluator, experience  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence import working_memory  # noqa: E402
from friday.intelligence.self_state import SELF_STATE, SelfStatus  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.llm import LlmProvider, LlmRequest, LlmResponse  # noqa: E402
from friday.orchestrator import Observation, OrchestratorResult, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult  # noqa: E402
from friday.session import SESSION  # noqa: E402


class ScriptedPlanner(LlmProvider):
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


def no_subgoals() -> str:
    """A decomposition reply meaning 'doesn't split into more than one
    subgoal' — see friday.orchestrator.Orchestrator.decompose_goal. Prepend
    this to a ScriptedPlanner's replies for any goal text containing a
    connector word (looks_decomposable) so the one extra bounded call
    Phase 11.2 adds before run_goal starts doesn't eat into the replies the
    test scripted for run_goal's own decisions."""
    return json.dumps({"subgoals": []})


async def _run() -> None:
    store.init()
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    # -- A: goal creation + status transitions -------------------------------
    print("\n--- A: goal creation and status transitions ---\n")
    g = goals_mod.create("open my project", "open my project", success_criteria="project is open")
    ok = g.status == goals_mod.GoalStatus.PENDING and g.id and g.objective == "open my project"
    print(f"  {'OK  ' if ok else 'MISS'} goal created PENDING -> {g.id}")
    overall &= ok

    goals_mod.update_status(g.id, goals_mod.GoalStatus.RUNNING)
    fetched = goals_mod.get(g.id)
    ok = fetched is not None and fetched.status == goals_mod.GoalStatus.RUNNING
    print(f"  {'OK  ' if ok else 'MISS'} PENDING -> RUNNING persisted")
    overall &= ok

    goals_mod.update_status(g.id, goals_mod.GoalStatus.FAILED, failure_reason="could not find project")
    fetched = goals_mod.get(g.id)
    ok = (
        fetched is not None and fetched.status == goals_mod.GoalStatus.FAILED
        and fetched.failure_reason == "could not find project"
    )
    print(f"  {'OK  ' if ok else 'MISS'} RUNNING -> FAILED with reason persisted")
    overall &= ok

    child = goals_mod.create("no, my college project", "open my college project", parent_goal_id=g.id)
    ok = child.parent_goal_id == g.id
    print(f"  {'OK  ' if ok else 'MISS'} follow-up goal links to its parent")
    overall &= ok

    # -- B: goal classification ------------------------------------------------
    print("\n--- B: request classification ---\n")
    cases = [
        ("what time is it", False, goals_mod.GoalKind.SIMPLE_REQUEST),
        ("open chrome and go to my email", False, goals_mod.GoalKind.OBJECTIVE),
        ("open my project and then tell me what to work on next", False, goals_mod.GoalKind.MULTI_STEP),
        ("no, I meant my college project", False, goals_mod.GoalKind.CORRECTION),
        ("the other one", True, goals_mod.GoalKind.FOLLOW_UP),
    ]
    class_ok = 0
    for text, has_recent, expected in cases:
        kind = goals_mod.classify(text, has_recent_goal=has_recent)
        good = kind == expected
        class_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {text!r:55} -> {kind.value} (expected {expected.value})")
    overall &= class_ok == len(cases)

    # -- C: working memory is bounded ------------------------------------------
    print("\n--- C: working memory stays bounded ---\n")
    INTEL.reset()
    for i in range(50):
        INTEL.record_action(f"tool.{i}", "")
    ok = len(INTEL.state.recent_actions) <= CFG.intelligence.max_recent_actions
    print(f"  {'OK  ' if ok else 'MISS'} recent_actions capped at {CFG.intelligence.max_recent_actions} "
          f"(has {len(INTEL.state.recent_actions)}) after 50 recorded")
    overall &= ok

    wm = working_memory.build("some unrelated goal text", max_actions=3)
    ctx = wm.as_context(max_chars=50)
    ok = len(ctx) <= 51  # allow for the trailing ellipsis character
    print(f"  {'OK  ' if ok else 'MISS'} as_context() respects max_chars -> len={len(ctx)}")
    overall &= ok

    long_wm = working_memory.build(
        "goal", max_actions=3,
    )
    long_wm.relevant_facts = ["a" * 1000]
    long_ctx = long_wm.as_context(max_chars=100)
    ok = len(long_ctx) <= 101
    print(f"  {'OK  ' if ok else 'MISS'} a huge fact is still truncated to max_chars -> len={len(long_ctx)}")
    overall &= ok

    # -- D: evaluator is evidence-based, not optimistic ------------------------
    print("\n--- D: evaluator (deterministic, evidence-based) ---\n")
    ok_step = Observation(PlanStep("mock.tool", {}), True, "did the thing")
    fail_step = Observation(PlanStep("mock.tool", {}), False, "could not do the thing", error="SomeError")
    denied_step = Observation(PlanStep("mock.tool", {}), False, "denied", error="PermissionError_")

    ev = evaluator.evaluate_step(ok_step)
    ok = ev.success and not ev.needs_replan
    print(f"  {'OK  ' if ok else 'MISS'} successful step -> success=True, needs_replan=False")
    overall &= ok

    ev = evaluator.evaluate_step(fail_step)
    ok = (not ev.success) and ev.needs_replan
    print(f"  {'OK  ' if ok else 'MISS'} ordinary failure -> success=False, needs_replan=True")
    overall &= ok

    ev = evaluator.evaluate_step(denied_step)
    ok = (not ev.success) and (not ev.needs_replan)
    print(f"  {'OK  ' if ok else 'MISS'} permission denial -> needs_replan=False (never replanned)")
    overall &= ok

    complete_result = OrchestratorResult(
        goal="g", observations=[ok_step], ok=True, summary="done", stopped="completed",
    )
    ev = evaluator.evaluate_goal(complete_result)
    ok = ev.goal_complete and ev.success
    print(f"  {'OK  ' if ok else 'MISS'} all-ok + stopped=completed -> goal_complete=True")
    overall &= ok

    partial_result = OrchestratorResult(
        goal="g", observations=[ok_step, fail_step], ok=False, summary="failed midway", stopped="failure",
    )
    ev = evaluator.evaluate_goal(partial_result)
    ok = not ev.goal_complete
    print(f"  {'OK  ' if ok else 'MISS'} a failed step never counts as goal_complete, even with prior successes")
    overall &= ok

    # -- E: episode recording, retrieval, and secret sanitization -------------
    print("\n--- E: episodes: record, retrieve, sanitize secrets ---\n")
    secret_steps = [
        Observation(PlanStep("web.login", {"username": "alice", "password": "hunter2"}), True, "logged in"),
    ]
    eid = episodes.record(
        "log into the test site", goal_id=None, context="", steps=secret_steps,
        stopped="completed", ok=True, duration_ms=42,
    )
    stored = [e for e in episodes.recent(50) if e.id == eid][0]
    ok = stored.plan[0]["args"]["password"] == "[redacted]" and stored.plan[0]["args"]["username"] == "alice"
    print(f"  {'OK  ' if ok else 'MISS'} password redacted, non-secret args kept -> {stored.plan[0]['args']}")
    overall &= ok

    episodes.record(
        "open my friday project in vs code", goal_id=None, context="", steps=[ok_step],
        stopped="completed", ok=True, duration_ms=10,
    )
    similar = episodes.retrieve_similar("open my friday project", k=3, success_only=True)
    ok = any("friday project" in e.goal_text for e in similar)
    print(f"  {'OK  ' if ok else 'MISS'} experience retrieval finds a semantically similar past goal "
          f"({len(similar)} hit(s))")
    overall &= ok

    episodes.record(
        "a goal that always fails", goal_id=None, context="", steps=[fail_step],
        stopped="failure", ok=False, duration_ms=5,
    )
    only_success = episodes.retrieve_similar("a goal that always fails", k=5, success_only=True)
    ok = all(e.success for e in only_success)
    print(f"  {'OK  ' if ok else 'MISS'} success_only=True never returns a failed episode")
    overall &= ok

    # -- F: corrections: detection + recording ---------------------------------
    print("\n--- F: correction detection and recording ---\n")
    ok = goals_mod.looks_like_correction("no, I meant my college project")
    ok &= not goals_mod.looks_like_correction("open my college project")
    print(f"  {'OK  ' if ok else 'MISS'} looks_like_correction distinguishes a correction from a plain request")
    overall &= ok

    before = len(corrections.recent(1000))
    corrections.record(
        "no, I meant my college project", goal_id=g.id,
        original_interpretation="open my project", corrected_objective="open my college project",
    )
    after = corrections.recent(1000)
    ok = len(after) == before + 1 and after[0].goal_id == g.id
    print(f"  {'OK  ' if ok else 'MISS'} correction persisted and linked to the right goal")
    overall &= ok

    goal_corrections = corrections.for_goal(g.id)
    ok = len(goal_corrections) == 1
    print(f"  {'OK  ' if ok else 'MISS'} for_goal() finds it back")
    overall &= ok

    # -- G: correction detection wired into Session.handle, non-disruptive ----
    print("\n--- G: Session records a correction without breaking normal handling ---\n")
    before = len(corrections.recent(1000))
    result = await SESSION.handle("no, I meant my college project", actor="test")
    after = len(corrections.recent(1000))
    ok = after == before + 1
    print(f"  {'OK  ' if ok else 'MISS'} a correction-shaped utterance is recorded via the normal Session.handle path")
    overall &= ok
    # It must still be handled as a normal utterance afterward (not swallowed).
    ok = isinstance(result, SkillResult)
    print(f"  {'OK  ' if ok else 'MISS'} Session.handle still returns a normal result -> ok={result.ok}")
    overall &= ok

    before = len(corrections.recent(1000))
    await SESSION.handle("what time is it", actor="test")
    after = len(corrections.recent(1000))
    ok = after == before
    print(f"  {'OK  ' if ok else 'MISS'} an ordinary command records no correction")
    overall &= ok

    # -- H: self-state reflects real activity via the bus, no duplicate FSM ---
    print("\n--- H: self-state tracks activity via BUS events ---\n")
    SELF_STATE.wire()  # idempotent; already wired by SESSION's __init__
    await SESSION.handle("what time is it", actor="test")
    snap = SELF_STATE.snapshot()
    ok = snap.status in (SelfStatus.IDLE, SelfStatus.EXECUTING, SelfStatus.FAILED)
    print(f"  {'OK  ' if ok else 'MISS'} self-state updated after a real command -> status={snap.status.value}")
    overall &= ok

    # -- I: planner context stays bounded end-to-end via plan.run --------------
    print("\n--- I: plan.run attaches bounded working-memory context, unchanged contract ---\n")
    planner = ScriptedPlanner([call("meta.capabilities"), done("Reported capabilities.")])
    before_goals = len(goals_mod.recent(1000))
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": "tell me what skills you have"}, actor="test")
    after_goals = len(goals_mod.recent(1000))
    ok = (
        result.ok and result.data.get("stopped") == "completed"
        and "goal_id" in result.data and after_goals == before_goals + 1
    )
    print(f"  {'OK  ' if ok else 'MISS'} plan.run creates exactly one tracked goal, unchanged speech/ok contract "
          f"-> goal_id={result.data.get('goal_id')}")
    overall &= ok

    recorded_goal = goals_mod.get(result.data["goal_id"]) if result.data.get("goal_id") else None
    ok = recorded_goal is not None and recorded_goal.status == goals_mod.GoalStatus.SUCCEEDED
    print(f"  {'OK  ' if ok else 'MISS'} the tracked goal is marked SUCCEEDED")
    overall &= ok

    # The *context block* plan.run attaches (desktop summary + bounded working
    # memory) must stay small regardless of how large the tool catalog itself
    # is — that catalog dump is pre-existing orchestrator behavior, not part
    # of what Phase 10 adds here, so this checks the context line specifically
    # rather than the whole prompt.
    context_lines = [
        line for p in planner.prompts for line in p.splitlines()
        if line.startswith("Current desktop context:")
    ]
    max_context_chars = (
        CFG.desktop_observer.max_ocr_chars + CFG.intelligence.working_memory_max_chars
        + CFG.intelligence.experience_context_max_chars + 200
    )
    ok = bool(context_lines) and all(len(line) <= max_context_chars for line in context_lines)
    print(f"  {'OK  ' if ok else 'MISS'} attached context block stays bounded "
          f"({[len(line) for line in context_lines]} chars, cap {max_context_chars})")
    overall &= ok

    matching_episodes = [e for e in episodes.recent(200) if e.goal_id == recorded_goal.id] if recorded_goal else []
    ok = len(matching_episodes) == 1 and matching_episodes[0].success
    print(f"  {'OK  ' if ok else 'MISS'} exactly one episode recorded for this goal, marked successful")
    overall &= ok

    # -- J: bounded replanning survives the real plan.run path (config opt-in) -
    print("\n--- J: plan.run replans a bounded number of times when configured to ---\n")
    CFG.planner.max_replans = 1
    planner = ScriptedPlanner([
        no_subgoals(),  # this goal's "then" makes it decomposable (Phase 11.2)
        call("project.inspect", {"name": "a-project-that-does-not-exist-xyz-123"}),
        call("meta.capabilities"),
        done("Recovered after the first step failed."),
    ])
    with scripted_provider(planner):
        result = await EXECUTOR.run(
            "plan.run", {"goal": "inspect a bad project, then recover"}, actor="test",
        )
    ok = (
        result.ok and result.data.get("stopped") == "completed"
        and len(result.data.get("steps", [])) == 2
        and not result.data["steps"][0]["ok"] and result.data["steps"][1]["ok"]
    )
    print(f"  {'OK  ' if ok else 'MISS'} first step fails, replan recovers, plan completes -> "
          f"stopped={result.data.get('stopped')}, steps={len(result.data.get('steps', []))}")
    overall &= ok
    CFG.planner.max_replans = 0

    # -- K: goal/episode finalization never breaks plan.run on its own error ---
    print("\n--- K: intelligence-layer failures are non-fatal to plan.run ---\n")
    original = goals_mod.create

    def boom(*a, **kw):
        raise RuntimeError("simulated intelligence-layer failure")

    goals_mod.create = boom
    try:
        planner = ScriptedPlanner([done("Still works.")])
        with scripted_provider(planner):
            result = await EXECUTOR.run("plan.run", {"goal": "should still work"}, actor="test")
        ok = result.ok and result.data.get("stopped") == "completed"
        print(f"  {'OK  ' if ok else 'MISS'} plan.run still completes even if goal tracking throws -> ok={result.ok}")
        overall &= ok
    finally:
        goals_mod.create = original

    # -- L: training-data export, real data only, secrets sanitized -----------
    print("\n--- L: export_training_data pulls only real recorded episodes ---\n")
    import importlib

    export_mod = importlib.import_module("export_training_data")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "export.jsonl"
        count = export_mod.export("all", out)
        lines = out.read_text(encoding="utf-8").splitlines()
        ok = count == len(lines) and count > 0
        print(f"  {'OK  ' if ok else 'MISS'} exported {count} real episode(s) as JSONL")
        overall &= ok

        rows = [json.loads(line) for line in lines]
        ok = all("password" not in json.dumps(r) or "[redacted]" in json.dumps(r) for r in rows)
        print(f"  {'OK  ' if ok else 'MISS'} no raw secret leaks into the export")
        overall &= ok

        out2 = Path(tmp) / "success.jsonl"
        success_count = export_mod.export("success", out2)
        rows2 = [json.loads(line) for line in out2.read_text(encoding="utf-8").splitlines()]
        ok = success_count == len(rows2) and all(r["success"] for r in rows2)
        print(f"  {'OK  ' if ok else 'MISS'} --filter success only includes successful episodes ({success_count})")
        overall &= ok

        out3 = Path(tmp) / "failed.jsonl"
        failed_count = export_mod.export("failed", out3)
        rows3 = [json.loads(line) for line in out3.read_text(encoding="utf-8").splitlines()]
        ok = failed_count == len(rows3) and all(not r["success"] for r in rows3)
        print(f"  {'OK  ' if ok else 'MISS'} --filter failed only includes failed episodes ({failed_count})")
        overall &= ok

    # -- M: experience retrieval (Phase 11.1): relevance, bounds, wiring ------
    # NOTE: this section only ever ADDS rows (same convention as every
    # section above). Since Phase 19.0 the whole script runs against a
    # throwaway SQLite file (see `main` below / friday.store.use_temp_db),
    # not the live app's real data/friday.db. "Zero relevant episodes" cases
    # below are still simulated by monkeypatching the retrieval functions.
    #
    # Each sub-scenario below gets its own fresh uuid4 tag rather than one
    # shared literal prefix: short sentences embedded with bge-small-en are
    # disproportionately influenced by a rare/unique shared token, so two
    # genuinely unrelated goals that happen to share a static test prefix
    # (e.g. a hardcoded "zzz_exp") can spuriously score above the 0.5
    # similarity threshold against each other. A fresh random tag per
    # scenario keeps each pocket of test data isolated from the others (and
    # from real accumulated history) without that artifact.
    print("\n--- M: experience retrieval feeds bounded, relevant context into plan.run ---\n")

    # 1 & 2: relevant success retrieved; a genuinely unrelated episode
    # (deliberately untagged, so it shares no token with tag_a) is excluded.
    tag_a = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    episodes.record(
        f"{tag_a} open whatsapp and message rahul", goal_id=None, context="", steps=[ok_step],
        stopped="completed", ok=True, duration_ms=12,
    )
    episodes.record(
        "completely unrelated topic: baking a chocolate cake for a weekend party",
        goal_id=None, context="", steps=[ok_step], stopped="completed", ok=True, duration_ms=12,
    )
    rel = experience.retrieve_relevant_experience(f"{tag_a} message rahul on whatsapp")
    ok = any("message rahul" in e.goal_text for e in rel.successes)
    print(f"  {'OK  ' if ok else 'MISS'} relevant successful episode retrieved ({len(rel.successes)} hit(s))")
    overall &= ok

    ok = all("baking a chocolate cake" not in e.goal_text for e in (rel.successes + rel.failures))
    print(f"  {'OK  ' if ok else 'MISS'} an unrelated episode is excluded from the result")
    overall &= ok

    # 3: a relevant failure is retrieved (equally important, not an afterthought).
    tag_b = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    fail_goal = goals_mod.create(f"{tag_b} open whatsapp and search a contact", f"{tag_b} open whatsapp and search a contact")
    episodes.record(
        f"{tag_b} open whatsapp and search a contact", goal_id=fail_goal.id, context="", steps=[fail_step],
        stopped="failure", ok=False, duration_ms=9,
    )
    rel = experience.retrieve_relevant_experience(f"{tag_b} search a contact on whatsapp")
    ok = any("search a contact" in e.goal_text for e in rel.failures)
    print(f"  {'OK  ' if ok else 'MISS'} relevant failed episode retrieved ({len(rel.failures)} hit(s))")
    overall &= ok

    # 4: a correction linked to that same (failed) goal is surfaced as evidence.
    corrections.record(
        "no, I meant Chrome, not Edge", goal_id=fail_goal.id,
        original_interpretation=f"{tag_b} open whatsapp and search a contact",
    )
    rel = experience.retrieve_relevant_experience(f"{tag_b} search a contact on whatsapp")
    ctx_text = rel.as_context()
    ok = "Chrome, not Edge" in ctx_text
    print(f"  {'OK  ' if ok else 'MISS'} linked correction surfaced in the formatted experience context")
    overall &= ok

    # 5: a secret-looking correction is redacted before it can reach context.
    tag_c = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    secret_goal = goals_mod.create(f"{tag_c} login goal for redaction test", f"{tag_c} login goal for redaction test")
    episodes.record(
        f"{tag_c} login goal for redaction test", goal_id=secret_goal.id, context="", steps=[fail_step],
        stopped="failure", ok=False, duration_ms=5,
    )
    corrections.record(
        "no, the password: hunter2 is what you should have used", goal_id=secret_goal.id,
    )
    rel = experience.retrieve_relevant_experience(f"{tag_c} login goal for redaction test")
    ctx_text = rel.as_context()
    ok = "hunter2" not in ctx_text
    print(f"  {'OK  ' if ok else 'MISS'} a secret-looking correction is redacted before reaching context")
    overall &= ok

    # 6: retrieval limit enforced across successes+failures combined.
    tag_d = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    for i in range(10):
        episodes.record(
            f"{tag_d} limit test goal variant {i}", goal_id=None, context="", steps=[ok_step],
            stopped="completed", ok=True, duration_ms=1,
        )
    rel = experience.retrieve_relevant_experience(f"{tag_d} limit test goal variant", limit=3)
    total = len(rel.successes) + len(rel.failures)
    ok = total <= 3
    print(f"  {'OK  ' if ok else 'MISS'} combined retrieval stays at or under limit=3 (got {total})")
    overall &= ok

    # 7: character limit enforced on the formatted block.
    ctx_text = rel.as_context(max_chars=120)
    ok = len(ctx_text) <= 121  # allow the trailing ellipsis character
    print(f"  {'OK  ' if ok else 'MISS'} as_context() respects max_chars -> len={len(ctx_text)}")
    overall &= ok

    # 8: retrieval failure is non-fatal. Both retrieval functions are forced
    # to raise (a broken embedding model would realistically affect success
    # and failure queries alike, since both call the same `embed()`) —
    # `retrieve_relevant_experience` must swallow both and return empty
    # rather than let the exception propagate into plan.run.
    tag_e = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    original_retrieve_similar = episodes.retrieve_similar
    original_retrieve_similar_failures = episodes.retrieve_similar_failures

    def boom(*a, **kw):
        raise RuntimeError("simulated embedding failure")

    episodes.retrieve_similar = boom
    episodes.retrieve_similar_failures = boom
    try:
        rel = experience.retrieve_relevant_experience(f"{tag_e} anything at all")
        ok = rel.is_empty()
        print(f"  {'OK  ' if ok else 'MISS'} retrieval failure yields empty experience instead of raising")
        overall &= ok
    finally:
        episodes.retrieve_similar = original_retrieve_similar
        episodes.retrieve_similar_failures = original_retrieve_similar_failures

    # 9: plan.run's *actual* planner prompt contains the retrieved episode —
    # not just that retrieve_relevant_experience works in isolation.
    tag_f = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    reused_goal_text = f"{tag_f} reused pattern: open project and report status"
    episodes.record(
        reused_goal_text, goal_id=None, context="",
        steps=[ok_step], stopped="completed", ok=True, duration_ms=8,
    )
    planner = ScriptedPlanner([done("Reported status.")])
    with scripted_provider(planner):
        result = await EXECUTOR.run("plan.run", {"goal": reused_goal_text}, actor="test")
    ok = result.ok
    reused_lines = [
        line for p in planner.prompts for line in p.splitlines()
        if tag_f in line and "SUCCEEDED before" in line
    ]
    ok = ok and bool(reused_lines)
    print(f"  {'OK  ' if ok else 'MISS'} the retrieved episode actually reaches the planner's prompt "
          f"-> {reused_lines[:1]}")
    overall &= ok

    # 10: plan.run still works when nothing relevant exists (simulated via
    # monkeypatch — no real "empty database" state to construct safely
    # against the live store, and none is needed to prove this path).
    tag_zero = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    episodes.retrieve_similar = lambda *a, **kw: []
    original_retrieve_failures = episodes.retrieve_similar_failures
    episodes.retrieve_similar_failures = lambda *a, **kw: []
    try:
        planner = ScriptedPlanner([done("Still works with nothing relevant on file.")])
        with scripted_provider(planner):
            result = await EXECUTOR.run(
                "plan.run", {"goal": f"{tag_zero} a goal with no relevant prior episodes"}, actor="test",
            )
        ok = result.ok
        print(f"  {'OK  ' if ok else 'MISS'} plan.run still completes normally with zero relevant episodes")
        overall &= ok
    finally:
        episodes.retrieve_similar = original_retrieve_similar
        episodes.retrieve_similar_failures = original_retrieve_failures

    # 11: many near-duplicate episodes still yield a bounded block, not an
    # ever-growing prompt. Reuses tag_f's goal text so these really are
    # near-duplicates of an existing episode, not just more relevant ones.
    for i in range(15):
        episodes.record(
            reused_goal_text, goal_id=None, context="",
            steps=[ok_step], stopped="completed", ok=True, duration_ms=8,
        )
    rel = experience.retrieve_relevant_experience(reused_goal_text)
    ctx_text = rel.as_context(max_chars=CFG.intelligence.experience_context_max_chars)
    item_count = len(rel.successes) + len(rel.failures)
    ok = item_count <= CFG.intelligence.experience_max_episodes
    ok = ok and len(ctx_text) <= CFG.intelligence.experience_context_max_chars + 1
    print(f"  {'OK  ' if ok else 'MISS'} 15 near-duplicate episodes still yield a bounded block "
          f"({item_count} items, {len(ctx_text)} chars)")
    overall &= ok

    # 12: old, unrelated personal history isn't dragged into an unrelated
    # goal's context — the query below deliberately shares no tag/token
    # with the "personal" episode it must not retrieve.
    tag_g = f"zzz_exp_{uuid.uuid4().hex[:8]}"
    episodes.record(
        f"{tag_g} remember my medical appointment details are private", goal_id=None, context="",
        steps=[ok_step], stopped="completed", ok=True, duration_ms=3,
    )
    rel = experience.retrieve_relevant_experience("open the calculator application right now")
    ok = not any("medical appointment" in e.goal_text for e in (rel.successes + rel.failures))
    print(f"  {'OK  ' if ok else 'MISS'} unrelated personal history isn't injected into an unrelated goal's context")
    overall &= ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


async def main() -> None:
    # Phase 19.0 test-isolation fix: this script used to run against the real,
    # shared, ever-growing data/friday.db, so two of its own checks
    # ("experience retrieval finds a semantically similar past goal" and
    # "plan.run creates exactly one tracked goal") failed intermittently from
    # top-k crowding by accumulated real episodes/goals — the same class of
    # flakiness Phase 14.0 fixed for smoke_experience_planning.py and
    # smoke_goal_decomposition.py via friday.store.use_temp_db. Every check
    # below is unchanged; only the database it runs against is now a
    # throwaway file (removed on exit, real DB path/connection restored).
    with store.use_temp_db():
        await _run()


if __name__ == "__main__":
    asyncio.run(main())
