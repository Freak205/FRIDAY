"""Phase 95 -- resuming a goal does not forget a shell command that never started or whose outcome is unknown.

A goal is resumed (a clarification answer, "yes, fix it") by calling `plan.run` again with the SAME goal id. `run_goal` then starts with
a fresh observation list, so Phase 92/93's repeat guard (which reads that list) forgot that the identical command had not started, or
had ended with an unknown outcome, and let it run again. Now the blocking outcomes of a goal's run (compact copies, with the exact
arguments) are carried to its resumed run and consulted by the same guard, under the same identity rules.

What is NOT done, and pinned below: nothing is rebuilt from the audit log. Its shell arguments are stored redacted (first token and
counts), which cannot tell `git add a.txt` from `git add b.txt`; blocking on that would block different commands. So the protection
is for the running process; after a restart the audit still says exactly what happened (outcome per row, UNKNOWN in the rebuilt
ledger) but cannot establish "the identical command", and the guard does not guess.

  A  unknown_outcome -> resumed goal -> identical command: not run (no second audit row, no second process), UNKNOWN wording
  B  not_started -> resumed goal -> identical command: not run, wording unchanged from Phase 92 (it never ran)
  C  a different command / shell / working directory still runs in the resumed goal; a respelled identical one does not
  D  a genuinely new goal (same text, no resume; an unknown goal id) is unaffected
  E  ordinary failure, timeout, cancelled, success are not held (resumed identical command runs: Phase 92/93 scope unchanged)
  F  legacy rows: an old audit row (NULL outcome, ok=0) is not treated as unknown
  G  a simulated restart: audit history is reconstructed (outcome words, UNKNOWN step); the guard does not guess from it
  H  bounds and privacy of what is carried; same-run protection intact; held calls never appear as steps

Deterministic: scripted planner; the real plan.run, Executor, shell.run and audit; `shellproc.run_owned` replaced (nothing is spawned);
throwaway database (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import iso_env
from phase21_common import ScriptedPlanner, call, check, done, finish, scenario, scripted_provider

from friday import audit, shellproc, steps, store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Observation, PlanStep, calls_to_hold  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402

REGISTRY.discover()
from friday.skills import plan as plan_skill  # noqa: E402

GOAL = "Update big.txt"
WORKDIR = str(Path(__file__).resolve().parent.parent)
OUTCOMES = {
    "unknown": shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed"),
    "not_started": shellproc.Outcome(shellproc.NOT_STARTED, detail="no_job"),
    "ordinary": shellproc.Outcome(shellproc.COMPLETED, returncode=3, stderr="boom"),
    "timeout": shellproc.Outcome(shellproc.TIMEOUT, how="forced"),
    "cancelled": shellproc.Outcome(shellproc.CANCELLED, how="graceful"),
    "success": shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout="hi"),
}
NEXT = ["success"]   # what the next process start reports
STARTED: list[list[str]] = []   # every command that really reached "the process layer"


def fake_run_owned(argv, **kw):
    STARTED.append(list(argv))
    return OUTCOMES[NEXT[0]]


async def approve(skill_, args, preview: str) -> bool:
    return True


def reset() -> None:
    INTEL.reset()
    plan_skill._HELD.clear()
    STARTED.clear()
    EXECUTOR.set_confirm_handler(approve)
    CFG.planner.structured_output = False
    CFG.planner.intent_guard = True
    CFG.planner.goal_coverage = True
    CFG.planner.semantic_repeat_guard = True
    CFG.planner.counted_repeats = True
    CFG.planner.scope_expansion = True
    CFG.desktop_observer.enabled = False


def sh(command: str, shell_: str = "cmd", cwd: str = WORKDIR) -> dict:
    return {"command": command, "shell": shell_, "cwd": cwd}


async def go(replies: list[str], *, goal: str = GOAL, **extra):
    planner = ScriptedPlanner(replies)
    with scripted_provider(planner):
        r = await EXECUTOR.run("plan.run", {"goal": goal, **extra}, actor="text")
    return r, planner


async def first(kind: str, command: str = "ipconfig /all", goal: str = GOAL):
    NEXT[0] = kind
    r, _ = await go([call("shell.run", sh(command)), done("x")], goal=goal)
    return r, r.data["goal_id"]


async def resume(replies: list[str], gid: str, kind: str = "success", goal: str = GOAL):
    NEXT[0] = kind
    return await go(replies, goal=goal, resume_goal_id=gid, clarification_answer="go ahead")


def blocked(r) -> list[dict]:
    """The steps of a run that the REPEAT GUARD refused (not run for any other reason)."""
    return [s for s in r.data.get("steps", []) if s["tool"] == "shell.run" and s["error"] == "repeated_call"]


def shell_rows(gid: str) -> list[dict]:
    return [r for r in reversed(audit.recent(500)) if r["skill"] == "shell.run" and r["goal_id"] == gid]


def reopen() -> None:
    store.conn().close()
    store._local.conns.clear()


async def main() -> int:
    t0 = time.perf_counter()
    shellproc.run_owned = fake_run_owned
    with iso_env.isolated_runtime():
        # ------------------------------------------------------------------------------------------------ A
        scenario("A. unknown_outcome -> the goal is resumed -> the identical command")
        reset()
        r1, gid = await first("unknown")
        check("A1 the first run started the command once and it ended with an unknown outcome",
              len(STARTED) == 1 and shell_rows(gid)[0]["outcome"] == "unknown_outcome" and shell_rows(gid)[0]["status"] == "outcome_unknown")
        check("A2 the run kept it: one held call, status unknown_outcome, exact arguments, no output", len(plan_skill._HELD.get(gid, [])) == 1
              and plan_skill._HELD[gid][0].data == {"status": "unknown_outcome"} and plan_skill._HELD[gid][0].step.args["command"] == "ipconfig /all")
        r2, p2 = await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid)
        check("A3 resumed with the SAME goal id", r2.data["goal_id"] == gid)
        check("A4 the identical command was NOT started again (no second process, no second audit row), and it was the repeat guard that refused it",
              len(STARTED) == 1 and len(shell_rows(gid)) == 1 and len(blocked(r2)) == 1, str(STARTED))
        seen = p2.prompt_of(len(p2.requests) - 1)
        check("A5 the planner is told: outcome UNKNOWN, may already have run, an earlier run of this goal",
              "UNKNOWN" in seen and "may already have run" in seen and "earlier run of this goal" in seen, seen[-500:])
        check("A6 ...and not that it failed or that nothing changed", "FAILED" not in seen.split("NOT RUN AGAIN")[-1][:400] and "nothing has changed" not in seen, seen[-500:])
        listed = [s for s in r2.data.get("steps", []) if s["tool"] == "shell.run"]
        check("A7 the held call is not a step of the resumed run: the only shell.run entry is the blocked repeat, and the report has no shell step",
              len(listed) == 1 and listed[0]["error"] == "repeated_call" and not [s for s in r2.data["report"]["steps"] if s["tool"] == "shell.run"],
              str(listed) + str(r2.data["report"]))
        check("A8 the unknown outcome is still held for a further resume", len(plan_skill._HELD.get(gid, [])) == 1)
        r3, p3 = await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid)
        check("A9 a second resume is held as well", len(STARTED) == 1)

        # ------------------------------------------------------------------------------------------------ B
        scenario("B. not_started -> the goal is resumed -> the identical command")
        reset()
        r1, gid = await first("not_started")
        check("B1 the first run did not start it (outcome not_started, held, distinct from unknown)", shell_rows(gid)[0]["outcome"] == "not_started"
              and plan_skill._HELD[gid][0].data == {"status": "not_started"})
        r2, p2 = await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid)
        seen = p2.prompt_of(len(p2.requests) - 1)
        check("B2 the identical command is not started on resume: refused by the repeat guard", len(STARTED) == 1 and len(shell_rows(gid)) == 1 and len(blocked(r2)) == 1)
        check("B3 the wording is the 'never ran' one (FAILED, as in Phase 92) -- NOT the unknown-outcome wording",
              "ALREADY_TRIED" in seen and "UNKNOWN" not in seen and "earlier run of this goal" in seen, seen[-500:])

        # ------------------------------------------------------------------------------------------------ C
        scenario("C. genuinely different recovery approaches still run on resume; the existing identity rules apply")
        for kind in ("unknown", "not_started"):
            reset()
            r1, gid = await first(kind)
            base = len(STARTED)
            await resume([call("shell.run", sh("hostname")), done("x")], gid)
            check(f"C1 [{kind}] a different command runs", len(STARTED) == base + 1 and STARTED[-1][-1] == "hostname", str(STARTED))
            reset()
            r1, gid = await first(kind)
            base = len(STARTED)
            await resume([call("shell.run", sh("ipconfig /all", "powershell")), done("x")], gid)
            check(f"C2 [{kind}] the same command in the other shell runs", len(STARTED) == base + 1)
            reset()
            r1, gid = await first(kind)
            base = len(STARTED)
            await resume([call("shell.run", sh("ipconfig /all", cwd=str(Path.home()))), done("x")], gid)
            check(f"C3 [{kind}] the same command in another working directory runs", len(STARTED) == base + 1)
            reset()
            r1, gid = await first(kind)
            base = len(STARTED)
            rc4, _ = await resume([call("shell.run", sh("  IPCONFIG   /all ")), done("x")], gid)
            check(f"C4 [{kind}] a re-spelled identical command (case/space) is the same call: refused by the repeat guard", len(STARTED) == base and len(blocked(rc4)) == 1)

        # ------------------------------------------------------------------------------------------------ D
        scenario("D. a genuinely new goal keeps its behaviour")
        reset()
        r1, gid = await first("unknown")
        NEXT[0] = "success"
        r_new, _ = await go([call("shell.run", sh("ipconfig /all")), done("x")])
        check("D1 the same goal text WITHOUT resuming is a new goal: the command runs", r_new.data["goal_id"] != gid and len(STARTED) == 2)
        r_new2, _ = await go([call("shell.run", sh("ipconfig /all")), done("x")], resume_goal_id="no-such-goal", clarification_answer="x")
        check("D2 an unknown goal id starts a new goal: the command runs", r_new2.data["goal_id"] not in (gid, "no-such-goal") and len(STARTED) == 3)
        check("D3 the first goal's held call is still held (the new goals did not consume it)", len(plan_skill._HELD.get(gid, [])) == 1)

        # ------------------------------------------------------------------------------------------------ E
        scenario("E. other outcomes are not held: Phase 92/93 scope is unchanged")
        for kind in ("ordinary", "timeout", "cancelled", "success"):
            reset()
            r1, gid = await first(kind)
            check(f"E1 [{kind}] nothing is held", gid not in plan_skill._HELD)
            await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid, "success")
            check(f"E2 [{kind}] the identical command runs again on resume (as before Phase 95)", len(STARTED) == 2)

        # ------------------------------------------------------------------------------------------------ F
        scenario("F. legacy rows: a NULL-outcome row is never read as unknown")
        reset()
        r1, gid = await first("ordinary")
        store.conn().execute("UPDATE audit SET outcome=NULL WHERE goal_id=? AND skill='shell.run'", (gid,))   # as an older database has it
        store.conn().commit()
        row = shell_rows(gid)[0]
        check("F1 the legacy-shaped row is ok=0, outcome NULL, status failed", row["ok"] == 0 and row["outcome"] is None and row["status"] == "failed")
        await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid, "success")
        check("F2 resuming that goal: the identical command runs (nothing was inferred from the old row)", len(STARTED) == 2)
        check("F3 calls_to_hold ignores an observation whose data carries no unknown/not_started status",
              calls_to_hold([Observation(PlanStep("shell.run", sh("x")), False, "Exit code 1.", {"returncode": 1}),
                             Observation(PlanStep("shell.run", sh("y")), False, "x", None)]) == [])

        # ------------------------------------------------------------------------------------------------ G
        scenario("G. a simulated restart: the audit history is reconstructed; the guard does not guess from it")
        reset()
        r1, gid_u = await first("unknown")
        r1, gid_n = await first("not_started", "hostname")
        plan_skill._HELD.clear()      # the process is gone
        reopen()                      # ...and the database is opened again
        ledger_u = [s.state for s in steps.ledger_for_goal(gid_u, probe=lambda *a: "unknown")]
        ledger_n = [s.state for s in steps.ledger_for_goal(gid_n, probe=lambda *a: "unknown")]
        check("G1 the unknown_outcome step is reconstructed as UNKNOWN", ledger_u == [steps.StepState.UNKNOWN], str(ledger_u))
        check("G2 the not_started step is reconstructed as FAILED (a definite end), distinct from UNKNOWN", ledger_n == [steps.StepState.FAILED], str(ledger_n))
        check("G3 the audit rows still carry the two outcome words", shell_rows(gid_u)[0]["outcome"] == "unknown_outcome" and shell_rows(gid_n)[0]["outcome"] == "not_started")
        base = len(STARTED)
        await resume([call("shell.run", sh("ipconfig /all")), done("x")], gid_u, "success")
        check("G4 KNOWN LIMITATION, pinned: after a restart the redacted audit cannot establish 'the identical command', so nothing is blocked "
              "from it (the guard does not guess)", len(STARTED) == base + 1)

        # ------------------------------------------------------------------------------------------------ H
        scenario("H. bounds, privacy, and the same-run guard")
        plan_skill._HELD.clear()
        held_obs = [Observation(PlanStep("shell.run", sh(f"cmd{i}")), False, "x", {"status": "unknown_outcome", "stdout": "SECRET-OUTPUT", "returncode": 1}) for i in range(30)]
        kept = calls_to_hold(held_obs)
        check("H1 at most 20 calls are carried, the most recent ones", len(kept) == 20 and kept[-1].step.args["command"] == "cmd29" and kept[0].step.args["command"] == "cmd10")
        check("H2 the copies hold the status word only: no output, no return code", all(o.data == {"status": "unknown_outcome"} for o in kept))
        for i in range(70):
            plan_skill._remember_held(f"g{i}", [], held_obs[:1])
        check("H3 at most 64 goals are remembered, the oldest forgotten", len(plan_skill._HELD) == 64 and "g0" not in plan_skill._HELD and "g69" in plan_skill._HELD)
        plan_skill._remember_held("", [], held_obs[:1])
        plan_skill._remember_held(None, [], held_obs[:1])
        check("H4 no goal id: nothing is remembered", len(plan_skill._HELD) == 64)
        reset()
        NEXT[0] = "unknown"
        rr, _ = await go([call("shell.run", sh("ipconfig /all")), call("shell.run", sh("ipconfig /all")), done("x")])
        check("H5 same-run protection intact: an immediate identical repeat after unknown is blocked in the run itself", len(STARTED) == 1)
        reset()
        NEXT[0] = "success"
        rr, _ = await go([call("shell.run", sh("ipconfig /all")), done("x")])
        check("H6 a goal that only ever succeeded holds nothing and carries nothing", rr.data["goal_id"] not in plan_skill._HELD)

        # ------------------------------------------------------------------------------------------------ I
        scenario("I. the other resume path, the user's own count, and the carried bound")
        reset()
        NEXT[0] = "success"
        rq, _ = await go([call("shell.run", sh("hostname")), done("x")], goal="Inspect my project.")
        gid = rq.data["goal_id"]
        held_obs = Observation(PlanStep("shell.run", sh("ipconfig /all")), False, "That command was stopped, but I couldn't confirm that it ended.", {"status": "unknown_outcome"})
        plan_skill._remember_held(gid, [], [held_obs])
        base = len(STARTED)
        NEXT[0] = "success"
        rx, px = await go([call("shell.run", sh("ipconfig /all")), done("x")], goal="Inspect my project.", resume_goal_id=gid, scope_expansion="yes, fix it")
        check("I1 scope expansion ('yes, fix it') of the same goal id also carries the held call: the identical command is refused", rx.data["goal_id"] == gid
              and len(STARTED) == base and len(blocked(rx)) == 1, str(STARTED))
        reset()
        NEXT[0] = "success"
        rq, _ = await go([call("shell.run", sh("hostname")), done("x")], goal="Inspect my project.")
        gid = rq.data["goal_id"]
        base = len(STARTED)
        rx, _ = await go([call("shell.run", sh("ipconfig /all")), done("x")], goal="Inspect my project.", resume_goal_id=gid, scope_expansion="yes, fix it")
        check("I2 control: with nothing held the same expanded goal runs that command (so I1 was the held call, not the scope)", rx.data["goal_id"] == gid and len(STARTED) == base + 1)

        for kind in ("unknown", "not_started"):
            reset()
            r1, gid = await first(kind, "ipconfig", goal="Update ipconfig twice")
            rt, _ = await resume([call("shell.run", sh("ipconfig")), done("x")], gid, goal="Update ipconfig twice")
            check(f"I3 [{kind}] 'twice' in the goal does not make the held command run again on resume", len(STARTED) == 1 and len(blocked(rt)) == 1)
        reset()
        r1, gid = await first("ordinary", "ipconfig", goal="Update ipconfig twice")
        await resume([call("shell.run", sh("ipconfig")), done("x")], gid, "success", goal="Update ipconfig twice")
        check("I4 control: 'twice' after an ordinary failure still runs it again on resume", len(STARTED) == 2)

        mk = lambda i: Observation(PlanStep("shell.run", sh(f"c{i}")), False, "x", {"status": "unknown_outcome"})
        plan_skill._HELD.clear()
        plan_skill._remember_held("gcap", calls_to_hold([mk(i) for i in range(15)]), [mk(i) for i in range(15, 25)])
        names = [o.step.args["command"] for o in plan_skill._HELD["gcap"]]
        check("I5 carried + new calls stay within the bound, keeping the most recent", len(names) == 20 and names[-1] == "c24" and names[0] == "c5", str(names))

    return finish("Phase 95 -- execution safety across goal resumption", time.perf_counter() - t0, min_assertions=50, min_scenarios=9)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
