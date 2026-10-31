"""Phase 93 -- a `shell.run` whose outcome is UNKNOWN is not repeated as the identical operation.

`shell.run` ends as `unknown_outcome` when a stop or the time limit was acted on but the process tree could not be confirmed gone:
the command may already have run (or still be running). It is neither done nor failed. The repeat guard only blocked an IMMEDIATE
re-issue (any step in between made it a "real second press"), the user's "twice" count re-ran a call that had not succeeded, and the
block wording said "Previous result (FAILED)". Now (Phase 92's mechanism, widened to this status):

  A  `find_prior_attempt` (pure): after `unknown_outcome` the identical call (however re-spelled) is a duplicate even after other
     steps; a different command, shell or working directory is not; `not_started` keeps its Phase 92 protection; ordinary failure,
     timeout, cancelled and success keep the Phase 5 rule (immediate = blocked, after a step = allowed)
  B  the real `shell.run` (run_owned replaced, nothing is spawned): both causes of UNKNOWN produce the shape the guard keys on, and
     the orchestrator's predicate tells it from every other result
  C  the real `run_goal`: identical retry after an intervening read never runs; the block says the outcome is UNKNOWN / may already
     have run (never "FAILED", never "ok"); a different command still runs; "twice" does not re-run it; a repeat streak still stops
  D  controls: ordinary failure / timeout / cancelled / not_started behave exactly as before, through run_goal

Deterministic: scripted planner replies; the orchestrator tests answer the real `shell.run` name from the injected runner (no command is executed);
the shell-skill test uses the real `shell.run` with `shellproc.run_owned` replaced; every real non-L0 skill is hard-denied; no Ollama; data/ is untouched.
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
from friday.orchestrator import (  # noqa: E402
    Observation, Orchestrator, PlanStep, _never_started, _outcome_unknown, _tool_specs, find_prior_attempt,
)
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.skills import shell  # noqa: E402

REGISTRY.discover()

CALLS: list[tuple[str, dict]] = []
MODE = {"kind": "unknown_stopped"}
LAST: dict = {}   # the scripted planner of the latest drive(), to read what the planner was shown
TIER = {"shell.run": "L2", "test.un_other": "L0"}
UNKNOWN_KINDS = ("unknown_stopped", "unknown_timeout")
OTHERS = ("ordinary", "timeout", "cancelled", "not_started", "ok")


def _fixture_result(kind: str) -> SkillResult:
    base = {"stdout": "", "stderr": "", "returncode": None}
    if kind == "unknown_stopped":
        return SkillResult(speech="That command was stopped, but I couldn't confirm that it and everything it started have ended.",
                           ok=False, data={**base, "status": "unknown_outcome", "reason": "tree_not_confirmed"})
    if kind == "unknown_timeout":
        return SkillResult(speech="That command was timed out after 60 seconds, but I couldn't confirm that it and everything it started have ended.",
                           ok=False, data={**base, "status": "unknown_outcome", "reason": "tree_not_confirmed"})
    if kind == "not_started":
        return SkillResult(speech="I didn't run that command: I couldn't set it up so that I could reliably stop it.", ok=False,
                           data={"status": "not_started", "reason": "no_job"})
    if kind == "ordinary":
        return SkillResult(speech="Exit code 1. boom", ok=False, data={"stdout": "", "stderr": "boom", "returncode": 1})
    if kind in ("timeout", "cancelled"):
        return SkillResult(speech=f"shell: {kind}", ok=False, data={**base, "status": kind, "ended": "forced"})
    return SkillResult(speech="Done, no output.", ok=True, data={"stdout": "", "stderr": "", "returncode": 0})


def _register_fixtures() -> None:
    @skill(name="test.un_other", tier="L0", description="an unrelated read")
    def _other() -> SkillResult:
        return SkillResult(speech="un_other: something else.")


async def runner(tool: str, args: dict, actor: str) -> SkillResult:
    CALLS.append((tool, dict(args)))
    if tool == "shell.run":   # the orchestrator's injected seam: nothing is executed, the configured outcome comes back
        return _fixture_result(MODE["kind"])
    return await EXECUTOR.run(tool, args, actor=actor)


def reset(kind: str = "unknown_stopped") -> None:
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
    scripted = ScriptedPlanner(replies)
    orch = Orchestrator(tools=names, runner=runner, actor="test", llm_provider=scripted,
                        tool_specs=_tool_specs(names), max_steps=10)
    LAST["scripted"] = scripted
    return await orch.run_goal(goal, action_scope=intent.derive_scope(goal), max_replans=CFG.planner.max_replans)


def runs() -> list[dict]:
    return [a for t, a in CALLS if t == "shell.run"]


def blocked(res) -> list[Observation]:
    return [o for o in res.observations if o.error == "repeated_call"]


def sh(command: str, shell_: str = "powershell", cwd: str = "") -> dict:
    return {"command": command, **({"shell": shell_} if shell_ != "powershell" else {}), **({"cwd": cwd} if cwd else {})}


def shell_obs(command: str, kind: str, **kw) -> Observation:
    r = _fixture_result(kind)
    return Observation(PlanStep("shell.run", sh(command, **kw)), r.ok, r.speech, r.data)


def read_obs() -> Observation:
    return Observation(PlanStep("test.un_other", {}), True, "x", {})


# =============================================================================================
# A -- the guard, pure
# =============================================================================================


def section_a() -> None:
    other_dir = str(Path(tempfile.gettempdir()))
    for kind in UNKNOWN_KINDS:
        scenario(f"A[{kind}]: an identical call is a duplicate however many steps ran since; another call is not")
        started = [shell_obs("ipconfig /all", kind)]
        check(f"{kind}: recognised as an unknown outcome", _outcome_unknown(started[0]))
        check(f"{kind}: ...and not as a never-started command", not _never_started(started[0]))
        check(f"{kind}: immediate identical retry is blocked", find_prior_attempt(started, "shell.run", sh("ipconfig /all"), TIER) is not None)
        after = started + [read_obs(), read_obs()]
        hit = find_prior_attempt(after, "shell.run", sh("ipconfig /all"), TIER)
        check(f"{kind}: identical retry after two other steps is STILL blocked (it may already have run)", hit is not None and hit[0] == 0)
        check(f"{kind}: ...re-spelled (case/space) it is the same call", find_prior_attempt(after, "shell.run", sh("  IPCONFIG   /all "), TIER) is not None)
        check(f"{kind}: a different command is allowed", find_prior_attempt(after, "shell.run", sh("hostname"), TIER) is None)
        check(f"{kind}: the same command in the other shell is allowed", find_prior_attempt(after, "shell.run", sh("ipconfig /all", "cmd"), TIER) is None)
        check(f"{kind}: the same command in another working directory is allowed",
              find_prior_attempt(after, "shell.run", sh("ipconfig /all", cwd=other_dir), TIER) is None)
        located = [shell_obs("ipconfig /all", kind, cwd=other_dir), read_obs()]
        check(f"{kind}: ...and it is blocked again in the directory where it was left unknown",
              find_prior_attempt(located, "shell.run", sh("ipconfig /all", cwd=other_dir), TIER) is not None)
        check(f"{kind}: ...while the default directory is a different call", find_prior_attempt(located, "shell.run", sh("ipconfig /all"), TIER) is None)

    scenario("A[ownership]: only a shell.run that ended unknown holds a repeat")
    stray = Observation(PlanStep("apps.open", {"name": "x"}), False, "x", {"status": "unknown_outcome"})
    check("another tool carrying the same status is not held", not _outcome_unknown(stray))
    check("an ok result is never an unknown outcome", not _outcome_unknown(Observation(PlanStep("shell.run", sh("x")), True, "x", {"status": "unknown_outcome"})))
    check("no data -> not an unknown outcome", not _outcome_unknown(Observation(PlanStep("shell.run", sh("x")), False, "x", None)))

    scenario("A[controls]: everything else keeps its rule (immediate blocked; after a step: not_started held, the rest allowed)")
    for kind in OTHERS:
        obs = [shell_obs("ipconfig /all", kind)]
        check(f"{kind}: not an unknown outcome", not _outcome_unknown(obs[0]))
        check(f"{kind}: immediate identical retry is blocked", find_prior_attempt(obs, "shell.run", sh("ipconfig /all"), TIER) is not None)
        after = find_prior_attempt(obs + [read_obs()], "shell.run", sh("ipconfig /all"), TIER)
        held = kind == "not_started"
        check(f"{kind}: identical retry after another step is {'STILL blocked (Phase 92)' if held else 'allowed (unchanged)'}", (after is not None) == held)

    scenario("A[mixed]: only the matching call is affected")
    mix = [shell_obs("ipconfig /all", "unknown_timeout"), shell_obs("hostname", "ordinary"), read_obs()]
    check("the unknown-outcome command stays blocked behind an ordinary failed one", find_prior_attempt(mix, "shell.run", sh("ipconfig /all"), TIER) is not None)
    check("the ordinary failed command behind other steps stays retryable", find_prior_attempt(mix, "shell.run", sh("hostname"), TIER) is None)


# =============================================================================================
# B -- the real shell.run
# =============================================================================================


def section_b() -> None:
    scenario("B: the real shell.run reports an unknown outcome in the shape the guard keys on (run_owned replaced; nothing is spawned)")
    original = shellproc.run_owned
    tmp = str(Path(tempfile.gettempdir()))
    try:
        for cause in (shellproc.CANCELLED, shellproc.TIMEOUT):
            shellproc.run_owned = lambda argv, **kw: shellproc.Outcome(shellproc.UNKNOWN, cause=cause, detail="tree_not_confirmed", returncode=1, stdout="partial")
            r = shell.run("echo hi", shell="cmd", cwd=tmp)
            check(f"{cause}: ok False with status unknown_outcome", r.ok is False and r.data.get("status") == "unknown_outcome", str(r.data))
            check(f"{cause}: the speech says it could not be confirmed ended", "couldn't confirm" in r.speech, r.speech)
            ob = Observation(PlanStep("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": tmp}), r.ok, r.speech, r.data)
            check(f"{cause}: the orchestrator's predicate recognises the real result", _outcome_unknown(ob) and not _never_started(ob))
        for state, detail in ((shellproc.TIMEOUT, ""), (shellproc.CANCELLED, ""), (shellproc.NOT_STARTED, "no_job"), (shellproc.COMPLETED, "")):
            shellproc.run_owned = lambda argv, **kw: shellproc.Outcome(state, returncode=1, detail=detail)
            r = shell.run("echo hi", shell="cmd", cwd=tmp)
            ob = Observation(PlanStep("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": tmp}), r.ok, r.speech, r.data)
            check(f"{state}: not an unknown outcome", not _outcome_unknown(ob), str(r.data))
    finally:
        shellproc.run_owned = original


# =============================================================================================
# C -- the real run_goal
# =============================================================================================


async def section_c() -> None:
    for kind in UNKNOWN_KINDS:
        scenario(f"C[{kind}]: run_goal -- the identical retry never runs, a different command does")
        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("ipconfig")), done("x")])
        check(f"{kind}: the command was attempted ONCE (the identical retry after a read did not run)", len(runs()) == 1, str(runs()))
        b = blocked(res)
        check(f"{kind}: the retry became a blocked observation, not an executed step", len(b) == 1 and b[0].data.get("status") == "already_tried")
        w = b[0].speech if b else ""
        check(f"{kind}: the block says the outcome is UNKNOWN and the command may already have run", "UNKNOWN" in w and "may already have run" in w, w)
        check(f"{kind}: ...it does not call the previous attempt FAILED or ok", "FAILED" not in w and "(ok)" not in w, w)
        check(f"{kind}: ...it quotes the earlier, unchanged wording", "couldn't confirm" in w, w)
        check(f"{kind}: ...and points at a safe way forward (read-only check / different command / done)", "read-only" in w and "different" in w and "declare done" in w, w)
        check(f"{kind}: ...the data does not claim the earlier attempt failed", b and b[0].data.get("previous_ok") is None and b[0].data.get("previous_outcome") == "unknown", str(b[0].data if b else ""))
        shown = LAST["scripted"].prompt_of(len(LAST["scripted"].requests) - 1)   # what the planner saw right after the block
        check(f"{kind}: the planner is TOLD it is unknown and may already have run (history line, not only the block text)",
              "UNKNOWN" in shown and "may already have run" in shown and "read-only tool" in shown, shown[-600:])
        check(f"{kind}: ...and is not given the generic 'nothing has changed' ALREADY_TRIED line for it", "nothing has changed" not in shown, shown[-600:])
        sys_prompt = LAST["scripted"].system_of(len(LAST["scripted"].requests) - 1)
        check(f"{kind}: the next-turn hint says the outcome is unknown, not 'you already have its result'",
              "UNKNOWN" in sys_prompt and "may already have run" in sys_prompt and "already have its result" not in sys_prompt, sys_prompt[-500:])
        first = res.observations[0]
        check(f"{kind}: the original observation is unchanged (still not ok, still unknown_outcome, speech intact)",
              first.ok is False and first.data.get("status") == "unknown_outcome" and "couldn't confirm" in first.speech and first.error in (None, ""))

        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("hostname")), done("x")])
        check(f"{kind}: a genuinely different command still runs", [a["command"] for a in runs()] == ["ipconfig", "hostname"] and not blocked(res), str(runs()))
        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("ipconfig", "cmd")), done("x")])
        check(f"{kind}: the other shell still runs", len(runs()) == 2 and not blocked(res), str(runs()))

        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("shell.run", sh("ipconfig")), call("shell.run", sh("ipconfig")), done("x")])
        check(f"{kind}: the immediate-retry protection is unchanged (one run, then the repeat streak stops the run)", len(runs()) == 1 and res.stopped == "repeated_action", str(res.stopped))

    scenario("C[twice]: the user's own count does not make an unknown-outcome command run again")
    reset("unknown_stopped")
    res = await drive("Update ipconfig twice", [call("shell.run", sh("ipconfig")), call("shell.run", sh("ipconfig")), done("x")])
    check("'twice' with a first attempt of unknown outcome: not re-run", len(runs()) == 1, str(runs()))
    check("...the retry was blocked with the unknown-outcome wording", len(blocked(res)) == 1 and "UNKNOWN" in blocked(res)[0].speech)


# =============================================================================================
# D -- controls through run_goal
# =============================================================================================


async def section_d() -> None:
    for kind in ("ordinary", "timeout", "cancelled"):
        scenario(f"D[{kind}]: run_goal -- retry behaviour is exactly what it was")
        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("ipconfig")), done("x")])
        check(f"{kind}: retry after a read still runs (2 attempts, nothing blocked)", len(runs()) == 2 and not blocked(res), str(runs()))
        reset(kind)
        res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("shell.run", sh("ipconfig")), done("x")])
        b = blocked(res)
        check(f"{kind}: an immediate identical retry is still blocked, with the old FAILED wording", len(runs()) == 1 and len(b) == 1 and "FAILED" in b[0].speech and "UNKNOWN" not in b[0].speech, b[0].speech if b else "")
        reset(kind)
        res = await drive("Update ipconfig twice", [call("shell.run", sh("ipconfig")), call("shell.run", sh("ipconfig")), done("x")])
        check(f"{kind}: 'twice' still lets the second attempt run", len(runs()) == 2 and not blocked(res), str(runs()))

    scenario("D[not_started]: Phase 92 protection is intact, with its own wording")
    reset("not_started")
    res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("ipconfig")), done("x")])
    b = blocked(res)
    check("not_started: identical retry after a read does not run", len(runs()) == 1 and len(b) == 1)
    check("not_started: ...still reported as FAILED (it never ran), not as unknown", b and "FAILED" in b[0].speech and "UNKNOWN" not in b[0].speech and b[0].data.get("previous_ok") is False, b[0].speech if b else "")

    scenario("D[ok]: a successful run is not repeated after an intervening step... it is a real second press (unchanged)")
    reset("ok")
    res = await drive("Update big.txt", [call("shell.run", sh("ipconfig")), call("test.un_other"), call("shell.run", sh("ipconfig")), done("x")])
    check("ok: run, read, run -> 2 runs, nothing blocked", len(runs()) == 2 and not blocked(res))


async def main() -> int:
    t0 = time.perf_counter()
    _register_fixtures()
    saved = H.lock_down_real_tools(REGISTRY)
    try:
        with store.use_temp_db():
            section_a()
            section_b()
            await section_c()
            await section_d()
    finally:
        CFG.permissions.overrides = saved
    return H.finish("UNKNOWN-OUTCOME RETRY SCORECARD", time.perf_counter() - t0, min_assertions=85, min_scenarios=14)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
