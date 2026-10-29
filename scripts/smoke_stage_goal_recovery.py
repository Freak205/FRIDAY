"""Phase 84 -- stage-level recovery and the goal verdict make the same recovery decision for the same execution history.

Found by reading Phase 69's limitation against Phase 67: a state change whose read-back CONTRADICTED the tool (the tool said yes, the
state said no) followed by an equivalent retry that was only completed / partly read back / never read back was `recovered` in the step
report, so `steps.stage_standing` called the stage RESOLVED (and the orchestrator credited it SUCCEEDED) -- while `verify.summarize`, which
only accepts a later step that was itself read back and VERIFIED, still reported the failed read-back and the run's verdict `failed`.

Now `steps._records` recovers a contradicted read-back only the way `summarize` does (`verify._redone` by a later VERIFIED step).
Everything else is untouched: a plain tool failure (no read-back) keeps the Phase 54 rule, unknown / waiting steps, unrelated steps.

Pure functions on hand-built observations; nothing runs, no LLM, no data/.
"""

from __future__ import annotations

import sys
import time

import iso_env
import phase21_common as H
from phase21_common import check, scenario

from friday import steps, verify  # noqa: E402
from friday.intelligence.goals import Subgoal, SubgoalStatus  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402

STAGE = Subgoal(id="s0", description="write the file", status=SubgoalStatus.ACTIVE)
W = "files.write"


def vobs(tool: str, args: dict, status: str | None, *, stage: Subgoal = STAGE) -> Observation:
    """A step under STAGE. status: a verification status, or None for a step with no read-back (a plain completion)."""
    o = Observation(PlanStep(tool, dict(args), subgoal=stage.description, subgoal_id=stage.id), status != "failed", "", {},
                    error="postcondition_failed" if status == "failed" else "")
    if status is not None:
        o.verification = verify.Verification(verify.VerifyStatus(status), [], reason=f"{status} reason")
    return o


def plain_fail(tool: str, args: dict) -> Observation:
    return Observation(PlanStep(tool, dict(args), subgoal=STAGE.description, subgoal_id=STAGE.id), False, "boom", {}, error="RuntimeError")


def view(obs: list[Observation]) -> tuple[str, str, list[bool]]:
    """(stage standing, goal verdict, per-step recovered flags) of one history."""
    return (steps.stage_standing(obs, [STAGE])[0], verify.summarize(obs).status, [r.recovered for r in steps._records(obs)[0]])


def agree(label: str, obs: list[Observation], stage: str, goal: str, recovered: list[bool]) -> None:
    got = view(obs)
    check(f"{label}: stage={stage}, goal={goal}, recovered={recovered}", got == (stage, goal, recovered), str(got))
    # the same decision at both levels: the stage is resolved exactly when the goal verdict holds no failed read-back
    check(f"{label}: stage and goal agree", (got[0] == steps.RESOLVED) == (got[1] != "failed"), str(got))


def main() -> int:
    t0 = time.time()
    with iso_env.isolated_runtime():
        bad = vobs(W, {"path": "a.txt", "content": "1"}, "failed")

        scenario("A  a verified equivalent retry recovers the failed read-back at both levels")
        agree("same tool, other content", [bad, vobs(W, {"path": "a.txt", "content": "2"}, "verified")],
              steps.RESOLVED, "verified", [True, False])
        agree("identical call, spelling aside", [bad, vobs(W, {"path": "A.TXT ", "content": "1"}, "verified")],
              steps.RESOLVED, "verified", [True, False])
        agree("another tool of the same kind (modify) on the same path", [bad, vobs("files.mkdir", {"path": "a.txt"}, "verified")],
              steps.RESOLVED, "verified", [True, False])

        scenario("B  an equivalent retry that was not read back as verified does NOT recover it (the reproduced mismatch)")
        agree("retry partially read back", [bad, vobs(W, {"path": "a.txt", "content": "2"}, "partial")],
              steps.UNRESOLVED, "failed", [False, False])
        agree("retry never read back (unverified)", [bad, vobs(W, {"path": "a.txt", "content": "2"}, "unverified")],
              steps.UNRESOLVED, "failed", [False, False])
        agree("retry completed, nothing to read back", [bad, vobs(W, {"path": "a.txt", "content": "2"}, None)],
              steps.UNRESOLVED, "failed", [False, False])
        agree("retry contradicted again", [bad, vobs(W, {"path": "a.txt", "content": "2"}, "failed")],
              steps.UNRESOLVED, "failed", [False, False])
        agree("another tool of the same kind, only partly read back",
              [bad, vobs("files.mkdir", {"path": "a.txt"}, "partial")], steps.UNRESOLVED, "failed", [False, False])

        scenario("C  an unrelated successful step recovers nothing")
        agree("verified write to another file", [bad, vobs(W, {"path": "b.txt", "content": "1"}, "verified")],
              steps.UNRESOLVED, "failed", [False, False])
        agree("verified step of another kind on the same file", [bad, vobs("files.delete", {"path": "a.txt"}, "verified")],
              steps.UNRESOLVED, "failed", [False, False])
        agree("a verified redo BEFORE the failure", [vobs(W, {"path": "a.txt", "content": "0"}, "verified"), bad],
              steps.UNRESOLVED, "failed", [False, False])

        scenario("D  each contradicted read-back needs its own verified redo")
        bad_b = vobs(W, {"path": "b.txt", "content": "1"}, "failed")
        agree("two failures, one verified redo", [bad, bad_b, vobs(W, {"path": "a.txt", "content": "2"}, "verified")],
              steps.UNRESOLVED, "failed", [True, False, False])
        agree("two failures, two verified redos",
              [bad, bad_b, vobs(W, {"path": "a.txt", "content": "2"}, "verified"), vobs(W, {"path": "b.txt", "content": "2"}, "verified")],
              steps.RESOLVED, "verified", [True, True, False, False])
        agree("verified redo, then a partial retry of the same file (the earlier verified one stands)",
              [bad, vobs(W, {"path": "a.txt", "content": "2"}, "partial"), vobs(W, {"path": "a.txt", "content": "3"}, "verified")],
              steps.RESOLVED, "partial", [True, False, False])

        scenario("E  unchanged: ordinary verified steps, plain tool failures, unknown outcomes")
        agree("a lone verified step", [vobs(W, {"path": "a.txt", "content": "1"}, "verified")], steps.RESOLVED, "verified", [False])
        agree("a lone contradicted read-back", [bad], steps.UNRESOLVED, "failed", [False])
        # a tool that failed on its own has no read-back: the Phase 54 rule (any later equivalent success) still applies, and the goal
        # verdict has nothing to say about it
        pf = plain_fail(W, {"path": "a.txt", "content": "1"})
        got = view([pf, vobs(W, {"path": "a.txt", "content": "2"}, None)])
        check("plain failure + equivalent completed retry: still recovered (Phase 54), stage resolved",
              got[0] == steps.RESOLVED and got[2] == [True, False], str(got))
        got = view([pf, vobs(W, {"path": "b.txt", "content": "2"}, None)])
        check("plain failure + retry on another file: not recovered", got[0] == steps.UNRESOLVED and got[2] == [False, False], str(got))
        got = view([pf])
        check("plain failure alone: unresolved", got[0] == steps.UNRESOLVED and got[2] == [False], str(got))
        unk = Observation(PlanStep(W, {"path": "a.txt"}, subgoal=STAGE.description, subgoal_id=STAGE.id), False, "", {}, error="interrupted")
        check("an unknown outcome keeps the stage unresolved", steps.stage_standing([unk, vobs(W, {"path": "a.txt", "content": "2"}, "verified")], [STAGE]) == [steps.UNRESOLVED])

        scenario("F  the report and the stored flag follow the same decision, and a resolved stage is not a completed goal")
        obs = [bad, vobs(W, {"path": "a.txt", "content": "2"}, "partial")]
        r = steps.build_report(obs, stopped="completed", subgoals=[STAGE])
        check("report keeps the failed read-back as failed, not recovered", [(s.state.value, s.recovered) for s in r.steps] == [("failed", False), ("completed", False)], str(r.steps))
        check("so the outcome is not 'complete'", r.outcome in ("partial", "failed") and "Failed" in r.tail(), r.outcome)
        obs = [bad, vobs(W, {"path": "a.txt", "content": "2"}, "verified")]
        r = steps.build_report(obs, stopped="completed", subgoals=[STAGE])
        check("a verified redo: failure recovered, outcome complete (unverified: the recovered step is not itself verified)",
              [s.recovered for s in r.steps] == [True, False] and r.outcome == "complete_unverified", str(r.outcome))
        check("the recovery flag is what is stored", [s.get("recovered", False) for s in steps.progress_of(r, "completed")["steps"]] == [True, False])
        # a stage resolved on its own steps is not the goal's completion: the run stopping for another reason is still partial
        r = steps.build_report(obs, stopped="step_limit", subgoals=[STAGE])
        check("a resolved stage does not complete a run that stopped early", r.outcome == "partial", r.outcome)

    return H.finish("Phase 84 stage/goal recovery consistency", time.time() - t0, min_assertions=40, min_scenarios=6)


if __name__ == "__main__":
    sys.exit(main())
