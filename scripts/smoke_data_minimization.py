"""Phase 66 -- store less of what the user said, read, or was told; keep what a feature needs.

Credential redaction (Phases 56-64) cannot recognise an arbitrary secret in ordinary words. This phase is about the other lever: not keeping text
nothing reads. Traced field by field (writers, readers, tests, the export script):

  REQUIRED, kept raw, documented: `goals.objective` and `original_request` -- resuming a goal after a clarification, widening it ("yes, fix it") and
      correcting it re-read the user's own words; `user_correction` (bounded, credential-checked) -- `experience.py` surfaces it; the authorising follow-up
      and clarification answer in `contract.known_constraints` (credential-checked) -- Phase 21 records authority "in the user's own words".
  UNNECESSARY, removed or reduced:
      goals.success_criteria      a third copy of the goal inside boilerplate -- nothing reads it             -> fixed text
      contract.intent / desired_outcome   two more verbatim copies -- nothing reads them                     -> kind of outcome only
      episodes.context            the ambient desktop snapshot (window titles, files, contacts); `experience.py` says it is never surfaced -> not stored
      corrections.original_interpretation / context   copies of the goal (already linked by goal_id) and of the previous utterance -> no longer passed
      job_runs.detail             the raw result of every scheduled action, returned by `schedule.history`, bypassing the audit log's read-back
                                  reduction                                                                   -> minimized like an audit row
      audit result of plan.run / apps.list   the planner's summary of what its steps READ (re-stating a file's content that the step's own row had reduced
                                  to a length) / every open window title                                       -> read-back: length only above 80 chars
  CHECKED on the way to disk (fail-closed): every free-text field of the goal contract, the subgoals, the failure reason, corrections, job details.
  READERS: the goal-status answer scrubs the remaining-work text it reads (rows written before this phase were stored unchecked).
  LOGS: lines that only needed to say "a goal was created / cancelled, a phrasing taught, a notification sent" no longer print the text.

Synthetic data; throwaway databases (asserted); no real database or data/ folder; recognisable credential patterns only -- an unmarked secret in
the objective is still stored, because resuming needs it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import iso_env  # noqa: E402
import phase21_common as H  # noqa: E402
import smoke_goal_steps as G  # noqa: E402  (fixtures, planner, long-lived loop)
from phase21_common import call, check, done, finish, scenario  # noqa: E402

from friday import audit, auditview, dbplace, jobs, paths, steps, store, toolview  # noqa: E402
from friday.bus import BUS  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import corrections, episodes, experience  # noqa: E402
from friday.intelligence import goals as goals_mod  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402
from friday.skills import plan as plan_mod  # noqa: E402

S1 = "zq7-SYNTH-secret-8841"     # a recognisable credential typed into a goal
S2, S3, S4, S5 = "zq7-SYNTH-secret-1002", "zq7-SYNTH-secret-1003", "zq7-SYNTH-secret-1004", "zq7-SYNTH-secret-1005"
CONTENT = "ZQ-UNMARKED-77 is the launch code for the Orion migration, do not share"  # content no pattern can recognise
NOTES = f"The notes read: {CONTENT}."


@skill(name="test.dm_read", tier="L0", description="Phase 66 fixture: reads content back")
def dm_read() -> SkillResult:
    return SkillResult(speech=NOTES, data={"text": NOTES})


@skill(name="test.dm_raise", tier="L0", description="Phase 66 fixture: raises with a credential in its message")
def dm_raise() -> SkillResult:
    raise RuntimeError(f"upstream said: token={S4} and then more words")


def dump(sql: str, *params) -> str:
    return json.dumps([dict(r) for r in store.conn().execute(sql, params).fetchall()], default=str)


def table_text() -> str:
    """Every row of every table that carries free text, as one string (for 'this appears nowhere')."""
    return "\n".join(dump(f"SELECT * FROM {t}") for t in ("audit", "goals", "episodes", "corrections", "jobs", "job_runs", "memory", "undo"))


def main() -> int:
    t0 = time.perf_counter()
    REGISTRY.discover()
    saved_tools = H.lock_down_real_tools(REGISTRY)
    saved = CFG.desktop_observer.enabled
    CFG.desktop_observer.enabled = False
    saved_content = auditview.CONTENT_RESULT_SKILLS
    auditview.CONTENT_RESULT_SKILLS = saved_content | {"test.dm_read"}  # the fixture stands in for a read-back skill like clipboard.read
    try:
        with iso_env.isolated_runtime():
            run()
    finally:
        auditview.CONTENT_RESULT_SKILLS = saved_content
        CFG.desktop_observer.enabled = saved
        CFG.permissions.overrides = saved_tools
        G.GATE.set()
    return finish("Phase 66 -- data minimization", time.perf_counter() - t0, min_assertions=35, min_scenarios=9)


def run() -> None:
    scenario("0. the tests run on a throwaway database")
    check("0.1 the selected database is a throwaway under the temp folder, not the real one",
          not dbplace.is_real_db(paths.DB_PATH) and Path(paths.DB_PATH).resolve().parent == Path(tempfile.gettempdir()).resolve(), str(paths.DB_PATH))

    # ---------------------------------------------------------------------------------------------------------------
    scenario("A. a real goal run: what the goal's own row keeps")
    goal = f"read the project notes, then write the summary file. my API key is {S1}"
    sub_texts = [f"read the project notes", f"enter my password is {S2} into the form"]
    r = G.run_plan(goal, [call("test.dm_read"), call("test.g_write", {"name": "summary"}), done(NOTES)], subgoals=sub_texts)
    gid = r.data["goal_id"]
    check("A0 (precondition) the run's reply does carry the unrecognisable read-back content, so the checks below mean something", CONTENT in r.speech, r.speech)
    row = dict(store.conn().execute("SELECT * FROM goals WHERE id=?", (gid,)).fetchone())
    check("A1 the user's own words are kept in objective and original_request (resume / widening / correction re-read them)",
          row["objective"] == goal and row["original_request"] == goal)
    check("A2 success_criteria is fixed text -- no copy of the goal", row["success_criteria"] == "The plan reports success and the goal is satisfied.")
    contract = json.loads(row["goal_contract"])
    check("A3 contract.intent holds no copy of the goal and desired_outcome says only what kind of outcome was sought",
          contract["intent"] == "" and contract["desired_outcome"] in ("carry out the request", "report findings only"), str(contract))
    check("A4 no recognisable credential is in the stored contract (its scope's copy of the goal included)", S1 not in row["goal_contract"], row["goal_contract"][:300])
    check("A5 ...the scope is still stored and still describes what the goal allows", "modify" in contract["action_scope"]["allowed"])
    check("A6 the stored subgoals are credential-checked; their statuses are untouched", S2 not in (row["subgoals"] or "") and "[redacted]" in row["subgoals"]
          and all(s["status"] in ("pending", "active", "succeeded", "failed") for s in json.loads(row["subgoals"])))
    check("A7 failure_reason, progress and subgoals/contract together hold no credential and no unmarked content",
          S1 not in json.dumps([row["failure_reason"], row["progress"]]) and CONTENT not in json.dumps([row["failure_reason"], row["progress"], row["goal_contract"], row["subgoals"]]))
    check("A8 the goal's step progress is intact (Phase 54 reconstruction still reproduces the live report)",
          [(s.tool, s.state.value) for s in steps.reconstruct(gid).steps] == [(s["tool"], s["state"]) for s in r.data["report"]["steps"]])

    scenario("B. the audit log: the plan's reply is read-back, not an outcome")
    plan_row = dict(store.conn().execute("SELECT * FROM audit WHERE skill='plan.run' ORDER BY id DESC LIMIT 1").fetchone())
    check("B1 plan.run's long reply is stored as a length, not as the summary of what was read", plan_row["result"].startswith("[read-back content not stored:") and CONTENT not in plan_row["result"], str(plan_row["result"]))
    check("B2 the unrecognisable content appears in no audit row at all, nor in any other table the phase touches",
          CONTENT not in dump("SELECT * FROM audit") and CONTENT not in dump("SELECT * FROM episodes") and CONTENT not in dump("SELECT * FROM jobs"))
    check("B3 the audit status distinctions still work for that row and its steps", audit.status_of(plan_row) == "succeeded"
          and [audit.status_of(x) for x in goal_rows(gid)] == ["succeeded", "succeeded"], str([audit.status_of(x) for x in goal_rows(gid)]))
    r_short = G.run_plan("write the summary file", [call("test.g_write", {"name": "s2"}), done("Wrote it.")])
    short_row = dict(store.conn().execute("SELECT * FROM audit WHERE skill='plan.run' ORDER BY id DESC LIMIT 1").fetchone())
    check("B4 a short reply (an ordinary outcome) is still stored readable", short_row["result"] and short_row["result"].startswith("Wrote it."), str(short_row["result"]))
    check("B5 apps.list (every open window title) is read-back too; short results are unchanged",
          auditview.sanitize_result("apps.list", "You have 9 windows open: " + "Inbox - Payroll, " * 6).startswith("[read-back content not stored")
          and auditview.sanitize_result("apps.list", "Nothing seems to be open.") == "Nothing seems to be open.")
    check("B6 the audit argument for the goal keeps its (credential-checked, bounded) request, as before", S1 not in plan_row["args"] and "project notes" in plan_row["args"])

    scenario("C. episodes keep the goal for retrieval, not the desktop snapshot")
    ep = next(e for e in episodes.recent(20) if e.goal_id == gid)
    check("C1 the episode's goal_text is kept (retrieval needs it) with the credential checked", "project notes" in ep.goal_text and S1 not in ep.goal_text, ep.goal_text)
    check("C2 its ambient context is not stored", ep.context == "")
    eid = episodes.record("check the weather", goal_id=None, context="Active window: Bank - Statement.pdf; contacts: Sam; files: tax_2025.xlsx",
                          steps=[], stopped="completed", ok=True, duration_ms=3)
    check("C3 even when a caller passes a context, nothing of it is kept (the parameter stays for compatibility)",
          "Statement.pdf" not in dump("SELECT * FROM episodes WHERE id=?", eid) and "tax_2025" not in dump("SELECT * FROM episodes WHERE id=?", eid))
    check("C4 plan steps and errors are as before: sanitized arguments, no content", CONTENT not in json.dumps(ep.plan) and ep.stopped == "completed")

    scenario("D. corrections keep the correction, linked to its goal")
    last_goal = goals_mod.most_recent_active()
    SESSION._record_correction_if_any(f"no, I meant the college project, token: {S4}")
    corr = corrections.recent(1)[0]
    check("D1 the correction is stored (bounded, credential-checked) and linked to the goal row", corr.goal_id == last_goal.id and "college project" in corr.user_correction and S4 not in corr.user_correction, str(corr))
    check("D2 the goal's text is not copied into the correction, nor the previous utterance", corr.original_interpretation == "" and corr.context == "")
    g7 = goals_mod.create("open my project", "open my project")
    episodes.record("open my project", goal_id=g7.id, steps=[], stopped="completed", ok=True, duration_ms=1, context="")
    ep7 = next(e for e in episodes.recent(20) if e.goal_id == g7.id)
    SESSION._record_correction_if_any("no, I meant the college project")
    note = experience._correction_for(ep7)
    check("D3 the one reader of corrections (planner experience context) still surfaces a correction for that goal", note is not None and "college project" in note, str(note))
    note_secret = experience._correction_for(next(e for e in episodes.recent(20) if e.goal_id == last_goal.id)) if any(e.goal_id == last_goal.id for e in episodes.recent(20)) else ""
    check("D3b a correction that carried a credential is never surfaced with it", S4 not in str(note_secret))
    cid = corrections.record(f"x {'y' * 900}", goal_id=None, original_interpretation=f"open it, password: {S4}", context=f"my password is {S4}")
    big = dict(store.conn().execute("SELECT * FROM corrections WHERE id=?", (cid,)).fetchone())
    check("D4 the API still accepts every field but bounds each and checks it", len(big["user_correction"]) <= 500 and S4 not in json.dumps(big) and big["original_interpretation"].startswith("open it"), str(big)[:200])

    scenario("E. scheduled jobs: the stored run detail is minimized like an audit row")
    job = jobs.create("dm-job", "interval", {"minutes": 5}, [{"skill": "test.dm_read", "args": {}}, {"skill": "test.dm_raise", "args": {}}])
    seen: list[dict] = []

    async def on_done(event):
        seen.append(dict(event.data))

    BUS.subscribe("job.done", on_done)
    try:
        ok = G.on_loop(jobs.run_job(job.id))
    finally:
        BUS.unsubscribe("job.done", on_done)
    runs = jobs.history(job.id)
    check("E1 the job ran, failed on its second action, and was recorded", ok is False and len(runs) == 1 and runs[0]["ok"] == 0)
    check("E2 job_runs.detail holds neither the read-back content nor the credential from the exception", CONTENT not in runs[0]["detail"] and S4 not in runs[0]["detail"], runs[0]["detail"])
    check("E3 ...but still says which actions ran and what became of them", "test.dm_read:" in runs[0]["detail"] and "test.dm_raise: error" in runs[0]["detail"], runs[0]["detail"])
    hist = G.on_loop(EXECUTOR.run("schedule.history", {"name": "dm-job"}, actor="text"))
    check("E4 schedule.history (which returns the rows) exposes only the minimized detail", CONTENT not in json.dumps(hist.data, default=str) and S4 not in json.dumps(hist.data, default=str))
    check("E5 the in-process job.done event is unchanged (internal subscribers, as before)", seen and CONTENT in seen[0].get("detail", ""))

    scenario("F. a failing check never falls back to storing the original")
    real = toolview.redact_credentials

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    toolview.redact_credentials = boom  # type: ignore[assignment]
    try:
        g2 = goals_mod.create(f"a goal, password: {S3}", f"a goal, password: {S3}", success_criteria=f"password: {S3}")
        goals_mod.update_status(g2.id, goals_mod.GoalStatus.FAILED, failure_reason=f"failed: my API key is {S3}")
        contract2 = goals_mod.GoalContract(known_constraints=[f"User clarified: token = {S3}"], success_conditions=[f"found {S3}"])
        goals_mod.set_contract(g2.id, contract2)
        sg = goals_mod.Subgoal(id="s1", description=f"type my password {S3}")
        goals_mod.set_subgoals(g2.id, [sg])
        cid2 = corrections.record(f"I said password: {S3}", goal_id=g2.id)
        stored = dict(store.conn().execute("SELECT * FROM goals WHERE id=?", (g2.id,)).fetchone())
        stored_corr = dict(store.conn().execute("SELECT * FROM corrections WHERE id=?", (cid2,)).fetchone())
        job2 = jobs.create("dm-job2", "interval", {"minutes": 5}, [{"skill": "test.dm_raise", "args": {}}])
        G.on_loop(jobs.run_job(job2.id))
        detail2 = jobs.history(job2.id)[0]["detail"]
    finally:
        toolview.redact_credentials = real  # type: ignore[assignment]
    check("F1 goal success_criteria, failure_reason, contract and subgoals hold a notice, never the text",
          S3 not in json.dumps({k: stored[k] for k in ("success_criteria", "failure_reason", "goal_contract", "subgoals")}), str(stored)[:300])
    check("F2 ...the notice is the fixed one", "could not be scrubbed" in stored["failure_reason"])
    check("F3 a correction and a job's run detail do the same", S3 not in json.dumps(stored_corr) and S4 not in detail2 and "could not be scrubbed" in stored_corr["user_correction"])

    scenario("G. readers expose only what answers the question")
    gid3 = plan_mod._start_goal(f"look into the build, then report. my API key is {S1}")
    legacy_sub = goals_mod.Subgoal(id="legacy", description=f"report using my password is {S5}")
    store.conn().execute("UPDATE goals SET subgoals=? WHERE id=?", (json.dumps([legacy_sub.to_dict()]), gid3))  # a row written before Phase 66: unchecked
    store.conn().execute("UPDATE goals SET created_at=? WHERE id=?", ("2999-01-01T00:00:00+00:00", gid3))
    store.conn().commit()
    goals_mod.update_status(gid3, goals_mod.GoalStatus.PARTIAL)
    with audit.goal_scope(gid3):  # one recorded step, so there IS an account to give and its remaining-work text is read
        audit.complete(audit.record(actor="text", skill="test.dm_read", tier="L0", args={}, decision="auto"), ok=True, result="Done.", duration_ms=1)
    status = G.on_loop(EXECUTOR.run("meta.goal_status", {}, actor="text"))
    blob = status.speech + json.dumps(status.data, default=str)
    check("G1 the goal-status answer exposes neither the request, nor the stored contract, nor a credential in an old row's remaining-work text",
          S1 not in blob and S5 not in blob and "look into the build" not in blob, status.speech)
    recent = G.on_loop(EXECUTOR.run("meta.recent", {"count": 30}, actor="text"))
    check("G2 the recent-history answer (structured rows) carries no read-back content", CONTENT not in json.dumps(recent.data, default=str) + recent.speech)
    check("G3 the legitimately useful information is still reported: the step, and what was not attempted (with the credential checked)",
          "Done: 'test.dm_read'" in status.speech and "Not attempted: report using my password is [redacted]" in status.speech, status.speech)

    scenario("H. the features that need the raw words still work")
    gid4 = plan_mod._start_goal("Inspect my project.")
    contract4 = plan_mod._seed_contract(gid4, "Inspect my project.")
    plan_mod._action_scope_for(gid4, "Inspect my project.", contract4)
    contract4.success_conditions = ["requirements.txt pins flask==1.0"]
    plan_mod._save_contract(gid4, contract4)
    expanded = plan_mod._expand_goal(gid4, "yes, fix it")
    check("H1 widening ('yes, fix it') still works from the stored objective, scope and findings", expanded is not None and expanded[0] == gid4 and "Inspect my project." in expanded[3]
          and "requirements.txt pins flask==1.0" in expanded[4], str(expanded)[:200])
    kc = goals_mod.get(gid4).contract.known_constraints
    check("H2 the authorising follow-up is still recorded in the user's own words (benign text is unchanged by the check)", any("User authorized: yes, fix it" in c for c in kc), str(kc))
    gid5 = plan_mod._start_goal(f"open it, my password is {S5}")
    resumed = plan_mod._resume_goal(gid5, f"the backend one, token: {S5}")
    check("H3 resuming after a clarification still hands the planner the raw answer for THIS run ...", resumed[0] == gid5 and S5 in resumed[2], str(resumed))
    check("H4 ... while the stored constraint is credential-checked, and the objective needed to resume is intact",
          S5 not in json.dumps(goals_mod.get(gid5).contract.known_constraints) and goals_mod.get(gid5).objective == f"open it, my password is {S5}")
    pend = [dict(id=i, **{"status": s}) for i, s in enumerate(("succeeded", "failed"))]
    rows = [{"decision": "auto", "ok": 1}, {"decision": "auto", "ok": 0}, {"decision": "denied", "ok": None}, {"decision": "blocked", "ok": None},
            {"decision": "auto", "ok": None, "error": audit.OUTCOME_UNKNOWN}, {"decision": "auto", "ok": None, "error": audit.INTERRUPTED}, {"decision": "auto", "ok": None}]
    check("H5 every audit status distinction is still derived", [audit.status_of(x) for x in rows] ==
          ["succeeded", "failed", "denied", "blocked", "outcome_unknown", "interrupted", "unfinished"])

    scenario("I. persistent log lines no longer carry the text")
    lg = logging.getLogger()
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    h = Grab(logging.DEBUG)
    old = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    try:
        distinct = "marmalade-zebra-quokka-plan"
        g6 = goals_mod.create(f"do the {distinct}", f"do the {distinct}")
        corrections.record(f"no I meant the {distinct}", goal_id=g6.id)
        plan_mod._cancel_goal(g6.id, f"do the {distinct}", None)
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)
    check("I1 goal creation, a recorded correction and a cancelled goal log that they happened, with sizes, but not the words", lines and distinct not in "\n".join(lines) and any("goal created" in x for x in lines), "\n".join(lines)[-300:])


def goal_rows(gid: str) -> list[dict]:
    return [dict(r) for r in store.conn().execute("SELECT * FROM audit WHERE goal_id=? AND skill != 'plan.run' ORDER BY id", (gid,)).fetchall()]


if __name__ == "__main__":
    sys.exit(main())
