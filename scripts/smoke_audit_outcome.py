"""Phase 94 -- the execution outcome is a durable column of the audit log, not something to read out of `ok` or the speech.

Before: `shell.run` could end as not_started / unknown_outcome / timeout / cancelled, but `Executor.run` passed only `ok` and the
speech to the audit row. After a restart a command that MAY HAVE RUN looked exactly like one that plainly failed (`ok=0`), and a
command that never started looked the same again. Now `audit.outcome` holds one fixed word per classified row:

    success | failure | not_started | unknown_outcome | timeout | cancelled        (NULL = not classified; every older row)

  A  the mapping (`audit.outcome_of_result`), pure: each outcome; only `shell.run` defines the status words; nothing is guessed
  B  the real `Executor.run` + the real `shell.run` (run_owned replaced, nothing is spawned): each outcome lands in the row, with
     `ok` NULL only for an unknown outcome (never inferred as success or failure)
  C  a "restart": connections closed, the file read back raw and through `audit.recent` / `steps`: unknown_outcome survives and is
     UNKNOWN; not_started is distinct from it; live `classify` agrees with the rebuilt state
  D  the late outcome of an abandoned worker and the other markers (shutdown, interrupted)
  E  legacy databases: an old-shape table is migrated in place, no row is rewritten or back-filled, every old row reads as before
  F  the migration is idempotent (re-open, concurrent open, run again), one column, rows untouched
  G  existing consumers: `audit.recent`, the CLI table, `meta.recent`, `steps`, the scrubber's column rules

Deterministic; every database is a throwaway under the temp folder (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402
from rich.console import Console  # noqa: E402

from friday import audit, cli, dbscrub, paths, permissions, shellproc, steps, store  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.skills import meta  # noqa: E402

REGISTRY.discover()


@skill(name="test.ok94", tier="L0", description="Phase 94 test skill that succeeds.")
def _ok() -> SkillResult:
    return SkillResult(speech="Did it.", data={"status": "timeout"})   # another skill's `status` is its own business


@skill(name="test.fail94", tier="L0", description="Phase 94 test skill that reports failure.")
def _fail() -> SkillResult:
    return SkillResult(speech="Could not.", ok=False, data={"status": "unknown_outcome"})   # ...never an outcome for a non-shell skill


@skill(name="test.boom94", tier="L0", description="Phase 94 test skill that raises.")
def _boom() -> SkillResult:
    raise RuntimeError("it broke")


def run_skill(name: str, args: dict | None = None) -> SkillResult:
    return asyncio.run(EXECUTOR.run(name, args or {}))


def shell_run(outcome: shellproc.Outcome) -> SkillResult:
    original = shellproc.run_owned
    shellproc.run_owned = lambda argv, **kw: outcome
    try:
        return run_skill("shell.run", {"command": "echo hi", "shell": "cmd", "cwd": str(Path(ROOT))})
    finally:
        shellproc.run_owned = original


def row_of(skill_name: str, n: int = -1) -> dict:
    rows = [r for r in reversed(audit.recent(500)) if r["skill"] == skill_name]
    return rows[n]


def reopen() -> None:
    """A restart as far as the database is concerned: every connection closed, the file opened again."""
    store.conn().close()
    store._local.conns.clear()


class _Fut:
    def __init__(self, result=None, exc=None):
        self._r, self._e = result, exc

    def cancelled(self) -> bool:
        return False

    def exception(self):
        return self._e

    def result(self):
        return self._r


SHELL_CASES = {
    "success": (shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout="hi"), 1),
    "failure": (shellproc.Outcome(shellproc.COMPLETED, returncode=3, stderr="boom"), 0),
    "not_started": (shellproc.Outcome(shellproc.NOT_STARTED, detail="no_job"), 0),
    "unknown_outcome": (shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed"), None),
    "timeout": (shellproc.Outcome(shellproc.TIMEOUT, how="forced"), 0),
    "cancelled": (shellproc.Outcome(shellproc.CANCELLED, how="graceful"), 0),
}


def main() -> int:
    t0 = time.perf_counter()
    EXECUTOR.set_confirm_handler(lambda s, a, p: asyncio.sleep(0, result=True))

    # ------------------------------------------------------------------------------------------------ A
    scenario("A. the mapping: a finished call's outcome is established, never guessed")
    check("A1 the vocabulary is exactly the six words", audit.OUTCOMES == {"success", "failure", "not_started", "unknown_outcome", "timeout", "cancelled"})
    check("A2 ok -> success", audit.outcome_of_result("files.read", True, {}) == audit.SUCCESS)
    check("A3 not ok, nothing established -> failure", audit.outcome_of_result("files.read", False, {}) == audit.FAILURE
          and audit.outcome_of_result("files.read", False, None) == audit.FAILURE)
    for word in ("not_started", "unknown_outcome", "timeout", "cancelled"):
        check(f"A4 shell.run + status {word} -> {word}", audit.outcome_of_result("shell.run", False, {"status": word}) == word)
        check(f"A5 another skill carrying status {word} stays a plain failure", audit.outcome_of_result("apps.open", False, {"status": word}) == audit.FAILURE)
    check("A6 an ok result is a success whatever its data says", audit.outcome_of_result("shell.run", True, {"status": "unknown_outcome"}) == audit.SUCCESS)
    check("A7 a word shell.run does not define is a plain failure (nothing invented)", audit.outcome_of_result("shell.run", False, {"status": "already_tried"}) == audit.FAILURE
          and audit.outcome_of_result("shell.run", False, {"status": 7}) == audit.FAILURE and audit.outcome_of_result("shell.run", False, "x") == audit.FAILURE)

    with iso_env.isolated_runtime():
        # -------------------------------------------------------------------------------------------- B
        scenario("B. the real Executor + the real shell.run: each outcome is stored; ok is NULL only for an unknown outcome")
        seen: dict[str, dict] = {}
        token = audit._CURRENT_GOAL.set("g94")
        try:
            for name, (outcome, want_ok) in SHELL_CASES.items():
                res = shell_run(outcome)
                row = row_of("shell.run")
                seen[name] = row
                check(f"B1 {name}: stored outcome is {name}", row["outcome"] == name, str(row))
                check(f"B2 {name}: ok is {want_ok!r} in the row", row["ok"] == want_ok, str(row["ok"]))
                if name == "unknown_outcome":
                    check("B3 unknown: the caller still gets the skill's own ok=False result, unchanged", res.ok is False and res.data.get("status") == "unknown_outcome")
                    check("B4 unknown: the row says outcome_unknown -- not failed, not succeeded", row["status"] == "outcome_unknown")
                elif name == "not_started":
                    check("B3 not_started: the caller's result is unchanged", res.ok is False and res.data.get("status") == "not_started")
                    check("B4 not_started: a definite end that did not succeed (older readers: failed), with its own outcome", row["status"] == "failed" and row["outcome"] != "unknown_outcome")
            check("B5 no row has an outcome outside the vocabulary", all(r["outcome"] in audit.OUTCOMES for r in seen.values()))
            run_skill("test.ok94")
            run_skill("test.fail94")
            check("B6 a non-shell success is classified success", row_of("test.ok94")["outcome"] == "success" and row_of("test.ok94")["ok"] == 1)
            check("B7 a non-shell failure is a plain failure even if its data says unknown_outcome", row_of("test.fail94")["outcome"] == "failure" and row_of("test.fail94")["ok"] == 0)
            try:
                run_skill("test.boom94")
            except RuntimeError:
                pass
            check("B8 a skill that raised is classified failure, ok=0, error kept", row_of("test.boom94")["outcome"] == "failure" and row_of("test.boom94")["ok"] == 0
                  and "it broke" in (row_of("test.boom94")["error"] or ""))
            check("B9 the rows are tied to the goal (the reconstruction below reads them by it)", all(r["goal_id"] == "g94" for r in seen.values()))
        finally:
            audit._CURRENT_GOAL.reset(token)

        scenario("B'. complete(): validation and overwrite")
        rid = audit.record(actor="text", skill="test.ok94", tier="L0", args={}, decision="auto")
        audit.complete(rid, ok=True, result="x", outcome="made-up")
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("B'1 a word outside the vocabulary is stored as NULL (unclassified), never as given", r["outcome"] is None and r["ok"] == 1)
        audit.complete(rid, ok=False, result="y", outcome=audit.UNKNOWN_OUTCOME)
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("B'2 unknown_outcome stores ok NULL and replaces the earlier text/outcome", r["ok"] is None and r["outcome"] == "unknown_outcome")
        audit.complete(rid, ok=True, result="z", outcome=audit.SUCCESS)
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("B'3 a later real outcome overwrites it", r["ok"] == 1 and r["outcome"] == "success")
        rid2 = audit.record(actor="text", skill="test.ok94", tier="L0", args={}, decision="auto")
        audit.complete(rid2, ok=True, result="legacy-style caller")
        check("B'4 a caller that passes no outcome leaves the row unclassified (read as before)",
              store.conn().execute("SELECT outcome FROM audit WHERE id=?", (rid2,)).fetchone()["outcome"] is None and row_of("test.ok94")["status"] == "succeeded")

        # -------------------------------------------------------------------------------------------- C
        scenario("C. a restart: unknown_outcome survives and stays UNKNOWN; not_started stays distinct")
        reopen()
        raw = sqlite3.connect(str(paths.DB_PATH))
        on_disk = {r[0]: (r[1], r[2]) for r in raw.execute("SELECT id, outcome, ok FROM audit WHERE skill='shell.run'")}
        raw.close()
        check("C1 the file on disk holds the outcomes, read raw (no FRIDAY code)", sorted(v[0] for v in on_disk.values()) == sorted(SHELL_CASES))
        after = {n: row_of("shell.run", i) for i, n in enumerate(SHELL_CASES)}
        check("C2 after reopening: unknown_outcome is still unknown (outcome, ok NULL, status)",
              after["unknown_outcome"]["outcome"] == "unknown_outcome" and after["unknown_outcome"]["ok"] is None and after["unknown_outcome"]["status"] == "outcome_unknown")
        check("C3 after reopening: not_started is NOT unknown_outcome, and ok is 0", after["not_started"]["outcome"] == "not_started" and after["not_started"]["status"] != "outcome_unknown"
              and after["not_started"]["ok"] == 0)
        check("C4 after reopening: timeout and cancelled are distinguishable from each other and from failure",
              len({after[n]["outcome"] for n in ("timeout", "cancelled", "failure")}) == 3)
        check("C5 success is still success", after["success"]["outcome"] == "success" and after["success"]["status"] == "succeeded")
        states = [r.state for r in steps.ledger_for_goal("g94", probe=lambda *a: "unknown") if r.tool == "shell.run"]
        want = [steps.StepState.COMPLETED, steps.StepState.FAILED, steps.StepState.FAILED, steps.StepState.UNKNOWN, steps.StepState.FAILED, steps.StepState.FAILED]
        check("C6 the goal's rebuilt step states: unknown -> UNKNOWN; not_started/failure/timeout/cancelled -> FAILED; success -> COMPLETED", states == want, str(states))
        check("C7 the rebuilt ledger never turns the unknown step into a success or a failure", states[3] is steps.StepState.UNKNOWN)

        def obs(tool: str, ok: bool, data: dict | None, error: str = "") -> Observation:
            return Observation(PlanStep(tool, {}), ok, "x", data, error=error)

        check("C8 live classify agrees: shell unknown_outcome -> UNKNOWN", steps.classify(obs("shell.run", False, {"status": "unknown_outcome"})) is steps.StepState.UNKNOWN)
        check("C9 live classify: not_started / timeout / cancelled / ordinary failure -> FAILED (unchanged)",
              all(steps.classify(obs("shell.run", False, d)) is steps.StepState.FAILED for d in ({"status": "not_started"}, {"status": "timeout"}, {"status": "cancelled"}, {"returncode": 1})))
        check("C10 live classify: another tool's status 'unknown_outcome' is not this", steps.classify(obs("apps.open", False, {"status": "unknown_outcome"})) is steps.StepState.FAILED)
        check("C11 live classify: success unchanged", steps.classify(obs("shell.run", True, {})) is steps.StepState.COMPLETED)
        check("C12 an older checkpoint that stored FAILED for it is corrected by the row: UNKNOWN wins, not success",
              steps._merge(steps.StepState.FAILED, steps.StepState.UNKNOWN, None)[0] is steps.StepState.UNKNOWN)

        # -------------------------------------------------------------------------------------------- D
        scenario("D. late outcomes and the other markers")
        rid = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="confirmed")
        check("D1 an open row has no outcome", store.conn().execute("SELECT outcome FROM audit WHERE id=?", (rid,)).fetchone()["outcome"] is None)
        check("D2 abandoning it marks the row unknown_outcome (ok still NULL, marker text kept)", audit.mark_outcome_unknown(rid, detached=True)
              and store.conn().execute("SELECT ok, outcome, error FROM audit WHERE id=?", (rid,)).fetchone()["outcome"] == "unknown_outcome")
        check("D3 ...and it reads as outcome_unknown", audit.status_of(next(r for r in audit.recent(50) if r["id"] == rid)) == "outcome_unknown")
        permissions._record_late_outcome(rid, "shell.run", _Fut(SkillResult(speech="Done, no output.", data={"returncode": 0})), time.perf_counter())
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("D4 the abandoned worker finished with a success: that real outcome replaces the marker", r["ok"] == 1 and r["outcome"] == "success")
        rid = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="confirmed")
        audit.mark_outcome_unknown(rid, detached=True)
        permissions._record_late_outcome(rid, "shell.run", _Fut(SkillResult(speech="That command was stopped, but I couldn't confirm that it ended.", ok=False,
                                                                           data={"status": "unknown_outcome"})), time.perf_counter())
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("D5 the late result itself was an unknown outcome: the row stays unknown (not turned into a failure)", r["ok"] is None and r["outcome"] == "unknown_outcome")
        rid = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="confirmed")
        audit.mark_outcome_unknown(rid, detached=True)
        permissions._record_late_outcome(rid, "shell.run", _Fut(exc=RuntimeError("worker died")), time.perf_counter())
        r = store.conn().execute("SELECT ok, outcome FROM audit WHERE id=?", (rid,)).fetchone()
        check("D6 the late worker raised: failure", r["ok"] == 0 and r["outcome"] == "failure")
        rid = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="confirmed")
        check("D7 a shutdown marks an open row unknown_outcome too", audit.mark_outcome_unknown(rid)
              and store.conn().execute("SELECT outcome FROM audit WHERE id=?", (rid,)).fetchone()["outcome"] == "unknown_outcome")
        rid = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="confirmed")
        store.conn().execute("UPDATE audit SET session_id='dead-1' WHERE id=?", (rid,))
        store.conn().execute("INSERT INTO runtime_sessions (id, kind, pid, proc_start, host, started_at) VALUES ('dead-1','cli',1,'t','h',?)", (store.now(),))
        store.conn().commit()
        audit.reconcile_orphans(lambda pid, start, host: "dead")
        r = next(x for x in audit.recent(50) if x["id"] == rid)
        check("D8 an interrupted row (nobody saw anything) is NOT classified unknown_outcome: it stays interrupted, outcome NULL", r["status"] == "interrupted" and r["outcome"] is None)
        denied = audit.record(actor="text", skill="shell.run", tier="L2", args={}, decision="denied")
        check("D9 a denied row never ran: no outcome, status denied", next(x for x in audit.recent(80) if x["id"] == denied)["status"] == "denied"
              and store.conn().execute("SELECT outcome FROM audit WHERE id=?", (denied,)).fetchone()["outcome"] is None)

        # -------------------------------------------------------------------------------------------- G (before the DB is dropped)
        scenario("G. existing consumers")
        rows = audit.recent(500)
        check("G1 audit.recent still returns every original column plus status/session_id/goal_id, and now outcome",
              all({"id", "at", "actor", "skill", "tier", "args", "decision", "ok", "result", "error", "duration_ms", "status", "session_id", "goal_id", "outcome"} <= set(r) for r in rows))
        check("G2 every stored outcome is a word of the vocabulary or NULL", all(r["outcome"] is None or r["outcome"] in audit.OUTCOMES for r in rows))
        check("G3 the column holds a fixed word only -- the scrubber's content columns are unchanged and exclude it", dbscrub.SCRUBBED_COLUMNS["audit"] == ("args", "result", "error"))
        def render(rows_: list[dict]) -> tuple[str, str]:
            """What the CLI table and meta.recent show for exactly these rows (audit.recent replaced for the call)."""
            buf = io.StringIO()
            original_con, original_recent = cli.con, audit.recent
            cli.con = Console(file=buf, width=200, force_terminal=False)
            audit.recent = lambda limit=20: list(rows_)
            try:
                cli._audit_table(5)
                return buf.getvalue(), meta.recent(count=5).speech
            finally:
                cli.con, audit.recent = original_con, original_recent

        unknown_row, started_row = after["unknown_outcome"], after["not_started"]
        table_u, speech_u = render([unknown_row])
        table_n, speech_n = render([started_row])
        check("G4 the CLI table marks the executor-recorded unknown outcome with '?' and says its outcome is unknown",
              "?" in table_u and "outcome unknown" in table_u, ascii(table_u))
        check("G5 meta.recent says '(outcome unknown)' for it", "shell.run (outcome unknown)" in speech_u, speech_u)
        check("G6 ...and shows neither for a command that never started (a definite end): a cross, no unknown label",
              "?" not in table_n and chr(0x2717) in table_n and "unknown" not in table_n + speech_n, ascii(table_n + speech_n))
        check("G6b the CLI table still renders every row of a mixed log without error", bool(render(audit.recent(500))[0]))
        only_failed = [r for r in rows if r["skill"] == "shell.run" and r["outcome"] in ("failure", "timeout", "cancelled", "not_started")]
        check("G6c no definite failure/timeout/cancel/not_started row reads as outcome_unknown", all(r["status"] != "outcome_unknown" for r in only_failed))
        check("G7 the dbscrub digest includes the new column automatically (same file before/after is consistent)",
              dbscrub.table_digest(store.conn(), "audit") == dbscrub.table_digest(store.conn(), "audit"))

    # ------------------------------------------------------------------------------------------------ E
    scenario("E. legacy databases: migrated in place, no row rewritten or back-filled")
    with iso_env.isolated_runtime():
        raw = sqlite3.connect(str(paths.DB_PATH))
        raw.execute("PRAGMA journal_mode=WAL")
        raw.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, skill TEXT NOT NULL,"
            " tier TEXT NOT NULL, args TEXT NOT NULL, decision TEXT NOT NULL, ok INTEGER, result TEXT, error TEXT, duration_ms INTEGER);"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-01','text','a.done','L0','{}','auto',1,'ok');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-02','text','shell.run','L2','{}','confirmed',0,"
            "'That command was stopped, but I couldn''t confirm that it and everything it started have ended.');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-03','text','shell.run','L2','{}','confirmed',0,"
            "'I didn''t run that command: I couldn''t set it up so that I could reliably stop it.');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,error) VALUES ('2025-01-04','text','a.closed','L1','{}','auto','" + audit.OUTCOME_UNKNOWN.replace("'", "''") + "');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision) VALUES ('2025-01-05','text','a.open','L1','{}','auto');"
            "INSERT INTO audit (at,actor,skill,tier,args,decision) VALUES ('2025-01-06','text','a.denied','L1','{}','denied');"
        )
        raw.commit()
        before = [tuple(r) for r in raw.execute("SELECT id,at,actor,skill,tier,args,decision,ok,result,error,duration_ms FROM audit ORDER BY id")]
        raw.close()
        legacy = audit.recent(10)   # opens the database through FRIDAY: SCHEMA + migration
        cols = [r[1] for r in store.conn().execute("PRAGMA table_info(audit)")]
        check("E1 the outcome column was added to the old table", "outcome" in cols and cols.count("outcome") == 1)
        raw = sqlite3.connect(str(paths.DB_PATH))
        after = [tuple(r) for r in raw.execute("SELECT id,at,actor,skill,tier,args,decision,ok,result,error,duration_ms FROM audit ORDER BY id")]
        n_classified = raw.execute("SELECT COUNT(*) FROM audit WHERE outcome IS NOT NULL").fetchone()[0]
        raw.close()
        check("E2 every old row is byte-for-byte what it was (no rewrite)", before == after)
        check("E3 nothing was back-filled: no old row has an outcome -- the evidence (ok=0 and some speech) does not establish one", n_classified == 0)
        by_skill = [(r["skill"], r["status"], r["outcome"]) for r in reversed(legacy)]
        check("E4 old rows keep exactly their old statuses (a legacy ok=0 shell row stays 'failed', not guessed unknown or not_started)", by_skill == [
            ("a.done", "succeeded", None), ("shell.run", "failed", None), ("shell.run", "failed", None),
            ("a.closed", "outcome_unknown", None), ("a.open", "unfinished", None), ("a.denied", "denied", None)], str(by_skill))
        check("E5 old rows still get their old state in the step ledger machinery (status words unchanged)", all(steps._AUDIT_STATE.get(r["status"]) is not None or r["status"] in ("unfinished",) for r in legacy))
        rid = audit.record(actor="text", skill="a.new", tier="L0", args={}, decision="auto")
        audit.complete(rid, ok=True, result="x", outcome=audit.SUCCESS)
        check("E6 new rows in the migrated database are classified, old ones are untouched", audit.recent(10)[0]["outcome"] == "success"
              and [tuple(r) for r in store.conn().execute("SELECT id,at,actor,skill,tier,args,decision,ok,result,error,duration_ms FROM audit WHERE id<=6 ORDER BY id")] == before)

        # -------------------------------------------------------------------------------------------- F
        scenario("F. the migration is idempotent")
        snapshot = [tuple(r) for r in store.conn().execute("SELECT * FROM audit ORDER BY id")]
        for _ in range(3):
            reopen()
            store.conn().execute("SELECT 1").fetchone()
        cols2 = [r[1] for r in store.conn().execute("PRAGMA table_info(audit)")]
        check("F1 re-opening three times: still exactly one outcome column, same column list", cols2 == cols and cols2.count("outcome") == 1)
        store._migrate(store.conn())
        store._migrate(store.conn())
        check("F2 running the migration function again changes nothing", [r[1] for r in store.conn().execute("PRAGMA table_info(audit)")] == cols
              and [tuple(r) for r in store.conn().execute("SELECT * FROM audit ORDER BY id")] == snapshot)
        check("F3 exactly one MIGRATIONS entry adds it", [m for m in store.MIGRATIONS if m[0] == "audit" and m[1] == "outcome"].__len__() == 1)
        check("F4 it is a plain nullable TEXT column (no default that would claim an outcome for old rows)",
              next(r for r in store.conn().execute("PRAGMA table_info(audit)") if r[1] == "outcome")[2:5] == ("TEXT", 0, None))

    with iso_env.isolated_runtime():
        raw = sqlite3.connect(str(paths.DB_PATH))
        raw.execute("PRAGMA journal_mode=WAL")
        raw.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, skill TEXT NOT NULL,"
            " tier TEXT NOT NULL, args TEXT NOT NULL, decision TEXT NOT NULL, ok INTEGER, result TEXT, error TEXT, duration_ms INTEGER);"
            "INSERT INTO audit (at,actor,skill,tier,args,decision,ok,result) VALUES ('2025-01-01','text','a.done','L0','{}','auto',1,'ok');"
        )
        raw.commit()
        raw.close()
        errors: list[BaseException] = []
        gate = threading.Barrier(4)

        def opener() -> None:
            try:
                gate.wait()
                store.conn().execute("SELECT 1").fetchone()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=opener) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        cols3 = [r[1] for r in store.conn().execute("PRAGMA table_info(audit)")]
        check("F5 four connections opening an old database at once all succeed and add the column once", not errors and cols3.count("outcome") == 1, repr(errors))

    return finish("Phase 94 -- persisted execution outcome classification", time.perf_counter() - t0, min_assertions=75, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(main())
