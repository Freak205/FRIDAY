"""Phase 92 -- a `shell.run` that was NOT STARTED is not retried as the identical operation.

Phase 91 made `shell.run` return `ok=False, data={"status": "not_started", "reason": no_job|assign_failed|resume_failed}` when the
process tree could not be owned before the command could run. The repeat guard only ever blocked an IMMEDIATE re-issue of a
state-changing call (anything in between made it a "real second press"), and the "twice" quota path re-ran a call that had not
succeeded. For a command that never started, the setup failure is the same for the identical call, so:

  A  `find_prior_attempt` (pure), once per reason: the identical call (however re-spelled) is a duplicate even after other steps
     ran; a different command, or a different shell, is not; the same shape of result WITHOUT `not_started` (ordinary failure,
     timeout, cancelled, unknown_outcome, success) keeps the Phase 5 rule exactly (immediate = blocked, after a step = allowed)
  B  the real `shell.run`, one run per reason: the result really has that shape (never ran: no returncode/output, never
     cancelled/timeout/unknown_outcome) and the speech says it was not run
  C  the real `run_goal` with a fixture that returns that shape: identical retry after an intervening read never runs, the block
     says the earlier attempt FAILED, a different command still runs, and the user's "twice" does not re-run it
  D  controls: ordinary failures, timeouts, cancelled and unknown_outcome results are retried exactly as before

Deterministic: scripted planner replies; the shell tool is a `test.ns_run` fixture or the real `shell.run` with
`shellproc.run_owned` replaced (no process is started); every real non-L0 skill is hard-denied; no Ollama; data/ is untouched.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated

import phase21_common as H
from phase21_common import ScriptedPlanner, call, check, done, scenario

from friday import intent, shellproc, store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, Orchestrator, PlanStep, _never_started, _tool_specs, find_prior_attempt  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.skills import shell  # noqa: E402

REGISTRY.discover()

REASONS = ("no_job", "assign_failed", "resume_failed")
CALLS: list[tuple[str, dict]] = []
MODE = {"kind": "no_job"}   # what the fixture returns: a reason above, or one of the control kinds below
TIER = {"shell.run": "L2", "test.ns_run": "L1", "test.ns_other": "L0"}


def _fixture_result(kind: str) -> SkillResult:
    if kind in REASONS:
        return SkillResult(speech="I didn't run that command: I couldn't set it up so that I could reliably stop it.", ok=False,
                           data={"status": "not_started", "reason": kind})
    if kind == "ordinary":
        return SkillResult(speech="Exit code 1. boom", ok=False, data={"stdout": "", "stderr": "boom", "returncode": 1})
    if kind in ("timeout", "cancelled", "unknown_outcome"):
        return SkillResult(speech=f"shell: {kind}", ok=False, data={"stdout": "", "stderr": "", "returncode": None, "status": kind})
    return SkillResult(speech="Done, no output.", ok=True, data={"stdout": "", "stderr": "", "returncode": 0})


def _register_fixtures() -> None:
    @skill(name="test.ns_run", tier="L1", action="modify", description="stand-in for shell.run (returns the configured outcome)")
    def _run(command: Annotated[str, "the command"], shell: Annotated[str, "powershell or cmd"] = "powershell") -> SkillResult:
        return _fixture_result(MODE["kind"])

    @skill(name="test.ns_other", tier="L0", description="an unrelated read")
    def _other() -> SkillResult:
        return SkillResult(speech="ns_other: something else.")


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    return await EXECUTOR.run(tool, args, actor=actor)


def reset(kind: str = "no_job") -> None:
    CALLS.clear()
    INTEL.reset()
    MODE["kind"] = kind
    CFG.planner.structured_output = False
    CFG.planner.intent_guard = True
    CFG.planner.goal_coverage = True
    CFG.planner.semantic_repeat_guard = True
    CFG.planner.counted_repeats = True
    CFG.desktop_observer.enabled = False


async def drive(goal: str, replies: list[str]):
    CALLS.clear()
    names = [s.name for s in REGISTRY.all() if s.name != "plan.run"]
    orch = Orchestrator(tools=names, runner=runner, actor="test", llm_provider=ScriptedPlanner(replies),
                        tool_specs=_tool_specs(names), max_steps=10)
    return await orch.run_goal(goal, action_scope=intent.derive_scope(goal), max_replans=CFG.planner.max_replans)


def runs(tool: str = "test.ns_run") -> list[dict]:
    return [a for t, a in CALLS if t == tool]


def blocked(res) -> list[Observation]:
    return [o for o in res.observations if o.error == "repeated_call"]


def shell_obs(command: str, kind: str, shell_: str = "powershell") -> Observation:
    r = _fixture_result(kind)
    args = {"command": command, **({"shell": shell_} if shell_ != "powershell" else {})}
    return Observation(PlanStep("shell.run", args), r.ok, r.speech, r.data)


def read_obs() -> Observation:
    return Observation(PlanStep("test.ns_other", {}), True, "x", {})


def sh(command: str, shell_: str = "powershell") -> dict:
    return {"command": command, **({"shell": shell_} if shell_ != "powershell" else {})}


# =============================================================================================
# A -- the guard, pure
# =============================================================================================


def section_a() -> None:
    for reason in REASONS:
        scenario(f"A[{reason}]: an identical call is a duplicate however many steps ran since; another command is not")
        started = [shell_obs("ipconfig /all", reason)]
        check(f"{reason}: the result is recognised as never started", _never_started(started[0]))
        check(f"{reason}: immediate identical retry is blocked", find_prior_attempt(started, "shell.run", sh("ipconfig /all"), TIER) is not None)
        after = started + [read_obs(), read_obs()]
        hit = find_prior_attempt(after, "shell.run", sh("ipconfig /all"), TIER)
        check(f"{reason}: identical retry after two other steps is STILL blocked (it would meet the same setup failure)", hit is not None and hit[0] == 0)
        check(f"{reason}: ...re-spelled (case/space) it is the same call", find_prior_attempt(after, "shell.run", sh("  IPCONFIG   /all "), TIER) is not None)
        check(f"{reason}: a different command is allowed", find_prior_attempt(after, "shell.run", sh("hostname"), TIER) is None)
        check(f"{reason}: the same command in the other shell is a different approach and is allowed",
              find_prior_attempt(after, "shell.run", sh("ipconfig /all", "cmd"), TIER) is None)
        check(f"{reason}: a different working directory is a different call", find_prior_attempt(after, "shell.run", {**sh("ipconfig /all"), "cwd": str(Path.home())}, TIER) is None)

    scenario("A[controls]: results that are not `not_started` keep the Phase 5 rule exactly")
    for kind in ("ordinary", "timeout", "cancelled", "unknown_outcome", "ok"):
        obs = [shell_obs("ipconfig /all", kind)]
        check(f"{kind}: not treated as never started", not _never_started(obs[0]))
        check(f"{kind}: immediate identical retry is blocked (unchanged)", find_prior_attempt(obs, "shell.run", sh("ipconfig /all"), TIER) is not None)
        check(f"{kind}: identical retry after another step is allowed (unchanged)",
              find_prior_attempt(obs + [read_obs()], "shell.run", sh("ipconfig /all"), TIER) is None)
    ok_flag = Observation(PlanStep("shell.run", sh("x")), True, "x", {"status": "not_started"})
    check("an ok result is never 'never started', whatever its data says", not _never_started(ok_flag))
    check("an observation with no data is not 'never started'", not _never_started(Observation(PlanStep("shell.run", sh("x")), False, "x", None)))

    scenario("A[mixed]: only the matching call is affected")
    mix = [shell_obs("ipconfig /all", "no_job"), shell_obs("hostname", "ordinary"), read_obs()]
    check("the never-started command stays blocked behind an ordinary failed one", find_prior_attempt(mix, "shell.run", sh("ipconfig /all"), TIER) is not None)
    check("the ordinary failed command behind other steps stays retryable", find_prior_attempt(mix, "shell.run", sh("hostname"), TIER) is None)


# =============================================================================================
# B -- the real shell.run
# =============================================================================================


def section_b() -> None:
    scenario("B: the real shell.run reports each reason as a never-started result (run_owned replaced; nothing is spawned)")
    original = shellproc.run_owned
    try:
        for reason in REASONS:
            shellproc.run_owned = lambda argv, **kw: shellproc.Outcome(shellproc.NOT_STARTED, detail=reason)
            r = shell.run("echo hi", shell="cmd", cwd=str(Path(tempfile.gettempdir())))
            check(f"{reason}: ok False, status not_started, the reason is carried", r.ok is False and r.data == {"status": "not_started", "reason": reason}, str(r.data))
            check(f"{reason}: no returncode / output is claimed (it never ran)", "returncode" not in r.data and "stdout" not in r.data)
            check(f"{reason}: never cancelled / timeout / unknown_outcome", r.data.get("status") not in ("cancelled", "timeout", "unknown_outcome"))
            check(f"{reason}: the speech says it did not run", "didn't run" in r.speech, r.speech)
            ob = Observation(PlanStep("shell.run", {"command": "echo hi", "shell": "cmd"}), r.ok, r.speech, r.data)
            check(f"{reason}: the orchestrator's predicate recognises the real result", _never_started(ob))
    finally:
        shellproc.run_owned = original

    shellproc.run_owned = original
    o_new = shellproc._new_job
    shellproc._new_job = lambda: None   # the real no-job path: returns before Popen, so no process exists
    try:
        r = shell.run("echo hi", shell="cmd", cwd=str(Path(tempfile.gettempdir())))
    finally:
        shellproc._new_job = o_new
    check("end to end (real shellproc, no job object): not_started / no_job", r.ok is False and r.data == {"status": "not_started", "reason": "no_job"}, str(r.data))


# =============================================================================================
# C -- the real run_goal
# =============================================================================================


async def section_c() -> None:
    for reason in REASONS:
        scenario(f"C[{reason}]: run_goal -- the identical retry never runs, a different command does")
        reset(reason)
        res, = [await drive("Update big.txt", [call("test.ns_run", sh("ipconfig")), call("test.ns_other"), call("test.ns_run", sh("ipconfig")), done("x")])]
        check(f"{reason}: the command was attempted ONCE (the identical retry after a read did not run)", len(runs()) == 1, str(runs()))
        b = blocked(res)
        check(f"{reason}: the retry became a blocked observation, not an executed step", len(b) == 1 and b[0].data.get("status") == "already_tried")
        check(f"{reason}: the block tells the planner the earlier attempt FAILED", b and b[0].data.get("previous_ok") is False and "FAILED" in b[0].speech, b[0].speech if b else "")
        check(f"{reason}: the earlier observation still says it did not run (not 'executed', not 'unknown')",
              any(_never_started(o) and "didn't run" in o.speech and o.error == "" for o in res.observations if o.error != "repeated_call"))

        reset(reason)
        res = await drive("Update big.txt", [call("test.ns_run", sh("ipconfig")), call("test.ns_other"), call("test.ns_run", sh("hostname")), done("x")])
        check(f"{reason}: a genuinely different command still runs", [a["command"] for a in runs()] == ["ipconfig", "hostname"] and not blocked(res), str(runs()))

        reset(reason)
        res = await drive("Update big.txt", [call("test.ns_run", sh("ipconfig")), call("test.ns_run", sh("ipconfig")), call("test.ns_run", sh("ipconfig")), done("x")])
        check(f"{reason}: the immediate-retry protection is unchanged (one run, then the repeat streak stops the run)", len(runs()) == 1 and res.stopped == "repeated_action", str(res.stopped))

    scenario("C[twice]: the user's own count does not make a never-started command run again")
    reset("no_job")
    res = await drive("Update ipconfig twice", [call("test.ns_run", sh("ipconfig")), call("test.ns_run", sh("ipconfig")), done("x")])
    check("'twice' with a first attempt that never started: not re-run", len(runs()) == 1, str(runs()))
    check("...the retry was blocked", len(blocked(res)) == 1)

    scenario("C[twice-control]: 'twice' still lets an ordinary failed call run again (unchanged)")
    reset("ordinary")
    res = await drive("Update ipconfig twice", [call("test.ns_run", sh("ipconfig")), call("test.ns_run", sh("ipconfig")), done("x")])
    check("'twice' with an ordinary failure: the second attempt ran", len(runs()) == 2 and not blocked(res), str(runs()))


# =============================================================================================
# D -- controls through run_goal
# =============================================================================================


async def section_d() -> None:
    for kind in ("ordinary", "timeout", "cancelled", "unknown_outcome"):
        scenario(f"D[{kind}]: run_goal -- retry behaviour is exactly what it was")
        reset(kind)
        res = await drive("Update big.txt", [call("test.ns_run", sh("ipconfig")), call("test.ns_other"), call("test.ns_run", sh("ipconfig")), done("x")])
        check(f"{kind}: retry after a read still runs (2 attempts, nothing blocked)", len(runs()) == 2 and not blocked(res), str(runs()))
        reset(kind)
        res = await drive("Update big.txt", [call("test.ns_run", sh("ipconfig")), call("test.ns_run", sh("ipconfig")), done("x")])
        check(f"{kind}: an immediate identical retry is still blocked", len(runs()) == 1 and len(blocked(res)) == 1)


async def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    tmp = Path(tempfile.mkdtemp(prefix="friday_ns_"))
    try:
        with store.use_temp_db():
            section_a()
            section_b()
            await section_c()
            await section_d()
    finally:
        CFG.permissions.overrides = saved
        shutil.rmtree(tmp, ignore_errors=True)
    return H.finish("NOT-STARTED RETRY SCORECARD", time.perf_counter() - t0, min_assertions=80, min_scenarios=14)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
