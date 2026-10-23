"""Phase 67 -- when an adaptive recovery may close a stage, and what a bare `done` may not close.

Found by the one failure left from Phase 66: `smoke_goal_decomposition` scenario E ("recovered -> ok=True, recovery_attempts=1, final
status=active"). Since Phase 54 a failure counts as recovered only when a LATER success pursued the same objective, and since Phase 53
a planner `done` closes no subgoal while the run's report still needs an account. Two defects hid behind that:

  1. the Phase 53 guard looked at the WHOLE run: one stage's failure held every other open stage open, although its own steps all
     succeeded (and the stage a failure belonged to was still credited when the run moved ON to the next stage -- by an explicit
     `subgoal_index` or the evidence auto-advance -- because an unrelated later success looked like progress);
  2. the read-only evidence stops (discovery sufficiency, look-only) still closed every open stage unconditionally.

Phase 67 credits each stage from ITS OWN steps (`friday.steps.stage_standing`): resolved = a success of its own, every failure of its
own recovered (a later success against the same objective -- the same tool and target, or another tool of the same kind of action on
the same target), nothing unknown / running / waiting on the user; unresolved = attempted and not that; untouched = no step of its own.
The orchestrator closes only what the evidence supports, marks an unresolved stage FAILED when the run moves past it, and lets `done`
close an untouched stage only where nothing contradicts the planner. The rule is stored where it already was (recovery flags in
`goals.progress`, stage statuses in `goals.subgoals`), so a restart reproduces it.

Drives REAL `plan.run` goals (real Executor, real audit, real Goal rows, a scripted model behind the `llm` seam, `test.sr_*` fixture
skills), `Orchestrator.run_goal` with registered read-only fixtures for the evidence stops, and the pure functions directly. Nothing
real runs and every database is a throwaway.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
from phase21_common import ScriptedPlanner, check, done, finish, scenario, scripted_provider  # noqa: E402

from friday import audit, auditview, owner, steps, store, verify  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.intent import GoalScope  # noqa: E402
from friday.llm import LlmResponse  # noqa: E402
from friday.orchestrator import (  # noqa: E402
    Observation, Orchestrator, PlanStep, ToolSpec, _advance_subgoals, _close_stages_on_done,
)
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402

REGISTRY.discover()
LOG: list[str] = []
FAIL_NAMES: set[str] = set()   # test.sr_write fails for these names, every time
FAIL_ONCE: set[str] = set()    # ... for these only the first time (a flaky write)
GATE = threading.Event()
DEAD_ID = owner.Identity("fake-dead67", 4_200_067, 1.0, owner._host())


def probe_dead(pid, start, host):  # noqa: ANN001
    return owner.DEAD if pid == DEAD_ID.pid else owner.UNKNOWN


@skill(name="test.sr_write", tier="L1", action="modify", description="write a named file (Phase 67 fixture)")
def write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"write:{name}")
    if name in FAIL_NAMES:
        return SkillResult(speech="Could not write the file: disk full.", ok=False)
    if name in FAIL_ONCE:
        FAIL_ONCE.discard(name)
        return SkillResult(speech="Could not write the file: busy.", ok=False)
    return SkillResult(speech="Wrote the file.")


@skill(name="test.sr_append", tier="L1", action="modify", description="append to a named file (Phase 67 fixture)")
def append(name: str = "a", text: str = "") -> SkillResult:
    LOG.append(f"append:{name}")
    return SkillResult(speech="Appended to the file.")


@skill(name="test.sr_send", tier="L1", action="communicate", description="send a named file (Phase 67 fixture)")
def send(name: str = "a") -> SkillResult:
    LOG.append(f"send:{name}")
    return SkillResult(speech="Sent the file.")


@skill(name="test.sr_slow", tier="L1", action="modify", description="a write that outlives its timeout (Phase 67 fixture)")
def slow(name: str = "a") -> SkillResult:
    LOG.append("slow:start")
    GATE.wait(20)
    LOG.append("slow:finished")
    return SkillResult(speech="Wrote the slow file.")


@skill(name="test.sr_confirm", tier="L2", action="modify", description="a write that needs confirmation (Phase 67 fixture)")
def confirm_write(name: str = "a", text: str = "") -> SkillResult:
    LOG.append("confirm:ran")
    return SkillResult(speech="Wrote the protected file.")


@skill(name="test.sr_view", tier="L0", action="observe", description="look at the live screen -- always fails (Phase 67 fixture)")
def view() -> SkillResult:
    return SkillResult(speech="Could not look at the screen.", ok=False)


@skill(name="test.sr_time", tier="L0", action="read", description="the current time (Phase 67 fixture)")
def now() -> SkillResult:
    return SkillResult(speech="It's 3:45 PM.", data={"time": "15:45"})


for _t in ("test.sr_write", "test.sr_append", "test.sr_slow", "test.sr_confirm"):
    auditview.POLICY[_t] = {"name": auditview.KEEP, "text": auditview.OMIT}
auditview.POLICY["test.sr_send"] = {"name": auditview.KEEP}

PASSES = verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", True, "it ran", "it ran")])
CONTRADICTS = verify.Verifier(lambda a, d, b: [verify.Check("fixture effect", False, "it ran", "it did not")])
for _tool in ("test.sr_write", "test.sr_append", "test.sr_send"):
    verify.register(_tool, PASSES)


class Planner(ScriptedPlanner):
    """A scripted model: a decomposition request gets `stages`; a decision turn gets the next entry (the last one repeats)."""

    def __init__(self, decisions: list[str], stages: list[str] | None = None) -> None:
        super().__init__(decisions)
        self.stages = stages
        self.n = 0

    async def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        system = next((m.content for m in request.messages if m.role == "system"), "")
        if "Break a real-world goal down" in system:
            text = json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in self.stages or []]})
        else:
            text = self.replies[min(self.n, len(self.replies) - 1)]
            self.n += 1
        return LlmResponse(text=text, model="scripted", provider=self.name)


# One long-lived event loop on its own thread, like the app's backend (`asyncio.run` would wait at exit for the abandoned worker thread).
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True, name="stage-recovery-loop").start()


def on_loop(coro, timeout: float = 60.0):
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout)


def call(tool: str, args: dict | None = None, stage: int | None = None) -> str:
    payload: dict = {"action": "call", "tool": tool, "args": args or {}}
    if stage is not None:
        payload["subgoal_index"] = stage
    return json.dumps(payload)


GOAL = "Write file a, then send it"
STAGES = ["Write the file", "Send the file"]


def run_plan(decisions: list[str], stages: list[str] | None = None, goal: str = GOAL):
    LOG.clear()
    with scripted_provider(Planner(decisions, stages if stages is not None else STAGES)):
        return on_loop(EXECUTOR.run("plan.run", {"goal": goal}, actor="text"))


def stage_rows(gid: str) -> list[str]:
    """The stage statuses as saved with the goal -- what a restart finds."""
    return [s.status.value for s in (goals_mod.get(gid).subgoals or [])]


def outcome(r: SkillResult) -> str:
    return r.data["report"]["outcome"]


def tuples(report: dict) -> list[tuple]:
    return [(s["tool"], s["state"], bool(s.get("recovered"))) for s in report["steps"]]


def parity(label: str, r: SkillResult) -> None:
    """The goal as a restart would find it (the database alone) says what the live run said. (Until Phase 68 one account had to be
    compared more loosely: a step the tool called successful but the read-back contradicted was `failed` live and `unknown` once rebuilt.
    It is now `failed` in both, so every account is compared exactly.)"""
    gid = r.data["goal_id"]
    live = r.data["report"]
    rebuilt = steps.reconstruct(gid).to_dict()
    check(f"{label}: rebuilt from the database alone, the report matches the live one (outcome, steps, what is left)",
          rebuilt["outcome"] == live["outcome"] and tuples(rebuilt) == tuples(live)
          and rebuilt.get("remaining", []) == live.get("remaining", []), f"{rebuilt} VS {live}")
    saved = stage_rows(gid)
    check(f"{label}: every stage is recorded succeeded only if the account is complete (no stage claims what the steps do not)",
          saved != ["succeeded"] * len(saved) or rebuilt["outcome"] in ("complete", "complete_unverified"), f"{saved} {rebuilt['outcome']}")
    # A restart's refresh rewrites the stored step account from the audit log. It must never touch the stages or the goal's status, and the
    # steps stay as they were.
    before = (saved, goals_mod.get(gid).status.value, tuples(rebuilt))
    steps.refresh_goals()
    after = (stage_rows(gid), goals_mod.get(gid).status.value, tuples(steps.reconstruct(gid).to_dict()))
    check(f"{label}: a restart's refresh leaves this goal's stages, status and step account exactly as they were",
          before == after, f"{before} VS {after}")


def obs(tool: str, ok: bool = True, *, stage: str = "", args: dict | None = None, error: str = "", ver: str = "") -> Observation:
    o = Observation(PlanStep(tool, dict(args or {}), subgoal=stage), ok, "", {}, error="" if ok else (error or "RuntimeError"))
    if ver:
        o.verification = SimpleNamespace(status=SimpleNamespace(value=ver), checks=[])
    return o


def W(name: str, ok: bool = True, stage: str = "s0", **kw) -> Observation:      # a write
    return obs("test.sr_write", ok, stage=stage, args={"name": name, "text": "x"}, **kw)


def APP(name: str, ok: bool = True, stage: str = "s0", **kw) -> Observation:     # an append
    return obs("test.sr_append", ok, stage=stage, args={"name": name, "text": "x"}, **kw)


def SEND(name: str, ok: bool = True, stage: str = "s0", **kw) -> Observation:    # a send
    return obs("test.sr_send", ok, stage=stage, args={"name": name}, **kw)


def stage_list(*descriptions: str, status: SubgoalStatus = SubgoalStatus.ACTIVE) -> list[Subgoal]:
    out = [Subgoal(id=str(uuid.uuid4()), description=d) for d in descriptions]
    for s in out:
        s.status = status
    return out


def standing(observations: list[Observation], n: int = 1) -> list[str]:
    return steps.stage_standing(observations, stage_list(*[f"s{i}" for i in range(n)]))


def main() -> int:
    t0 = time.perf_counter()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s, CFG.planner.subgoal_evidence_advance)
    CFG.desktop_observer.enabled = False
    try:
        with iso_env.isolated_runtime():
            section_a()
            section_b()
            section_c()
            section_d()
            section_e()
            section_f()
    finally:
        (CFG.desktop_observer.enabled, CFG.planner.step_timeout_s, CFG.planner.total_timeout_s,
         CFG.planner.subgoal_evidence_advance) = saved
        CFG.permissions.overrides = saved_tools
        GATE.set()
    return finish("Phase 67 -- a stage is credited from its own steps; recovery by evidence, not by an unrelated success",
                  time.perf_counter() - t0, min_assertions=100, min_scenarios=6)


# ---------------------------------------------------------------------------------------------------------------------------
def section_a() -> None:
    scenario("A. the rule: a stage is resolved by ITS OWN steps (steps.stage_standing)")
    check("A1 a stage whose step succeeded is resolved", standing([W("a")]) == ["resolved"])
    check("A2 a stage whose step failed is unresolved", standing([W("a", False)]) == ["unresolved"])
    check("A3 control -- retried: a failure followed by the same tool on the SAME target is a recovery -> resolved",
          standing([W("a", False), W("a")]) == ["resolved"])
    check("A4 control -- a DIFFERENT tool of the same kind of action on the same target is a recovery too (write -> append) -> resolved",
          standing([W("a", False), APP("a")]) == ["resolved"])
    check("A5 an unrelated success -- the same tool on ANOTHER file -- does not close the failed one -> unresolved",
          standing([W("a", False), W("b")]) == ["unresolved"])
    check("A6 ...nor does another KIND of action on the same name (a send is not a write) -> unresolved",
          standing([W("a", False), SEND("a")]) == ["unresolved"])
    check("A7 scenario E's shape: an unclassified tool followed by a different unclassified tool with other arguments -> unresolved",
          standing([obs("mock.fail", False, stage="s0"), obs("mock.echo", stage="s0", args={"text": "recovered"})]) == ["unresolved"])
    check("A8 a stage with no step of its own is untouched", standing([]) == ["untouched"])
    check("A9 a failed stage is not closed by the NEXT stage's success, and does not hold it open",
          standing([W("a", False, "s0"), W("b", True, "s1")], 2) == ["unresolved", "resolved"])
    check("A10 a later stage's failure does not unwind an earlier stage that succeeded",
          standing([W("a", True, "s0"), W("b", False, "s1")], 2) == ["resolved", "unresolved"])
    check("A11 a stage whose only steps failed stays unresolved even when a LATER stage redid the same thing (reported, not guessed)",
          standing([W("a", False, "s0"), W("a", True, "s1")], 2) == ["unresolved", "resolved"])

    scenario("A'. a success that is not known to have happened is never enough")
    check("A'1 an outcome that is unknown (a state change that timed out) is unresolved, even with a later success on the same target",
          standing([W("a", False, error="timeout"), APP("a")]) == ["unresolved"])
    check("A'2 so is a step that was interrupted", standing([W("a", False, error="interrupted"), W("a")]) == ["unresolved"])
    check("A'3 so is a step waiting on the user (a declined confirmation), whatever succeeded after it",
          standing([W("a", False, error="confirmation_declined"), APP("a")]) == ["unresolved"])
    check("A'4 a read that timed out changed nothing: a later read recovers it (the read rule, unchanged)",
          standing([obs("test.sr_time", False, stage="s0", error="timeout"), obs("test.sr_time", stage="s0")]) == ["resolved"])
    check("A'5 ...but another KIND of look does not (observe vs read)",
          standing([obs("test.sr_view", False, stage="s0"), obs("test.sr_time", stage="s0")]) == ["unresolved"])
    check("A'6 blocked repeats and rejected calls are messages to the planner, not steps: alone they leave a stage untouched",
          standing([obs("test.sr_write", False, stage="s0", error="repeated_call", args={"name": "a"})]) == ["untouched"])
    check("A'7 ...and beside a real success they change nothing",
          standing([W("a"), obs("test.sr_write", False, stage="s0", error="repeated_call", args={"name": "a"})]) == ["resolved"])

    scenario("A''. completed is not verified; a contradicted result is a failure")
    done_unverified = standing([W("a")])
    done_verified = standing([W("a", ver="verified")])
    report_u = steps.build_report([W("a")], stopped="completed")
    report_v = steps.build_report([W("a", ver="verified")], stopped="completed")
    check("A''1 a verified step and an unverifiable one both resolve a stage", done_unverified == done_verified == ["resolved"])
    check("A''2 ...and the report still tells them apart (verified vs completed; complete vs complete_unverified)",
          report_v.steps[0].state is steps.StepState.VERIFIED and report_u.steps[0].state is steps.StepState.COMPLETED
          and report_v.outcome == "complete" and report_u.outcome == "complete_unverified")
    check("A''3 a result the read-back contradicted is a failure: alone it leaves the stage unresolved",
          standing([W("a", False, error="postcondition_failed")]) == ["unresolved"])
    check("A''4 ...and a retry whose effect was confirmed makes it good",
          standing([W("a", False, error="postcondition_failed"), W("a", ver="verified")]) == ["resolved"])


# ---------------------------------------------------------------------------------------------------------------------------
def section_b() -> None:
    scenario("B. what closes a stage when the run ends or moves on (orchestrator helpers, directly)")

    def closed(observations: list[Observation], statuses: list[SubgoalStatus]) -> list[str]:
        stages = stage_list(*[f"s{i}" for i in range(len(statuses))])
        for s, st in zip(stages, statuses):
            s.status = st
        _close_stages_on_done(stages, observations)
        return [s.status.value for s in stages]

    A, P, F = SubgoalStatus.ACTIVE, SubgoalStatus.PENDING, SubgoalStatus.FAILED
    check("B1 `done` closes a resolved stage and, where nothing contradicts the planner, a stage nothing was done for",
          closed([W("a")], [A, P]) == ["succeeded", "succeeded"])
    check("B2 `done` over one unresolved stage closes neither it nor the untouched stage after it; the resolved one still closes",
          closed([W("a", True, "s0"), W("b", False, "s1")], [A, A, P]) == ["succeeded", "active", "pending"])
    check("B3 a bare `done` over no steps closes nothing", closed([], [A, P]) == ["active", "pending"])
    check("B4 ...nor over steps that never ran (blocked repeats)",
          closed([obs("test.sr_write", False, stage="s0", error="repeated_call", args={"name": "a"})], [A, P]) == ["active", "pending"])
    check("B5 an already-failed stage stays failed and keeps the untouched ones open; a resolved one is still credited",
          closed([W("a", True, "s1")], [F, A, P]) == ["failed", "succeeded", "pending"])
    check("B6 `done` never reopens or rewrites a stage that is already closed",
          closed([W("a", False, "s0")], [SubgoalStatus.SUCCEEDED, P]) == ["succeeded", "pending"])
    check("B7 an unknown outcome in a stage keeps it open even with a success beside it",
          closed([W("a", False, "s0", error="timeout"), APP("a", True, "s0")], [A, P]) == ["active", "pending"])

    def advanced(observations: list[Observation] | None, old: int = 0, new: int = 1) -> list[str]:
        stages = stage_list("s0", "s1")
        stages[1].status = SubgoalStatus.PENDING
        _advance_subgoals(stages, old, new, observations)
        return [s.status.value for s in stages]

    check("B8 moving on from a resolved stage credits it", advanced([W("a")]) == ["succeeded", "active"])
    check("B9 moving on from a stage that was attempted and not resolved marks it FAILED, not succeeded",
          advanced([W("a", False)]) == ["failed", "active"])
    check("B10 ...even when the next stage's step succeeded", advanced([W("a", False, "s0"), W("b", True, "s1")]) == ["failed", "active"])
    check("B11 control -- moving on after a genuine recovery credits the stage", advanced([W("a", False), APP("a")]) == ["succeeded", "active"])
    check("B12 a stage with no step of its own is still credited when passed over (satisfied by reasoning alone)",
          advanced([]) == ["succeeded", "active"])
    check("B13 callers that pass no observations keep the earlier behaviour", advanced(None) == ["succeeded", "active"])


# ---------------------------------------------------------------------------------------------------------------------------
def section_c() -> None:
    saved_adv = CFG.planner.subgoal_evidence_advance
    try:
        for adv in (False, True):
            CFG.planner.subgoal_evidence_advance = adv
            scenario(f"C. real goals through plan.run -- recovery vs an unrelated success (evidence auto-advance {'on' if adv else 'off'})")
            c_recovery_and_unrelated(adv)
        CFG.planner.subgoal_evidence_advance = True
        scenario("C'. real goals through plan.run -- moving on, a bare `done`, and what the read-back says")
        c_moving_on()
        scenario("C''. a recovery whose effect was not, or could not be, confirmed")
        c_verification()
    finally:
        CFG.planner.subgoal_evidence_advance = saved_adv
        FAIL_NAMES.clear()
        FAIL_ONCE.clear()


COMPLETE = ("complete", "complete_unverified")   # a failure that was made good is still a step that failed: never plain "complete"


def c_recovery_and_unrelated(adv: bool) -> None:
    p = "C1" if adv else "C0"
    FAIL_NAMES.clear()
    FAIL_ONCE.clear()
    FAIL_ONCE.add("a")
    r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_write", {"name": "a", "text": "retry"}, 0),
                  call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check(f"{p}a control: the same tool retried on the same file recovers -> the stage is succeeded, the goal succeeded and ok",
          r.ok and stage_rows(gid) == ["succeeded", "succeeded"] and outcome(r) in COMPLETE
          and r.data["report"]["steps"][0].get("recovered") is True and goals_mod.get(gid).status is goals_mod.GoalStatus.SUCCEEDED,
          f"{r.ok} {stage_rows(gid)} {r.data['report']}")
    parity(f"{p}a", r)

    FAIL_ONCE.clear()
    FAIL_NAMES.add("a")
    r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_append", {"name": "a", "text": "x"}, 0),
                  call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check(f"{p}b control: ANOTHER tool of the same kind on the same file recovers too (write failed, append worked) -> succeeded, ok",
          r.ok and stage_rows(gid) == ["succeeded", "succeeded"] and r.data["report"]["steps"][0].get("recovered") is True
          and outcome(r) in COMPLETE and goals_mod.get(gid).status is goals_mod.GoalStatus.SUCCEEDED,
          f"{r.ok} {stage_rows(gid)} {r.data['report']}")
    parity(f"{p}b", r)

    # the planner stays on the first stage and goes on to something else that worked: scenario E's shape, with a classified fixture
    want = ["failed", "active"] if adv else ["active", "pending"]
    r = run_plan([call("test.sr_write", {"name": "a"}), call("test.sr_write", {"name": "b"}), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check(f"{p}c an unrelated success (the same tool on ANOTHER file) does not close the failed stage: it stays {want[0]}, the run is "
          f"partial and not ok", stage_rows(gid) == want and r.ok is False and outcome(r) == "partial"
          and goals_mod.get(gid).status is goals_mod.GoalStatus.PARTIAL, f"{r.ok} {stage_rows(gid)} {r.data['report']}")
    check(f"{p}c' ...and the account names the failed step and does not call it made good",
          "Failed: 'test.sr_write'" in r.speech and not r.data["report"]["steps"][0].get("recovered")
          and ("Send the file" in r.data["report"].get("remaining", [])) == (not adv), r.speech)
    parity(f"{p}c", r)

    r = run_plan([call("test.sr_write", {"name": "a"}), call("test.sr_send", {"name": "a"}), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check(f"{p}d a success of another KIND on the same name does not close it either (a send is not the write) -> stays {want[0]}",
          stage_rows(gid) == want and r.ok is False and outcome(r) == "partial", f"{stage_rows(gid)} {r.data['report']}")
    parity(f"{p}d", r)
    FAIL_NAMES.clear()


def c_moving_on() -> None:
    FAIL_NAMES.clear()
    FAIL_NAMES.add("a")
    r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check("C'1 the planner moves on from a failed stage: that stage is FAILED (it used to be credited succeeded), the next one's own "
          "success is credited (it used to be held open)", stage_rows(gid) == ["failed", "succeeded"], str(stage_rows(gid)))
    check("C'2 ...and the run is still not complete", r.ok is False and outcome(r) == "partial" and "remaining" not in r.data["report"],
          f"{r.ok} {r.data['report']}")
    parity("C'1", r)
    FAIL_NAMES.clear()

    r = run_plan([call("test.sr_write", {"name": "b"}, 0), call("test.sr_send", {"name": "b"}, 1), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check("C'3 control -- two stages that both succeeded: both credited, complete, ok",
          r.ok and stage_rows(gid) == ["succeeded", "succeeded"] and outcome(r) == "complete", f"{stage_rows(gid)} {r.data['report']}")
    parity("C'3", r)

    FAIL_NAMES.add("a")
    r = run_plan([call("test.sr_write", {"name": "a"}), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check("C'4 a `done` over a failed stage and nothing else is not taken at its word (the existing action nudge): no stage succeeded, "
          "the stage never started is still listed as not attempted, the goal is not succeeded",
          stage_rows(gid) == ["failed", "pending"] and r.ok is False and "Send the file" in r.data["report"].get("remaining", [])
          and goals_mod.get(gid).status is not goals_mod.GoalStatus.SUCCEEDED, f"{stage_rows(gid)} {r.ok} {r.data['report']}")
    parity("C'4", r)
    FAIL_NAMES.clear()

    r = run_plan([done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    check("C'5 a bare `done` before any step closes no stage (it used to mark every stage succeeded), and the run is called unverified",
          stage_rows(gid) == ["active", "pending"] and r.data["verified"] is False and LOG == [], f"{stage_rows(gid)} {r.data} {LOG}")
    check("C'6 ...the goal is not recorded as succeeded, and what never ran is listed",
          goals_mod.get(gid).status is goals_mod.GoalStatus.PARTIAL and "Send the file" in r.data["report"].get("remaining", []),
          f"{goals_mod.get(gid).status} {r.data['report']}")
    parity("C'5", r)


def c_verification() -> None:
    FAIL_NAMES.clear()
    FAIL_NAMES.add("a")
    verify.unregister("test.sr_append")
    try:
        r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_append", {"name": "a", "text": "x"}, 0),
                      call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
        gid = r.data["goal_id"]
        check("C''1 a recovery that cannot be read back still recovers the stage (completed counts) ...",
              stage_rows(gid) == ["succeeded", "succeeded"] and r.data["report"]["steps"][0].get("recovered") is True, str(stage_rows(gid)))
        check("C''2 ...but the run says it is unconfirmed, and is not called verified (completed is not verified)",
              outcome(r) == "complete_unverified" and r.data["verified"] is False and "unconfirmed" in r.speech
              and r.data["report"]["steps"][1]["state"] == "completed", f"{outcome(r)} {r.data['verified']} {r.speech}")
        parity("C''1", r)
    finally:
        verify.register("test.sr_append", PASSES)

    verify.register("test.sr_append", CONTRADICTS)
    try:
        r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_append", {"name": "a", "text": "x"}, 0),
                      call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
        gid = r.data["goal_id"]
        check("C''3 a 'recovery' the read-back contradicts is not one: the first stage is not succeeded and the run is not ok",
              stage_rows(gid)[0] != "succeeded" and r.ok is False and outcome(r) in ("partial", "failed"),
              f"{stage_rows(gid)} {r.ok} {r.data['report']}")
        check("C''4 ...the contradicted step is a failure, not a completion",
              r.data["report"]["steps"][1] == {"n": 2, "tool": "test.sr_append", "state": "failed"}, str(r.data["report"]))
        parity("C''3", r)
    finally:
        verify.register("test.sr_append", PASSES)
    FAIL_NAMES.clear()


# ---------------------------------------------------------------------------------------------------------------------------
def section_d() -> None:
    scenario("D. the read-only evidence stops close stages by the same rule")
    SPECS = [ToolSpec(name="test.sr_view", description="look at the live screen", tier="L0", params="", action="observe"),
             ToolSpec(name="test.sr_time", description="the current time", tier="L0", params="", action="read")]

    async def runner(tool: str, args: dict, actor: str) -> SkillResult:
        return REGISTRY.get(tool).fn(**args)

    def step(tool: str, stage: int = 0) -> str:
        return call(tool, {}, stage)

    async def drive(replies: list[str], **kw):
        stage = Subgoal(id=str(uuid.uuid4()), description="Check the time")
        orch = Orchestrator(tools=[s.name for s in SPECS], runner=runner, actor="test", llm_provider=ScriptedPlanner(replies),
                            tool_specs=SPECS, max_steps=6)
        res = await orch.run_goal("What time is it", subgoals=[stage], max_replans=1, **kw)
        return res, stage, steps.build_report(res.observations, stopped=res.stopped, subgoals=[stage])

    look_only = GoalScope("What time is it", frozenset(), frozenset(), (), "verbs")
    for label, kw in (("discovery sufficiency stop", {"discovery_mode": True}),
                      ("look-only evidence stop", {"action_scope": look_only, "coverage_goal": "What time is it"})):
        res, stage, rep = on_loop(drive([step("test.sr_time"), done("It is 3:45 PM.")], **kw))
        check(f"D1 [{label}] control: the evidence answers the goal and the stage's step succeeded -> the run ends on evidence, the stage is "
              f"succeeded", res.stopped == "completed" and res.ok and stage.status is SubgoalStatus.SUCCEEDED
              and len(res.observations) == 1, f"{res.stopped} {stage.status} {len(res.observations)}")
        res, stage, rep = on_loop(drive([step("test.sr_view"), step("test.sr_time"), done("It is 3:45 PM.")], **kw))
        check(f"D2 [{label}] the same answer, but the stage also holds a failed look that a read does not make good -> the stage is NOT "
              f"credited (it used to be), and the report agrees it is partial",
              res.stopped == "completed" and stage.status is SubgoalStatus.ACTIVE and rep.outcome == "partial"
              and [o.step.tool for o in res.observations] == ["test.sr_view", "test.sr_time"],
              f"{res.stopped} {stage.status} {rep.outcome} {[o.step.tool for o in res.observations]}")


# ---------------------------------------------------------------------------------------------------------------------------
def section_e() -> None:
    scenario("E. an outcome that is not known to have succeeded never becomes a success (timeout, restart, declined confirmation)")
    FAIL_NAMES.clear()
    CFG.planner.subgoal_evidence_advance = True
    CFG.planner.step_timeout_s = 1.0
    CFG.planner.total_timeout_s = 120.0
    GATE.clear()
    r = run_plan([call("test.sr_slow", {"name": "a"}, 0), call("test.sr_send", {"name": "a"}, 1), done("Wrote and sent it.")])
    gid = r.data["goal_id"]
    first = stage_rows(gid)
    check("E1 a step that outlived its timeout is unknown: the run stops, no stage is succeeded, the account says it cannot tell",
          r.data["stopped"] == "timeout" and "succeeded" not in first and outcome(r) == "unknown" and r.ok is False
          and LOG == ["slow:start"], f"{first} {outcome(r)} {LOG}")
    parity("E1", r)
    GATE.set()
    deadline = time.time() + 5
    while time.time() < deadline and "slow:finished" not in LOG:
        time.sleep(0.05)
    time.sleep(0.3)
    steps.refresh_goals()
    check("E2 the abandoned step then finished for real and the goal's step account now says so ...",
          steps.reconstruct(gid).steps[0].state in (steps.StepState.COMPLETED, steps.StepState.VERIFIED),
          str(steps.reconstruct(gid).to_dict()))
    check("E3 ...but nothing upgrades the stage or the goal because of it: the late success was never part of any decision",
          stage_rows(gid) == first and goals_mod.get(gid).status is not goals_mod.GoalStatus.SUCCEEDED
          and steps.reconstruct(gid).outcome != "complete", f"{stage_rows(gid)} {steps.reconstruct(gid).outcome}")
    CFG.planner.step_timeout_s = 90.0

    async def decline(skill, args, preview):  # noqa: ANN001
        return False

    EXECUTOR.set_confirm_handler(decline)
    r = run_plan([call("test.sr_write", {"name": "b"}, 0), call("test.sr_confirm", {"name": "p", "text": "x"}, 1), done("Done.")])
    gid = r.data["goal_id"]
    check("E4 a step waiting on the user is not a success: its stage is not succeeded, the stage before it (resolved) is, the run is needs_input",
          stage_rows(gid) == ["succeeded", "failed"] and outcome(r) == "needs_input" and r.ok is False and "confirm:ran" not in LOG,
          f"{stage_rows(gid)} {outcome(r)} {LOG}")
    parity("E4", r)

    # A goal left running by a process that no longer exists (Phase 52 ownership): settled from the evidence, never resumed or credited.
    g = goals_mod.create("p67 goal", "p67 goal")
    goals_mod.update_status(g.id, goals_mod.GoalStatus.RUNNING)
    stages = stage_list("Write the file", "Send the file")
    stages[1].status = SubgoalStatus.PENDING
    goals_mod.set_subgoals(g.id, stages)
    with owner._use_identity(DEAD_ID), audit.goal_scope(g.id):
        audit.record(actor="text", skill="test.sr_write", tier="L1", args={"name": "a"}, decision="auto")   # opened, never completed
    before = stage_rows(g.id)
    settled = steps.settle_interrupted_goals(probe_dead)
    rebuilt = steps.reconstruct(g.id, probe_dead)
    check("E5 a goal cut off mid-step is settled as not finished, the step as unknown, and the stages are left exactly as saved",
          settled == 1 and goals_mod.get(g.id).status in (goals_mod.GoalStatus.FAILED, goals_mod.GoalStatus.PARTIAL)
          and rebuilt.outcome == "unknown" and stage_rows(g.id) == before == ["active", "pending"]
          and rebuilt.remaining == ["Send the file"], f"{settled} {goals_mod.get(g.id).status} {rebuilt.to_dict()} {stage_rows(g.id)}")
    check("E6 settling twice changes nothing", steps.settle_interrupted_goals(probe_dead) == 0 and stage_rows(g.id) == before)


# ---------------------------------------------------------------------------------------------------------------------------
def section_f() -> None:
    scenario("F. what the user is told about a goal afterwards agrees with the stages and the steps")
    CFG.planner.subgoal_evidence_advance = True
    FAIL_NAMES.clear()
    FAIL_NAMES.add("a")
    r = run_plan([call("test.sr_write", {"name": "a"}, 0), call("test.sr_write", {"name": "b"}, 0), call("test.sr_send", {"name": "b"}, 1),
                  done("Wrote and sent it.")])
    speech, data = steps.describe_goal()
    check("F1 an unrelated success after a failure: the status answer says it did not finish and names the failed step, not a success",
          "It did not finish." in speech and "Failed: 'test.sr_write'" in speech and "succeeded" not in speech.lower().replace("not succeed", "")
          and data["report"]["outcome"] == "partial", speech)
    check("F2 ...the saved goal does not contradict that (no 'record says it succeeded' warning is needed)",
          "Its record says it succeeded" not in speech and goals_mod.get(r.data["goal_id"]).status is goals_mod.GoalStatus.PARTIAL)
    FAIL_NAMES.clear()
    FAIL_ONCE.add("c")
    r = run_plan([call("test.sr_write", {"name": "c"}, 0), call("test.sr_write", {"name": "c", "text": "again"}, 0),
                  call("test.sr_send", {"name": "c"}, 1), done("Wrote and sent it.")])
    speech, data = steps.describe_goal()
    check("F3 a genuine recovery: the status answer says it finished, with every stage succeeded",
          "It finished" in speech and data["report"]["outcome"] in COMPLETE and stage_rows(r.data["goal_id"]) == ["succeeded", "succeeded"],
          speech)


if __name__ == "__main__":
    sys.exit(main())
