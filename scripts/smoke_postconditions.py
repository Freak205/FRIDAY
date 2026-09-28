"""Phase 22.0 — deterministic scorecard for POST-CONDITION VERIFICATION.

    goal -> action -> execution -> POST-CONDITION CHECK -> verified | failed | partial | unverified

Until Phase 22 a state-changing step counted as done because the TOOL said so and the
planner said `done`. `friday/verify.py` reads the real state back after the executor
returned and builds the verdict from those observations only. This suite pins the honesty
rules, not just the happy path:

  A  the status rule and the filesystem primitives (pure, real temp files)
  B  the real Orchestrator + fixture tools that really touch a temp dir:
       create / rename / delete VERIFIED; a tool that claims success but did nothing FAILED;
       a half-done rename FAILED; a tool with no read-back UNVERIFIED (never verified);
       a crashing / slow reader UNVERIFIED (never FAILED); recovery; the done-guard
  C  the full plan.run -> real EXECUTOR pipeline: goal status, `verified`, speech; a
     DESTRUCTIVE step stays confirmation-protected (declined -> never ran, never verified)
  D  the real verifiers (volume, brightness, apps, windows, memory, schedule, notes,
     clipboard, download, process, wifi) against a scripted fake machine — match, mismatch,
     unreadable, settle-then-verify
  E  coverage and static invariants: every real state-changing skill has a verifier or an
     explicit, reasoned UNVERIFIABLE entry; verification only reads; switches
  F  read-only smoke of the REAL readers on this machine (coverage measurement)

Deterministic: scripted planner replies, a recording/fixture runner, fixture skills whose
effects are confined to a temp directory, every real non-L0 skill hard-denied, throwaway DB.
No Ollama, and nothing on the real machine is changed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario, scripted_provider

from friday import store, verify  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, ToolSpec  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

REGISTRY.discover()

V = verify.VerifyStatus
TMP = Path(tempfile.mkdtemp(prefix="friday_pc_"))
CALLS: list[tuple[str, dict]] = []
EVENTS: list[dict] = []
FLAGS: dict[str, int] = {}


def flag(name: str) -> int:
    return FLAGS.get(name, 0)


def bump(name: str) -> None:
    FLAGS[name] = FLAGS.get(name, 0) + 1


# ---------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------


async def fs_runner(tool: str, args: dict, actor: str) -> SkillResult:
    """Fixture tools that REALLY touch the temp dir (or pretend to)."""
    CALLS.append((tool, dict(args)))
    p = args.get("path") or ""
    if tool == "test.pc_create":
        Path(p).write_text(args.get("text", "hello"), encoding="utf-8")
        return SkillResult(speech=f"Created {Path(p).name}.", data={"path": p})
    if tool == "test.pc_create_liar":
        return SkillResult(speech=f"Created {Path(p).name}.", data={"path": p})  # claims it, does nothing
    if tool == "test.pc_write":
        Path(p).write_text(args.get("text", ""), encoding="utf-8")
        return SkillResult(speech="Wrote it.")
    if tool == "test.pc_rename":
        os.replace(args["old"], args["new"])
        return SkillResult(speech="Renamed it.")
    if tool == "test.pc_rename_half":
        shutil.copy(args["old"], args["new"])  # the new one appears, the old one is NOT removed
        return SkillResult(speech="Renamed it.")
    if tool == "test.pc_delete":
        os.remove(p)
        return SkillResult(speech=f"Deleted {Path(p).name}.")
    if tool == "test.pc_delete_liar":
        return SkillResult(speech=f"Deleted {Path(p).name}.")  # claims it, leaves the file
    if tool == "test.pc_noverify":
        return SkillResult(speech="Did the thing.")
    if tool == "test.pc_boom":
        return SkillResult(speech="Did the boom thing.")
    if tool == "test.pc_slow":
        return SkillResult(speech="Did the slow thing.")
    if tool == "test.pc_fail":
        return SkillResult(speech="It broke.", ok=False)
    if tool == "test.pc_none":
        return SkillResult(speech="Report mode; changed nothing.")
    if tool == "test.pc_read":
        return SkillResult(speech="Read something.")
    return SkillResult(speech=f"{tool} ok")


SPECS = [
    ToolSpec("test.pc_create", "create a file", "L1", "path (str)", action="modify"),
    ToolSpec("test.pc_create_liar", "create a file (lies)", "L1", "path (str)", action="modify"),
    ToolSpec("test.pc_write", "write a file", "L1", "path (str), text (str)", action="modify"),
    ToolSpec("test.pc_rename", "rename a file", "L1", "old (str), new (str)", action="modify"),
    ToolSpec("test.pc_rename_half", "rename a file (copies)", "L1", "old (str), new (str)", action="modify"),
    ToolSpec("test.pc_delete", "delete a file", "L2", "path (str)", action="delete"),
    ToolSpec("test.pc_delete_liar", "delete a file (lies)", "L2", "path (str)", action="delete"),
    ToolSpec("test.pc_noverify", "a state change with no read-back", "L1", "", action="modify"),
    ToolSpec("test.pc_boom", "a state change whose reader crashes", "L1", "", action="modify"),
    ToolSpec("test.pc_slow", "a state change whose reader hangs", "L1", "", action="modify"),
    ToolSpec("test.pc_fail", "a tool that fails on its own", "L1", "", action="modify"),
    ToolSpec("test.pc_none", "a setter in report mode", "L1", "", action="modify"),
    ToolSpec("test.pc_read", "a read", "L0", "", action="read"),
]


def _register_fs_verifiers() -> None:
    for name in ("test.pc_create", "test.pc_create_liar"):
        verify.register(name, verify.creates_file("path"))
    verify.register("test.pc_write", verify.writes_text("path", "text"))
    for name in ("test.pc_rename", "test.pc_rename_half"):
        verify.register(name, verify.renames_path("old", "new"))
    for name in ("test.pc_delete", "test.pc_delete_liar"):
        verify.register(name, verify.removes_path("path"))

    def boom(a, d, b):
        raise RuntimeError("reader exploded")

    verify.register("test.pc_boom", verify.Verifier(boom))

    def slow(a, d, b):
        time.sleep(1.5)
        return [verify.Check("slow", True)]

    verify.register("test.pc_slow", verify.Verifier(slow))
    verify.register("test.pc_none", verify.Verifier(lambda a, d, b: None))
    # test.pc_noverify deliberately has NO verifier.


def _register_pipeline_fixtures() -> None:
    """Real registry skills for the plan.run -> EXECUTOR pipeline (section C). Effects confined to TMP."""

    @skill(name="test.pcx_create", tier="L1", action="modify", description="create a file (post-condition smoke)")
    def _create(path: Annotated[str, "file to create"]) -> SkillResult:
        bump("pcx_create")
        Path(path).write_text("x", encoding="utf-8")
        return SkillResult(speech=f"Created {Path(path).name}.", data={"path": path})

    @skill(name="test.pcx_liar", tier="L1", action="modify", description="claims to create a file, does not")
    def _liar(path: Annotated[str, "file to create"]) -> SkillResult:
        bump("pcx_liar")
        return SkillResult(speech=f"Created {Path(path).name}.", data={"path": path})

    @skill(name="test.pcx_noverify", tier="L1", action="modify", description="changes something nothing can read back")
    def _nov() -> SkillResult:
        bump("pcx_noverify")
        return SkillResult(speech="Did the thing.")

    @skill(name="test.pcx_delete", tier="L2", action="delete", description="delete a file (needs confirmation)")
    def _delete(path: Annotated[str, "file to delete"]) -> SkillResult:
        bump("pcx_delete")
        os.remove(path)
        return SkillResult(speech=f"Deleted {Path(path).name}.")

    verify.register("test.pcx_create", verify.creates_file("path"))
    verify.register("test.pcx_liar", verify.creates_file("path"))
    verify.register("test.pcx_delete", verify.removes_path("path"))


APPROVE = [False]


async def _confirm(skill_, args, preview: str) -> bool:
    bump("confirm_prompts")
    return APPROVE[0]


def reset() -> None:
    CALLS.clear()
    EVENTS.clear()
    FLAGS.clear()
    INTEL.reset()
    SESSION.pending = None
    SESSION._followup = None
    EXECUTOR.set_confirm_handler(_confirm)
    APPROVE[0] = False
    CFG.planner.structured_output = False
    CFG.planner.decision_repair_attempts = 1
    CFG.planner.intent_guard = True
    CFG.planner.intent_prefilter = True
    CFG.planner.goal_coverage = True
    CFG.planner.postcondition_verify = True
    CFG.planner.verify_timeout_s = 4.0
    CFG.planner.max_replans = 0
    CFG.desktop_observer.enabled = False
    BASE[0] = len(goals_mod.recent(1000))


BASE = [0]


def newfile(name: str, text: str = "seed") -> str:
    p = TMP / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def target(name: str) -> str:
    """A path inside TMP that does not exist yet."""
    p = TMP / name
    if p.exists():
        p.unlink()
    return str(p)


async def drive(goal: str, replies: list[str], *, verify_on: bool | None = True, max_replans: int = 0, scope: str | None = "none"):
    planner = ScriptedPlanner(replies)
    orch = Orchestrator(
        tools=[s.name for s in SPECS], runner=fs_runner, actor="test", llm_provider=planner,
        tool_specs=SPECS, max_steps=8, verify=verify_on,
    )
    res = await orch.run_goal(goal, max_replans=max_replans)
    return res, planner


def last(res) -> Observation:
    return res.observations[-1]


async def on_event(ev) -> None:
    EVENTS.append(ev)


# =============================================================================================
# A — the status rule and the primitives
# =============================================================================================


def section_a() -> None:
    scenario("A1: status_of — the one rule from observations to a verdict")
    C = verify.Check
    check("no checks at all -> UNVERIFIED (nothing was observed)", verify.status_of([]) is V.UNVERIFIED)
    check("every check passed -> VERIFIED", verify.status_of([C("a", True), C("b", True)]) is V.VERIFIED)
    check("one check contradicts -> FAILED", verify.status_of([C("a", True), C("b", False)]) is V.FAILED)
    check("a contradiction beats an unreadable check -> FAILED", verify.status_of([C("a", None), C("b", False)]) is V.FAILED)
    check("some passed, the rest unreadable -> PARTIAL", verify.status_of([C("a", True), C("b", None)]) is V.PARTIAL)
    check("nothing readable -> UNVERIFIED", verify.status_of([C("a", None), C("b", None)]) is V.UNVERIFIED)
    check("an unreadable check is never a pass (None != True)", verify.status_of([C("a", None)]) is not V.VERIFIED)
    v = verify.build([C("file exists", True, "x exists", "x exists")])
    check("build(): status + a reason built from what was observed", v.status is V.VERIFIED and "file exists" in v.reason and v.verified)
    v = verify.build([C("file exists", False, "x exists", "it does not exist")])
    check("build(): a failure's reason states expected vs found", "expected x exists" in v.reason and "found it does not exist" in v.reason)
    check("to_dict is JSON-shaped", isinstance(v.to_dict()["checks"], list) and v.to_dict()["status"] == "failed")

    scenario("A2: filesystem primitives (real temp files)")
    f = newfile("a.txt", "hello world")
    d = TMP / "sub"
    d.mkdir(exist_ok=True)
    check("path_exists: a real file", verify.path_exists(f).passed is True)
    check("path_exists(kind=file) on a folder -> False", verify.path_exists(str(d), kind="file").passed is False)
    check("path_exists(kind=dir) on a folder -> True", verify.path_exists(str(d), kind="dir").passed is True)
    check("path_exists: a missing path -> False (evidence, not 'unknown')", verify.path_exists(str(TMP / "nope.txt")).passed is False)
    check("path_exists(min_bytes) on an empty file -> False", verify.path_exists(newfile("empty.txt", ""), min_bytes=1).passed is False)
    check("path_absent: a missing path -> True", verify.path_absent(str(TMP / "nope.txt")).passed is True)
    check("path_absent: an existing file -> False", verify.path_absent(f).passed is False)
    check("file_contains: present text -> True", verify.file_contains(f, "hello").passed is True)
    check("file_contains: absent text -> False", verify.file_contains(f, "zzz").passed is False)
    check("file_contains: unreadable file -> None (unknown, not failed)", verify.file_contains(str(TMP / "nope.txt"), "x").passed is None)
    old, new = newfile("old.txt"), target("new.txt")
    os.replace(old, new)
    moved = verify.path_moved(old, new)
    check("path_moved after a real rename: both checks pass", all(c.passed is True for c in moved) and len(moved) == 2)
    shutil.copy(new, old)
    check("path_moved when the old path is back: the 'old path is gone' check fails", any(c.passed is False for c in verify.path_moved(old, new)))


# =============================================================================================
# B — the real Orchestrator over fixture tools
# =============================================================================================


async def section_b() -> None:
    scenario("B1: a mutation executes and its post-condition holds -> VERIFIED")
    reset()
    p = target("b1.txt")
    res, pl = await drive("create the file", [call("test.pc_create", {"path": p}), done("created")])
    o = res.observations[0]
    check("the file really exists (the fixture really ran)", os.path.exists(p))
    check("the observation carries a VERIFIED verification", o.verification is not None and o.verification.status is V.VERIFIED)
    check("...and stays ok with its own speech untouched", o.ok and o.speech == "Created b1.txt." and o.error == "")
    check("...the verdict is also in obs.data['verification'] for anything that only sees data", o.data.get("verification", {}).get("status") == "verified")
    check("the run completed ok", res.ok and res.stopped == "completed")
    gv = res.verification
    check("goal-level verification: verified", gv.status == "verified" and gv.verified_ok and gv.failed == [] and gv.unverified == [])
    check("the planner saw the step as 'ok, VERIFIED' in its history", "ok, VERIFIED" in pl.prompt_of(1))

    scenario("B2: rename -> new path exists AND old path is gone")
    reset()
    old, new = newfile("b2_old.txt"), target("b2_new.txt")
    res, _ = await drive("rename the file", [call("test.pc_rename", {"old": old, "new": new}), done("renamed")])
    o = res.observations[0]
    check("a real rename: VERIFIED with two passing checks", o.verification.status is V.VERIFIED and len(o.verification.checks) == 2)
    check("...(new exists, old gone)", os.path.exists(new) and not os.path.exists(old))

    scenario("B3: delete -> the target is gone")
    reset()
    victim = newfile("b3.txt")
    res, _ = await drive("delete the file", [call("test.pc_delete", {"path": victim}), done("deleted")])
    check("a real delete: VERIFIED", last(res).verification.status is V.VERIFIED and not os.path.exists(victim))

    scenario("B4: the tool CLAIMS success but the world disagrees -> FAILED (never a success)")
    reset()
    p = target("b4.txt")
    res, pl = await drive("create the file", [call("test.pc_create_liar", {"path": p}), done("created it")])
    o = res.observations[0]
    check("the liar really did nothing", not os.path.exists(p))
    check("verification FAILED", o.verification.status is V.FAILED)
    check("the observation is turned into a failure: ok False, error 'postcondition_failed'", not o.ok and o.error == "postcondition_failed")
    check("...its speech says it did not take effect, and keeps what the tool claimed for context",
          "did not take effect" in o.speech and "Created b4.txt." in o.speech, o.speech)
    check("...the reason states expected vs found", "expected" in o.verification.reason and "does not exist" in o.verification.reason)
    check("max_replans=0: the run stops at the failed step, not ok, stopped=failure", not res.ok and res.stopped == "failure")
    check("goal-level verification: failed, naming the tool", res.verification.status == "failed" and "test.pc_create_liar" in res.verification.failed[0])
    check("the planner is never asked to declare done over it (one model call)", pl.calls == 1)

    scenario("B4b: replanning is allowed, but `done` over an unrecovered failed check is NOT a completion")
    reset()
    p = target("b4b.txt")
    res, pl = await drive("create the file", [call("test.pc_create_liar", {"path": p}), done("all good")], max_replans=1)
    check("the planner saw the FAILED step and could answer", pl.calls == 2 and "FAILED" in pl.prompt_of(1) and "did not take effect" in pl.prompt_of(1))
    check("it declared done anyway: the run is NOT ok and NOT 'completed' (every step failed)", not res.ok and res.stopped == "failure")
    check("...and the summary reports what really happened, not the planner's 'all good'", "did not take effect" in res.summary and "all good" not in res.summary, res.summary)

    reset()
    good, bad = target("b4b_good.txt"), target("b4b_bad.txt")
    res, pl = await drive(
        "create both files",
        [call("test.pc_create", {"path": good}), call("test.pc_create_liar", {"path": bad}), done("both created")], max_replans=1,
    )
    check("one step verified, one FAILED, then `done`: NOT completed (the mixed case the all-failed rule cannot catch)", not res.ok and res.stopped == "failure")
    check("...the summary names the failed check and does not repeat the planner's claim", "couldn't confirm" in res.summary and "both created" not in res.summary, res.summary)
    check("...goal-level verification: failed, with exactly one failed step", res.verification.status == "failed" and len(res.verification.failed) == 1)

    scenario("B4c: a redo that really works RECOVERS the earlier failed check")
    reset()
    p = target("b4c.txt")
    res, pl = await drive(
        "create the file",
        [call("test.pc_create_liar", {"path": p}), call("test.pc_create", {"path": p}), done("created")], max_replans=1,
    )
    check("the second, honest tool really created it", os.path.exists(p))
    gv = res.verification
    check("two different tools: not the 'same call' recovery -> the failure stands", gv.status == "failed" and res.stopped == "failure")

    reset()
    p = target("b4c2.txt")
    # the same tool + same target called again, but this time the fixture (a stateful liar) tells the truth
    state = {"n": 0}

    async def flaky(tool, args, actor):
        if tool != "test.pc_flaky":
            return SkillResult(speech="Read something.")
        state["n"] += 1
        if state["n"] == 2:  # the first call lies, the second one really does it
            Path(args["path"]).write_text("x", encoding="utf-8")
        return SkillResult(speech="Created it.", data={"path": args["path"]})

    verify.register("test.pc_flaky", verify.creates_file("path"))
    spec = [ToolSpec("test.pc_flaky", "flaky create", "L1", "path (str)", action="modify"), ToolSpec("test.pc_read", "read", "L0", "", action="read")]
    planner = ScriptedPlanner([call("test.pc_flaky", {"path": p}), call("test.pc_read"), call("test.pc_flaky", {"path": p}), done("created")])
    orch = Orchestrator(tools=[s.name for s in spec], runner=flaky, actor="test", llm_provider=planner, tool_specs=spec, verify=True)
    res = await orch.run_goal("create the file", max_replans=2)
    gv = res.verification
    check("same call, redone and verified: the earlier failure is recovered -> verified", gv.status == "verified" and res.ok and res.stopped == "completed", f"{gv.status} {res.stopped}")
    check("...both steps are still visible in the history (nothing is hidden)", [o.verification.status.value for o in res.observations if o.verification] == ["failed", "verified"])
    verify.unregister("test.pc_flaky")

    scenario("B5: a half-done rename (new appears, old stays) -> FAILED on the check that is wrong")
    reset()
    old, new = newfile("b5_old.txt"), target("b5_new.txt")
    res, _ = await drive("rename the file", [call("test.pc_rename_half", {"old": old, "new": new})])
    v = last(res).verification
    passed = {c.name: c.passed for c in v.checks}
    check("FAILED overall", v.status is V.FAILED and not last(res).ok)
    check("...the new-path check passed and the old-path check is the failing one", passed == {"new path exists": True, "old path is gone": False}, str(passed))

    scenario("B5b: a delete that did not delete -> FAILED")
    reset()
    victim = newfile("b5b.txt")
    res, _ = await drive("delete the file", [call("test.pc_delete_liar", {"path": victim})])
    check("FAILED: the target is still there", last(res).verification.status is V.FAILED and os.path.exists(victim) and not last(res).ok)

    scenario("B6: a mutation with NO reliable read-back -> UNVERIFIED, never 'verified'")
    reset()
    res, pl = await drive("do the thing", [call("test.pc_noverify"), done("did it")])
    o = res.observations[0]
    check("the step ran and reported ok (nothing is known to have failed)", o.ok and o.error == "")
    check("...but its verification is UNVERIFIED with the reason spelled out",
          o.verification.status is V.UNVERIFIED and "no way to read the result" in o.verification.reason, o.verification.reason)
    check("goal-level verification: unverified — NOT verified", res.verification.status == "unverified" and not res.verification.verified_ok)
    check("the run itself completed ok (an unverifiable step is not a failure)", res.ok and res.stopped == "completed")
    check("the planner is told plainly: 'ok, UNVERIFIED' + [not verified: ...]", "ok, UNVERIFIED" in pl.prompt_of(1) and "[not verified:" in pl.prompt_of(1))
    check("an unverifiable step can never be reported as verified by mixing it with a verified one",
          (await _mixed())[0] == "partial")

    scenario("B7: a reader that CRASHES or HANGS is unverified — not failed")
    reset()
    res, _ = await drive("do the boom", [call("test.pc_boom"), done("x")])
    v = last(res).verification
    check("a crashing verifier: UNVERIFIED, the step stays ok, the run does not crash", v.status is V.UNVERIFIED and last(res).ok and res.ok, v.status.value)
    check("...and says the read-back failed", "read-back failed" in v.reason)
    reset()
    CFG.planner.verify_timeout_s = 0.4
    t0 = time.perf_counter()
    res, _ = await drive("do the slow thing", [call("test.pc_slow"), done("x")])
    took = time.perf_counter() - t0
    v = last(res).verification
    check("a hanging verifier is bounded by verify_timeout_s: UNVERIFIED, step ok", v.status is V.UNVERIFIED and last(res).ok and "timed out" in v.reason, v.reason)
    check("...and the run was not held up for the reader's full 1.5 s", took < 1.3, f"{took:.2f}s")
    CFG.planner.verify_timeout_s = 4.0

    scenario("B8: things that must NOT be verified")
    reset()
    res, _ = await drive("read it", [call("test.pc_read"), done("x")])
    check("a read-only step carries no verification", last(res).verification is None and res.verification.status == "not_applicable")
    reset()
    res, _ = await drive("break it", [call("test.pc_fail")])
    check("a step that FAILED by itself is never verified (nothing to read back)", last(res).verification is None and not last(res).ok and last(res).error == "")
    reset()
    res, _ = await drive("report", [call("test.pc_none"), done("x")])
    check("a verifier that says 'nothing to verify for this call' (a setter in report mode) -> no verification", last(res).verification is None and res.ok)
    reset()
    p = target("b8.txt")
    res, _ = await drive("create", [call("test.pc_create", {"path": p}), done("x")], verify_on=None)
    check("an injected runner with verify=None (auto): verification is OFF (a fake tool must not make the verifier look at this machine)",
          last(res).verification is None and res.verification.status == "not_applicable")
    reset()
    p = target("b8b.txt")
    res, _ = await drive("create", [call("test.pc_create_liar", {"path": p}), done("x")], verify_on=False)
    check("verify=False: the liar is NOT caught (the pre-Phase-22 behaviour, kept switchable)", last(res).verification is None and res.ok)
    reset()
    CFG.planner.postcondition_verify = False
    p = target("b8c.txt")
    res, _ = await drive("create", [call("test.pc_create_liar", {"path": p}), done("x")], verify_on=True)
    check("CFG.planner.postcondition_verify=False overrides even verify=True", last(res).verification is None and res.ok)
    CFG.planner.postcondition_verify = True

    scenario("B9: ordering — the baseline is read BEFORE the call, the check AFTER, only for a state-changing call")
    reset()
    order: list[str] = []

    async def ordered(tool, args, actor):
        order.append("call")
        return SkillResult(speech="ok")

    verify.register("test.pc_order", verify.Verifier(
        lambda a, d, b: (order.append("check"), [verify.Check("x", True)])[1],
        prepare=lambda a: (order.append("prepare"), {})[1],
    ))
    spec = [ToolSpec("test.pc_order", "ordered", "L1", "", action="modify")]
    planner = ScriptedPlanner([call("test.pc_order"), done("x")])
    orch = Orchestrator(tools=["test.pc_order"], runner=ordered, actor="test", llm_provider=planner, tool_specs=spec, verify=True)
    await orch.run_goal("do it")
    check("order is prepare -> call -> check", order == ["prepare", "call", "check"], str(order))
    verify.unregister("test.pc_order")
    reset()
    res, _ = await drive("break it", [call("test.pc_fail")])
    check("a failed call is never verified (the check never ran)", last(res).verification is None)

    scenario("B10: run_plan (explicit steps) verifies too, and stops at the first failed check")
    reset()
    p1, p2 = target("b10a.txt"), target("b10b.txt")
    orch = Orchestrator(tools=[s.name for s in SPECS], runner=fs_runner, actor="test", tool_specs=SPECS, verify=True)
    res = await orch.run_plan("create two files", [PlanStep("test.pc_create_liar", {"path": p1}), PlanStep("test.pc_create", {"path": p2})])
    check("run_plan: the first step's check failed -> stopped, second step never ran", not res.ok and res.stopped == "failure" and len(res.observations) == 1 and not os.path.exists(p2))

    scenario("B11: events")
    reset()
    BUS.subscribe("orchestrator.verification", on_event)
    try:
        await drive("create", [call("test.pc_create_liar", {"path": target("b11.txt")}), done("x")])
    finally:
        BUS.unsubscribe("orchestrator.verification", on_event)
    check("orchestrator.verification is published with status and reason", len(EVENTS) == 1 and EVENTS[0].data["status"] == "failed" and EVENTS[0].data["tool"] == "test.pc_create_liar")


async def _mixed():
    reset()
    p = target("mixed.txt")
    res, _ = await drive("create and do a thing", [call("test.pc_create", {"path": p}), call("test.pc_noverify"), done("x")])
    return res.verification.status, res


# =============================================================================================
# C — plan.run through the real executor
# =============================================================================================


async def run_plan_goal(goal: str, replies: list[str], *, approve: bool = False, actor: str = "text"):
    APPROVE[0] = approve
    planner = ScriptedPlanner(replies)
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": goal}, actor=actor)
    return r, planner


def status_of_goal(r) -> str:
    return goals_mod.get(r.data["goal_id"]).status.value


async def section_c() -> None:
    scenario("C1: plan.run — a verified state change is a real SUCCESS")
    reset()
    p = target("c1.txt")
    r, _ = await run_plan_goal("Create the file " + p, [call("test.pcx_create", {"path": p}), done("created")])
    check("the fixture ran once and the file exists", flag("pcx_create") == 1 and os.path.exists(p))
    check("result ok, data.verified True, verification status verified", r.ok and r.data["verified"] is True and r.data["verification"]["status"] == "verified", str(r.data.get("verification")))
    check("the Goal is SUCCEEDED (verified success)", status_of_goal(r) == "succeeded", status_of_goal(r))
    check("each step records its verification status", r.data["steps"][0]["verification"] == "verified")
    check("no 'couldn't verify' hedge in the speech", "couldn't verify" not in r.speech and "did not" not in r.speech, r.speech)

    scenario("C2: plan.run — the tool claimed success, the world says no -> failed, in every field")
    reset()
    p = target("c2.txt")
    r, pl = await run_plan_goal("Create the file " + p, [call("test.pcx_liar", {"path": p}), done("created it")])
    check("the liar ran, nothing was created", flag("pcx_liar") == 1 and not os.path.exists(p))
    check("result ok is False", r.ok is False)
    check("data.verified is False and the verification says failed", r.data["verified"] is False and r.data["verification"]["status"] == "failed")
    check("the speech states that it did not take effect (and does not claim success)", "take effect" in r.speech or "did not check out" in r.speech or "couldn't confirm" in r.speech, r.speech)
    check("the Goal is not SUCCEEDED", status_of_goal(r) != "succeeded", status_of_goal(r))
    check("the step is reported failed with its error", r.data["steps"][0]["ok"] is False and r.data["steps"][0]["error"] == "postcondition_failed")

    scenario("C3: plan.run — a state change nobody can read back is UNVERIFIED, goal PARTIAL")
    reset()
    r, _ = await run_plan_goal("Create the file report.txt", [call("test.pcx_noverify"), done("did it")])
    check("the tool ran once", flag("pcx_noverify") == 1)
    check("ok stays True (nothing failed)", r.ok is True)
    check("data.verified is False — the run does NOT claim verified success", r.data["verified"] is False)
    check("verification.status is 'unverified' and names the tool", r.data["verification"]["status"] == "unverified" and "test.pcx_noverify" in r.data["verification"]["unverified"][0], str(r.data["verification"]))
    check("the speech says the result could not be verified", "couldn't verify" in r.speech and "unconfirmed" in r.speech, r.speech)
    rec = goals_mod.get(r.data["goal_id"])
    check("the Goal is PARTIAL, not SUCCEEDED", rec.status is goals_mod.GoalStatus.PARTIAL, rec.status.value)
    check("...with the verdict 'uncertain' and a reason saying why", rec.contract.final_verdict == "uncertain" and "could not be verified" in rec.failure_reason, f"{rec.contract.final_verdict!r} {rec.failure_reason!r}")

    scenario("C4: a read-only goal is untouched by verification")
    reset()
    r, _ = await run_plan_goal("What time is it?", [call("system.time"), done("it is the time")])
    check("no 'verification' key in the data; verified stays True", "verification" not in r.data and r.data["verified"] is True)
    check("the goal succeeded exactly as before", r.ok and status_of_goal(r) in ("succeeded", "partial"), status_of_goal(r))

    scenario("C5: a DESTRUCTIVE action stays confirmation-protected — verification adds no way around it")
    reset()
    victim = newfile("c5_a.txt")
    checks_before = []
    r, pl = await run_plan_goal("Delete the file " + victim, [call("test.pcx_delete", {"path": victim}), done("deleted")], approve=False)
    check("the confirmation prompt was raised exactly once", flag("confirm_prompts") == 1)
    check("declined -> the delete NEVER ran and the file is still there", flag("pcx_delete") == 0 and os.path.exists(victim))
    check("...the step is a confirmation_declined, not a failed post-condition", r.data["steps"][0]["error"] == "confirmation_declined")
    check("...and nothing was verified (a call that never ran has no state to read back)", "verification" not in r.data["steps"][0] and "verification" not in r.data)
    check("...the plan stopped without replanning around the decline (one model call)", pl.calls == 1 and not r.ok)

    reset()
    victim = newfile("c5_b.txt")
    r, _ = await run_plan_goal("Delete the file " + victim, [call("test.pcx_delete", {"path": victim}), done("deleted")], approve=True)
    check("approved: it asked once, ran once, and the delete was then VERIFIED (file gone)",
          flag("confirm_prompts") == 1 and flag("pcx_delete") == 1 and not os.path.exists(victim) and r.data["verification"]["status"] == "verified", str(r.data.get("verification")))

    reset()
    victim = newfile("c5_c.txt")
    saved = dict(CFG.permissions.overrides)
    CFG.permissions.overrides = {**saved, "test.pcx_delete": "deny"}
    try:
        r, _ = await run_plan_goal("Delete the file " + victim, [call("test.pcx_delete", {"path": victim}), done("deleted")], approve=True)
    finally:
        CFG.permissions.overrides = saved
    check("a policy DENY still wins: never asked, never ran, never verified", flag("confirm_prompts") == 0 and flag("pcx_delete") == 0 and os.path.exists(victim) and "verification" not in r.data)

    reset()
    victim = newfile("c5_d.txt")
    r, _ = await run_plan_goal("Delete the file " + victim, [call("test.pcx_delete", {"path": victim}), done("deleted")], approve=True, actor="scheduler")
    check("an unattended actor still cannot run an L2 delete: never ran, never verified", flag("pcx_delete") == 0 and os.path.exists(victim) and "verification" not in r.data)

    reset()
    victim = newfile("c5_e.txt")
    r, _ = await run_plan_goal("Inspect the folder", [call("test.pcx_delete", {"path": victim}), done("x")], approve=True)
    check("a delete on a read-only goal is an intent_mismatch: never asked, never ran, never verified",
          flag("confirm_prompts") == 0 and flag("pcx_delete") == 0 and os.path.exists(victim) and "verification" not in r.data)


# =============================================================================================
# D — the real verifiers against a scripted fake machine
# =============================================================================================


class FakeMachine:
    """Stands in for `verify.READERS`: a small mutable machine. Every attribute a real reader
    returns is here; None means "cannot be read on this machine"."""

    def __init__(self) -> None:
        self.vol: int | None = 50
        self.muted_: bool | None = False
        self.bright: int | None = 60
        self.plan_name: str | None = "power scheme guid: 381b4222 (balanced)"
        self.wins: list[dict] | None = []
        self.fg: tuple[int, str] | None = (1, "Editor")
        self.states: dict[int, dict] = {}
        self.mem: list[str] | None = []
        self.job_rows: dict[int, object] = {}
        self.jobs_readable = True
        self.notes: list[str] | None = []
        self.clip: str | None = ""
        self.wifi: bool | None = True
        self.alive: set[int] = set()
        self.named: dict[str, list[int]] = {}

    def volume_pct(self): return self.vol
    def muted(self): return self.muted_
    def brightness_pct(self): return self.bright
    def power_plan_name(self): return self.plan_name
    def windows(self): return None if self.wins is None else list(self.wins)

    def match_window(self, app):
        for w in self.wins or []:
            if app.lower() in w["title"].lower():
                return w
        return None

    def foreground(self): return self.fg

    def resolve_window(self, app):
        w = self.match_window(app) if app else None
        return (w["hwnd"], w["title"]) if w else (self.fg if not app else None)

    def window_state(self, hwnd): return self.states.get(hwnd)
    def memory_values(self): return None if self.mem is None else list(self.mem)

    def job_by_id(self, job_id):
        if not self.jobs_readable:
            return False
        return self.job_rows.get(int(job_id))

    def job_by_name(self, name):
        if not self.jobs_readable:
            return False
        return next((j for j in self.job_rows.values() if j.name.lower() == name.lower()), None)

    def jobs(self): return list(self.job_rows.values()) if self.jobs_readable else None
    def notes_lines(self): return None if self.notes is None else list(self.notes)
    def clipboard_text(self): return self.clip
    def wifi_up(self): return self.wifi
    def running_pids(self, pids): return {p for p in pids if p in self.alive}
    def pids_named(self, name): return list(self.named.get(name.lower().removesuffix(".exe"), []))


@dataclasses.dataclass
class FakeJob:
    id: int
    name: str
    enabled: bool = True
    trigger_spec: dict = dataclasses.field(default_factory=dict)


def instant(tool: str) -> None:
    """Make one real verifier non-settling for the matrix (settling is tested separately)."""
    verify.VERIFIERS[tool] = dataclasses.replace(verify.VERIFIERS[tool], settle_s=0.0)


async def run_verifier(tool: str, args: dict, data: dict | None = None, *, mutate=None):
    """prepare -> (the 'tool' mutates the fake machine) -> verify_call, exactly as _run_step does."""
    before = await verify.capture_before(tool, args, timeout_s=2.0)
    if mutate:
        mutate()
    return await verify.verify_call(tool, args, data or {}, before, timeout_s=2.0)


async def section_d() -> None:
    saved_readers = verify.READERS
    saved_verifiers = dict(verify.VERIFIERS)
    m = FakeMachine()
    verify.READERS = m
    for name in list(verify.VERIFIERS):
        if not name.startswith("test."):
            instant(name)
    try:
        scenario("D1: volume — set / up / down / mute against the read-back level")
        m.vol = 50
        v = await run_verifier("system.volume.set", {"level": 30}, mutate=lambda: setattr(m, "vol", 30))
        check("set 30, the machine reads 30 -> VERIFIED", v.status is V.VERIFIED)
        m.vol = 50
        v = await run_verifier("system.volume.set", {"level": 30}, mutate=lambda: setattr(m, "vol", 50))
        check("set 30, the machine still reads 50 -> FAILED (the tool said 'Volume set to 30' — the world says no)", v.status is V.FAILED and "50%" in v.reason, v.reason)
        m.vol = None
        v = await run_verifier("system.volume.set", {"level": 30})
        check("the level cannot be read here -> UNVERIFIED, not failed", v.status is V.UNVERIFIED)
        m.vol = 50
        v = await run_verifier("system.volume.up", {"amount": 10}, mutate=lambda: setattr(m, "vol", 60))
        check("up 10 from 50, machine reads 60 -> VERIFIED (baseline read BEFORE the call)", v.status is V.VERIFIED)
        m.vol = 50
        v = await run_verifier("system.volume.up", {}, mutate=lambda: setattr(m, "vol", 60))
        check("up with the DEFAULT amount (10) is checked against the default", v.status is V.VERIFIED)
        m.vol = 95
        v = await run_verifier("system.volume.up", {"amount": 10}, mutate=lambda: setattr(m, "vol", 100))
        check("up from 95 clamps to 100 -> VERIFIED (the expectation clamps like the skill)", v.status is V.VERIFIED)
        m.vol = 50
        v = await run_verifier("system.volume.down", {"amount": 20}, mutate=lambda: setattr(m, "vol", 50))
        check("down 20, nothing changed -> FAILED", v.status is V.FAILED)
        m.vol = 50
        v = await run_verifier("system.volume.set", {"level": 30}, mutate=lambda: setattr(m, "vol", 31))
        check("±1 rounding tolerance: 31 for a requested 30 -> VERIFIED", v.status is V.VERIFIED)
        m.vol = 50
        v = await run_verifier("system.volume.set", {"level": 30}, mutate=lambda: setattr(m, "vol", 35))
        check("...but 35 for a requested 30 -> FAILED", v.status is V.FAILED)
        m.muted_ = False
        v = await run_verifier("system.volume.mute", {"state": True}, mutate=lambda: setattr(m, "muted_", True))
        check("mute, machine reports muted -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("system.volume.mute", {"state": True}, mutate=lambda: setattr(m, "muted_", False))
        check("mute, machine reports NOT muted -> FAILED", v.status is V.FAILED)
        v = await run_verifier("system.volume.mute", {}, mutate=lambda: setattr(m, "muted_", True))
        check("mute with the default state (True) is checked as muted", v.status is V.VERIFIED)
        m.muted_ = None
        v = await run_verifier("system.volume.mute", {"state": False})
        check("mute state unreadable -> UNVERIFIED", v.status is V.UNVERIFIED)

        scenario("D2: brightness / power plan / wifi")
        m.bright = 60
        v = await run_verifier("system.brightness.set", {"level": 80}, mutate=lambda: setattr(m, "bright", 80))
        check("brightness set 80 -> reads 80 -> VERIFIED", v.status is V.VERIFIED)
        m.bright = 60
        v = await run_verifier("system.brightness.up", {"amount": 20}, mutate=lambda: setattr(m, "bright", 80))
        check("brightness up 20 from 60 -> reads 80 -> VERIFIED", v.status is V.VERIFIED)
        m.bright = 60
        v = await run_verifier("system.brightness.down", {"amount": 20}, mutate=lambda: setattr(m, "bright", 60))
        check("brightness down 20, unchanged -> FAILED", v.status is V.FAILED)
        m.bright = None
        v = await run_verifier("system.brightness.set", {"level": 80})
        check("a display that cannot report brightness -> UNVERIFIED (never pretended)", v.status is V.UNVERIFIED)
        m.bright = None
        v = await run_verifier("system.brightness.up", {"amount": 20})
        check("...and a delta with no baseline is UNVERIFIED too", v.status is V.UNVERIFIED and "baseline" in v.reason)
        m.plan_name = "power scheme guid: 381b4222 (balanced)"
        v = await run_verifier("system.power_plan", {"plan": "high performance"}, mutate=lambda: setattr(m, "plan_name", "power scheme guid: 8c5e7fda (high performance)"))
        check("power plan -> the active scheme now says high performance -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("system.power_plan", {"plan": "power saver"})
        check("power plan requested but the active scheme did not change -> FAILED", v.status is V.FAILED)
        v = await run_verifier("system.power_plan", {"plan": ""})
        check("power plan in REPORT mode (no plan) has nothing to verify -> None", v is None)
        m.wifi = True
        v = await run_verifier("network.wifi.toggle", {"enable": False}, mutate=lambda: setattr(m, "wifi", False))
        check("wifi off -> interface down -> VERIFIED", v.status is V.VERIFIED)
        m.wifi = True
        v = await run_verifier("network.wifi.toggle", {"enable": False})
        check("wifi off but the interface is still up -> FAILED", v.status is V.FAILED)
        m.wifi = False
        v = await run_verifier("network.wifi.toggle", {"enable": True})
        check("wifi ON but still not up: not evidence of failure (an idle adapter reads down) -> UNVERIFIED, not FAILED", v.status is V.UNVERIFIED)
        m.wifi = None
        v = await run_verifier("network.wifi.toggle", {"enable": True})
        check("no Wi-Fi interface readable -> UNVERIFIED", v.status is V.UNVERIFIED)

        scenario("D3: apps and windows")
        m.wins = [{"hwnd": 11, "title": "Untitled - Notepad", "pid": 5, "process": "notepad.exe"}]
        v = await run_verifier("apps.open", {"app": "notepad"}, {"app": "notepad"})
        check("apps.open: a window for the app is visible -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("apps.open", {"app": "spotify"}, {"app": "spotify"})
        check("apps.open: no window (yet) -> UNVERIFIED — a slow starter is not proof of failure", v.status is V.UNVERIFIED and "no window" in v.reason, v.reason)
        v = await run_verifier("apps.close", {"app": "notepad"}, mutate=lambda: setattr(m, "wins", []))
        check("apps.close: the window that WAS open is gone -> VERIFIED", v.status is V.VERIFIED)
        m.wins = [{"hwnd": 11, "title": "Untitled - Notepad", "pid": 5, "process": "notepad.exe"}]
        v = await run_verifier("apps.close", {"app": "notepad"})
        check("apps.close: the same window is still there -> FAILED (it may be waiting on a save prompt)", v.status is V.FAILED and "still open" in v.reason)
        m.wins = [{"hwnd": 11, "title": "Notepad", "pid": 5, "process": "notepad.exe"}, {"hwnd": 12, "title": "Notepad (2)", "pid": 6, "process": "notepad.exe"}]
        v = await run_verifier("apps.close", {"app": "Notepad"}, mutate=lambda: setattr(m, "wins", [m.wins[1]]))
        check("apps.close is by WINDOW HANDLE: closing one of two similar windows verifies (the other does not fool it)", v.status is V.VERIFIED)
        m.wins = []
        v = await run_verifier("apps.close", {"app": "ghost"})
        check("apps.close where no window was identified beforehand -> UNVERIFIED", v.status is V.UNVERIFIED)
        m.fg = (11, "Untitled - Notepad")
        v = await run_verifier("apps.focus", {"app": "notepad"}, {"title": "Untitled - Notepad"})
        check("apps.focus: the foreground window is the target -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("apps.focus", {"app": "chrome"}, {"title": "Google Chrome"})
        check("apps.focus: a different window is in front -> FAILED", v.status is V.FAILED)
        m.wins = [{"hwnd": 21, "title": "Doc", "pid": 1, "process": "x.exe"}]
        m.fg = (21, "Doc")
        m.states[21] = {"exists": True, "zoomed": False, "iconic": False}
        v = await run_verifier("window.maximize", {"app": "doc"}, mutate=lambda: m.states.__setitem__(21, {"exists": True, "zoomed": True, "iconic": False}))
        check("window.maximize -> the window reads as maximized -> VERIFIED", v.status is V.VERIFIED)
        m.states[21] = {"exists": True, "zoomed": False, "iconic": False}
        v = await run_verifier("window.minimize", {"app": "doc"})
        check("window.minimize but it is not minimized -> FAILED", v.status is V.FAILED)
        m.states[21] = {"exists": True, "zoomed": True, "iconic": False}
        v = await run_verifier("window.restore", {"app": "doc"}, mutate=lambda: m.states.__setitem__(21, {"exists": True, "zoomed": False, "iconic": False}))
        check("window.restore -> normal size -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("window.maximize", {"app": "nothing-like-this"})
        check("a window that could not be identified beforehand -> UNVERIFIED", v.status is V.UNVERIFIED)
        m.alive = {100, 101}
        m.named = {"chrome": [100, 101]}
        v = await run_verifier("process.kill", {"name": "chrome.exe"}, {"pids": [100, 101]}, mutate=lambda: setattr(m, "alive", set()))
        check("process.kill: the pids are gone -> VERIFIED", v.status is V.VERIFIED)
        m.alive = {100, 101}
        v = await run_verifier("process.kill", {"name": "chrome.exe"}, {"pids": [100, 101]}, mutate=lambda: setattr(m, "alive", {101}))
        check("process.kill: one pid still running -> FAILED", v.status is V.FAILED and "1 still running" in v.reason)

        scenario("D4: memory / schedule / notes / clipboard / files")
        m.mem = ["my sister is priya"]
        v = await run_verifier("memory.remember", {"text": "x"}, {"remembered": "the code is 42"}, mutate=lambda: m.mem.append("the code is 42"))
        check("memory.remember: the value is now in the store -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("memory.remember", {"text": "x"}, {"remembered": "never stored"})
        check("memory.remember: reported stored but not in the store -> FAILED", v.status is V.FAILED)
        v = await run_verifier("memory.remember", {"text": "what's my name"}, {"rerouted": True})
        check("memory.remember rerouted to a recall (nothing stored) -> nothing to verify", v is None)
        m.mem = ["a", "b", "b"]
        v = await run_verifier("memory.forget", {"query": "b"}, {"forgotten": ["b", "b"]}, mutate=lambda: setattr(m, "mem", ["a"]))
        check("memory.forget: both copies gone -> VERIFIED", v.status is V.VERIFIED)
        m.mem = ["a", "b", "b"]
        v = await run_verifier("memory.forget", {"query": "b"}, {"forgotten": ["b", "b"]}, mutate=lambda: setattr(m, "mem", ["a", "b"]))
        check("memory.forget: a copy the tool said it forgot is still there -> FAILED", v.status is V.FAILED)
        m.mem = None
        v = await run_verifier("memory.forget", {"query": "b"}, {"forgotten": ["b"]})
        check("memory store unreadable -> UNVERIFIED", v.status is V.UNVERIFIED)
        m.job_rows = {}
        v = await run_verifier("schedule.create", {}, {"id": 7}, mutate=lambda: m.job_rows.__setitem__(7, FakeJob(7, "morning")))
        check("schedule.create: the job id exists -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("schedule.create", {}, {"id": 8})
        check("schedule.create: reported id 8 but no such job -> FAILED", v.status is V.FAILED)
        m.jobs_readable = False
        v = await run_verifier("schedule.create", {}, {"id": 7})
        check("job store unreadable -> UNVERIFIED", v.status is V.UNVERIFIED)
        m.jobs_readable = True
        m.job_rows = {7: FakeJob(7, "morning")}
        v = await run_verifier("schedule.delete", {"name": "morning"}, {"deleted": "morning"}, mutate=lambda: m.job_rows.clear())
        check("schedule.delete: the job is gone -> VERIFIED", v.status is V.VERIFIED)
        m.job_rows = {7: FakeJob(7, "morning")}
        v = await run_verifier("schedule.delete", {"name": "morning"}, {"deleted": "morning"})
        check("schedule.delete: still scheduled -> FAILED", v.status is V.FAILED)
        m.job_rows = {7: FakeJob(7, "morning", enabled=True)}
        v = await run_verifier("schedule.toggle", {"name": "morning", "enable": False}, {"name": "morning"}, mutate=lambda: setattr(m.job_rows[7], "enabled", False))
        check("schedule.toggle(pause): the job is now paused -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("schedule.toggle", {"name": "morning", "enable": True}, {"name": "morning"})
        check("schedule.toggle(resume) but it is still paused -> FAILED", v.status is V.FAILED)
        m.job_rows = {9: FakeJob(9, "timer 10:05", trigger_spec={"run_at": "2026-09-21T10:05:00"})}
        v = await run_verifier("timer.set", {"duration": "10 minutes"}, {"fires_at": "2026-09-21T10:05:00"})
        check("timer.set: a one-shot job at that time exists -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("timer.set", {"duration": "10 minutes"}, {"fires_at": "2030-01-01T00:00:00"})
        check("timer.set: no job at that time -> FAILED", v.status is V.FAILED)
        m.notes = ["- [2026-09-21 10:00] old"]
        v = await run_verifier("notes.add", {"text": "x"}, {"note": "buy milk"}, mutate=lambda: m.notes.append("- [2026-09-21 10:01] buy milk"))
        check("notes.add: the note is the new last line -> VERIFIED", v.status is V.VERIFIED)
        m.notes = ["- [2026-09-21 10:00] old"]
        v = await run_verifier("notes.add", {"text": "x"}, {"note": "buy milk"})
        check("notes.add: not written -> FAILED", v.status is V.FAILED)
        m.clip = "old"
        v = await run_verifier("clipboard.write", {"text": "hello"}, mutate=lambda: setattr(m, "clip", "hello"))
        check("clipboard.write: the clipboard holds the text -> VERIFIED", v.status is V.VERIFIED)
        v = await run_verifier("clipboard.write", {"text": "hello"}, mutate=lambda: setattr(m, "clip", "other"))
        check("clipboard.write: it holds something else -> FAILED", v.status is V.FAILED)
        m.clip = None
        v = await run_verifier("clipboard.write", {"text": "hello"})
        check("clipboard unreadable -> UNVERIFIED", v.status is V.UNVERIFIED)
        f = newfile("dl.bin", "payload")
        v = await run_verifier("web.download", {"url": "x"}, {"path": f})
        check("web.download: the file exists and is non-empty -> VERIFIED (a real file-create read-back)", v.status is V.VERIFIED)
        v = await run_verifier("web.download", {"url": "x"}, {"path": str(TMP / "never.bin")})
        check("web.download: reported a path that does not exist -> FAILED", v.status is V.FAILED)
        v = await run_verifier("web.download", {"url": "x"}, {})
        check("web.download: no path reported -> UNVERIFIED", v.status is V.UNVERIFIED)

        scenario("D5: settling — a state that is only reached a moment later")
        for name in ("apps.close", "system.volume.set"):
            verify.VERIFIERS[name] = saved_verifiers[name]  # the real settle windows again
        m.wins = [{"hwnd": 31, "title": "Slow App", "pid": 1, "process": "s.exe"}]

        async def close_later():
            await asyncio.sleep(0.5)
            m.wins = []

        before = await verify.capture_before("apps.close", {"app": "slow"}, timeout_s=4.0)
        t0 = time.perf_counter()
        task = asyncio.ensure_future(close_later())
        v = await verify.verify_call("apps.close", {"app": "slow"}, {}, before, timeout_s=4.0)
        await task
        check("a window that closes 0.5 s later is VERIFIED thanks to settle polling", v.status is V.VERIFIED and 0.4 < time.perf_counter() - t0 < 3.0, f"{v.status.value} {time.perf_counter() - t0:.2f}s")
        m.wins = [{"hwnd": 32, "title": "Stuck App", "pid": 1, "process": "s.exe"}]
        before = await verify.capture_before("apps.close", {"app": "stuck"}, timeout_s=4.0)
        t0 = time.perf_counter()
        v = await verify.verify_call("apps.close", {"app": "stuck"}, {}, before, timeout_s=4.0)
        took = time.perf_counter() - t0
        check("a window that never closes: FAILED, only after the bounded settle window (3 s, not forever)", v.status is V.FAILED and 2.5 < took < 4.5, f"{v.status.value} {took:.2f}s")
        m.vol = 50
        t0 = time.perf_counter()
        v = await verify.verify_call("system.volume.set", {"level": 20}, {}, {}, timeout_s=4.0)
        check("a mismatch that never resolves is bounded by the verifier's own settle time (0.6 s)", v.status is V.FAILED and time.perf_counter() - t0 < 1.6)
    finally:
        verify.READERS = saved_readers
        verify.VERIFIERS.clear()
        verify.VERIFIERS.update(saved_verifiers)


# =============================================================================================
# E — coverage and static invariants
# =============================================================================================


def section_e() -> None:
    scenario("E1: coverage — every real state-changing skill is verified or EXPLICITLY unverifiable")
    non_l0 = [s.name for s in REGISTRY.all() if s.tier != "L0" and not s.name.startswith("test.")]
    has_v = [n for n in non_l0 if n in verify.VERIFIERS]
    explicit = [n for n in non_l0 if n in verify.UNVERIFIABLE]
    neither = [n for n in non_l0 if n not in verify.VERIFIERS and n not in verify.UNVERIFIABLE]
    both = [n for n in non_l0 if n in verify.VERIFIERS and n in verify.UNVERIFIABLE]
    check("no real state-changing skill is in neither table (a new skill must choose)", neither == [], str(neither))
    check("no skill is claimed both verifiable and unverifiable", both == [], str(both))
    stale = [n for n in list(verify.VERIFIERS) + list(verify.UNVERIFIABLE) if not n.startswith("test.") and REGISTRY.get(n) is None]
    check("no table entry names a skill that does not exist", stale == [], str(stale))
    check("every UNVERIFIABLE entry carries a real reason", all(len(r) > 15 for r in verify.UNVERIFIABLE.values()))
    print(f"    coverage: {len(non_l0)} real state-changing skills = {len(has_v)} verifiable + {len(explicit)} explicitly unverifiable "
          f"({100 * len(has_v) // len(non_l0)}% have a read-back)")
    by_tier: dict[str, list[int]] = {}
    for s in REGISTRY.all():
        if s.tier != "L0" and not s.name.startswith("test."):
            by_tier.setdefault(s.tier, [0, 0])[0 if s.name in verify.VERIFIERS else 1] += 1
    print("    by tier (verifiable / not):", {t: tuple(v) for t, v in sorted(by_tier.items())})
    unver = list(explicit)
    v = verify.no_verifier(unver[0])
    check("a tool in UNVERIFIABLE yields UNVERIFIED with its reason", v.status is V.UNVERIFIED and verify.UNVERIFIABLE[unver[0]] in v.reason)
    check("a completely unknown tool also yields UNVERIFIED (never verified by default)", verify.no_verifier("nobody.knows").status is V.UNVERIFIED)
    check("the destructive real skills that CAN be read back are covered (apps.close, process.kill, schedule.delete, memory.forget)",
          all(n in verify.VERIFIERS for n in ("apps.close", "process.kill", "schedule.delete", "memory.forget")))

    scenario("E2: verification only READS, and sits after the executor")
    src = inspect.getsource(verify)
    banned = ("EXECUTOR", "SetMasterVolume", "SetMute(", ".kill(", ".terminate(", "os.remove", ".unlink(", "rmtree",
              "write_text", "startfile", "Popen(", "SetForegroundWindow", "ShowWindow", "PostMessage", "SetClipboard",
              "EmptyClipboard", "INSERT INTO", "UPDATE ", "DELETE FROM", "set_enabled", "jobs.delete", "memory.forget(")
    hits = [b for b in banned if b in src]
    check("verify.py contains no call that writes, kills, closes, or dispatches a tool", hits == [], str(hits))
    imports = {n.module for n in __import__("ast").walk(__import__("ast").parse(src)) if isinstance(n, __import__("ast").ImportFrom) and n.module}
    check("verify.py imports nothing from permissions or the orchestrator (it cannot authorize or run anything)",
          not any(("permissions" in (m or "")) or ("orchestrator" in (m or "")) for m in imports), str(imports))
    orch_src = inspect.getsource(Orchestrator._run_step)
    check("in _run_step the read-back is attached only when the runner returned ok", "if verifying and result.ok:" in orch_src)
    check("...and the baseline read happens before the runner call", orch_src.index("capture_before") < orch_src.index("self.runner("))
    check("the executor is untouched: permissions.py has no reference to verify", "verify" not in inspect.getsource(__import__("friday.permissions", fromlist=["x"])))

    scenario("E3: registry facts")
    check("every verifier key is a real skill or a test.* fixture", all(n.startswith("test.") or REGISTRY.get(n) is not None for n in verify.VERIFIERS))
    check("Verifier is immutable (frozen dataclass)", dataclasses.is_dataclass(verify.Verifier) and verify.Verifier.__dataclass_params__.frozen)


# =============================================================================================
# F — read-only smoke of the REAL readers on this machine
# =============================================================================================


_PROBE = r"""
import json, sys
sys.path.insert(0, ".")
from friday import store, verify
from friday.registry import REGISTRY
REGISTRY.discover()
R = verify.READERS
out = {}
with store.use_temp_db():
    # brightness BEFORE volume: this machine's COM stack (pycaw then screen_brightness_control) dies with a native
    # access violation ~1 run in 3 when the ORIGINAL skills' helpers are called in that order (measured with
    # friday.skills.system._get_volume_pct + friday.skills.hardware._get_brightness alone; never in this order)
    for name in ("brightness_pct", "volume_pct", "muted", "power_plan_name", "windows", "foreground",
                 "memory_values", "jobs", "notes_lines", "clipboard_text", "wifi_up"):
        try:
            val = getattr(R, name)()
            out[name] = {"raised": None, "readable": val is not None}
        except Exception as exc:
            out[name] = {"raised": type(exc).__name__, "readable": False}
    out["match_window"] = R.match_window("zzz-no-such-window-zzz") is None
    out["running_pids"] = R.running_pids([2**31 - 3]) == set()
    out["pids_named"] = R.pids_named("zzz-no-such-process") == []
print("PROBE" + json.dumps(out))
"""


def section_f() -> None:
    scenario("F: the REAL readers on this machine (read-only; measures how much of the coverage is live here)")
    import json
    import subprocess

    # A subprocess on purpose: the Windows audio / brightness COM stack this machine has can die with a
    # native access violation (a known, intermittent, pre-existing flake — see PLAN.md Phase 19); that must
    # never take the whole suite down, and a Python exception from a reader is still caught below.
    try:
        p = subprocess.run([sys.executable, "-c", _PROBE], cwd=str(H.ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90)
    except subprocess.TimeoutExpired:
        check("real readers: the probe finished within 90 s", False)
        return
    line = next((ln for ln in p.stdout.splitlines() if ln.startswith("PROBE")), "")
    if not line:
        crashed_natively = p.returncode != 0 and "Traceback" not in p.stderr
        print(f"    NOTE: the probe process died (exit {p.returncode}) — the known native COM flake, not a reader exception")
        check("real readers: the probe died natively (no Python traceback) — environment flake, not a reader bug", crashed_natively, p.stderr[-300:])
        return
    res = json.loads(line[len("PROBE"):])
    readers = {k: v for k, v in res.items() if isinstance(v, dict)}
    for name, r in readers.items():
        check(f"real reader {name}() never raises", r["raised"] is None, str(r["raised"]))
    print("    readable on this machine:", {k: v["readable"] for k, v in readers.items()})
    check("match_window for a nonexistent app -> None", res["match_window"] is True)
    check("running_pids for an impossible pid -> empty set", res["running_pids"] is True)
    check("pids_named for an impossible name -> empty list", res["pids_named"] is True)
    check("the readers are attributes of ONE seam (verify.READERS), so a test can replace them", isinstance(verify.READERS, verify._Readers))


async def main() -> int:
    t0 = time.perf_counter()
    _register_fs_verifiers()
    _register_pipeline_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    saved_cfg = (CFG.desktop_observer.enabled, CFG.planner.max_replans, CFG.planner.verify_timeout_s)
    try:
        with store.use_temp_db():
            section_a()
            await section_b()
            await section_c()
            await section_d()
            section_e()
            section_f()
    finally:
        CFG.permissions.overrides = saved
        CFG.desktop_observer.enabled, CFG.planner.max_replans, CFG.planner.verify_timeout_s = saved_cfg
        CFG.planner.postcondition_verify = True
        shutil.rmtree(TMP, ignore_errors=True)
    return H.finish("POST-CONDITION VERIFICATION SCORECARD", time.perf_counter() - t0, min_assertions=150, min_scenarios=20)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
