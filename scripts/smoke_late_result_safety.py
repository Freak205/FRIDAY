"""Phase 97 -- a result that arrives LATE from an abandoned worker is checked exactly like one that arrives on time.

A worker-thread skill that is abandoned (step timeout, cancelled run) keeps running; when it ends, `permissions._record_late_outcome`
writes its real outcome over the "outcome unknown" marker. That row is the only place a late result goes. This phase makes the
path run the SAME failed-result check as the end of `Executor.run` (`_check_failed`: `safe_speech` + `scrub_tree`), the same
exception-text check (`safe_speech`), keeps the outcome classification as it was, and makes every check fail closed.

  A  the on-time path is unchanged (pinned): failed speech/data checked, success left alone, exception text checked, audit redacted
  B  late results through the real Executor + a real cancelled worker thread: success / failure / exception / shell output, each
     carrying a synthetic credential -- the stored row never holds it, the outcome is the right one
  C  late == on-time: the same results through both paths store identical result / error / ok / outcome
  D  non-sensitive diagnostics stay useful (stored exactly as on time)
  E  consumers: no event, no log record, no audit reader (recent / meta.recent / any table of the database) ever sees the canary
  F  failures fail safe: a broken checker, a broken scrubber, an exception that cannot be read -- no canary, outcome still recorded
  G  shell.run late statuses still classify as before (not_started / unknown_outcome / timeout / cancelled), other skills' do not

Only synthetic canary strings; deterministic; every database is a throwaway under the temp folder (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, auditview, permissions, shellproc, store, toolview  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.skills import meta  # noqa: E402

REGISTRY.discover()

PW = "CANARYpw97alphaZ"      # named in a sentence:  "my password is <PW>"
KEY = "CANARYkey97betaZ"     # key=value / header shape
TOK = "CANARYtok97gammaZ"    # a url query token
ALL = (PW, KEY, TOK)

BODY_ENTERED = threading.Event()
RELEASE = threading.Event()
PLAN: dict = {}
STATE = {"late": False}   # True from the moment an abandoned worker is let go until its late write has landed


@skill(name="test.late97", tier="L0", description="Phase 97 worker-thread test skill that returns (or raises) what PLAN says.")
def _late() -> SkillResult:
    BODY_ENTERED.set()
    RELEASE.wait(10)
    if "raise" in PLAN:
        raise PLAN["raise"]
    return SkillResult(speech=PLAN["speech"], ok=PLAN.get("ok", True), data=PLAN.get("data", {}))


class Unreadable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text")


def dump_db() -> str:
    """Every table of the throwaway database as one string -- nothing in there may hold a canary."""
    c = store.conn()
    parts: list[str] = []
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in c.execute(f'SELECT * FROM "{name}"').fetchall():
            parts.append(repr(tuple(row)))
    return "\n".join(parts)


def rows_of(skill_name: str) -> list[dict]:
    return [r for r in reversed(audit.recent(1000)) if r["skill"] == skill_name]


def last(skill_name: str) -> dict:
    return rows_of(skill_name)[-1]


def stored(row: dict) -> tuple:
    return (row["result"], row["error"], row["ok"], row["outcome"])


def leaked(text: str) -> list[str]:
    return [c for c in ALL if c in text]


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.text: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not STATE["late"]:   # only what the late-completion window itself logs (an on-time failure logs its own traceback)
            return
        self.text.append(record.getMessage())
        if record.exc_info:
            self.text.append("".join(traceback.format_exception(*record.exc_info)))


async def settle(skill_name: str, timeout: float = 5.0) -> dict:
    """The newest row of a skill once the late write has replaced the abandoned-step marker."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = last(skill_name)
        if row["error"] != audit.OUTCOME_UNKNOWN_DETACHED:
            return row
        await asyncio.sleep(0.02)
    return last(skill_name)


async def abandoned(name: str, args: dict, entered: threading.Event, release: threading.Event) -> tuple[dict, dict]:
    """Run the real Executor.run, cancel it while its worker is mid-flight, let the worker finish: (row while abandoned, final row)."""
    task = asyncio.create_task(EXECUTOR.run(name, args))
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    mid = last(name)
    STATE["late"] = True
    release.set()
    try:
        final = await settle(name)
        await asyncio.sleep(0.05)   # the worker's own log line comes just after its write
        return mid, final
    finally:
        STATE["late"] = False


async def late_skill(plan: dict) -> tuple[dict, dict]:
    PLAN.clear()
    PLAN.update(plan)
    BODY_ENTERED.clear()
    RELEASE.clear()
    return await abandoned("test.late97", {}, BODY_ENTERED, RELEASE)


async def ontime_skill(plan: dict) -> tuple[SkillResult | None, dict]:
    PLAN.clear()
    PLAN.update(plan)
    RELEASE.set()
    res = None
    with contextlib.suppress(Exception):
        res = await EXECUTOR.run("test.late97", {})
    return res, last("test.late97")


def shell_outcome(outcome: shellproc.Outcome):
    def fake(argv, **kw):
        return outcome
    return fake


SHELL_ARGS = {"command": "echo hi", "shell": "cmd", "cwd": str(ROOT)}


async def shell_late(outcome: shellproc.Outcome) -> tuple[dict, dict]:
    entered, release = threading.Event(), threading.Event()

    def slow(argv, **kw):
        entered.set()
        release.wait(10)
        return outcome

    original = shellproc.run_owned
    shellproc.run_owned = slow
    try:
        return await abandoned("shell.run", SHELL_ARGS, entered, release)
    finally:
        shellproc.run_owned = original


async def shell_ontime(outcome: shellproc.Outcome) -> tuple[SkillResult, dict]:
    original = shellproc.run_owned
    shellproc.run_owned = shell_outcome(outcome)
    try:
        res = await EXECUTOR.run("shell.run", SHELL_ARGS)
    finally:
        shellproc.run_owned = original
    return res, last("shell.run")


async def main_async() -> None:
    EXECUTOR.set_confirm_handler(lambda s, a, p: asyncio.sleep(0, result=(s.name == "shell.run")))
    events: list = []
    after_release: list = []

    async def on_event(ev) -> None:
        events.append(ev)
        if STATE["late"]:
            after_release.append(ev)

    permissions.BUS.subscribe("*", on_event)
    logs = Capture()
    for lg in (logging.getLogger(), permissions.log):
        lg.addHandler(logs)
    columns_before = [r["name"] for r in store.conn().execute("PRAGMA table_info(audit)")]

    FAILED_PROSE = f"Could not log in: my password is {PW}"
    FAILED_KV = f"Request failed: api_key={KEY}"
    FAILED_URL = f"I couldn't fetch it: HTTP 401 for https://example.invalid/x?token={TOK}"

    # ------------------------------------------------------------------------------------------------ A
    scenario("A. the on-time path is unchanged")
    res, row = await ontime_skill({"speech": FAILED_PROSE, "ok": False, "data": {"detail": f"password={PW}", "count": 3, "flag": True}})
    check("A1 a failed result handed back is checked: speech redacted", PW not in res.speech and "[redacted]" in res.speech, res.speech)
    check("A2 ...its data strings are checked, numbers/flags/keys untouched", PW not in str(res.data) and res.data["count"] == 3 and res.data["flag"] is True and "detail" in res.data, str(res.data))
    check("A3 ...the audit row is redacted too, outcome failure, ok 0", not leaked(str(row)) and row["outcome"] == "failure" and row["ok"] == 0, str(row))
    res, row = await ontime_skill({"speech": f"Your clipboard says: my password is {PW}", "ok": True})
    check("A4 a SUCCESSFUL result is handed back exactly as the skill said it (read-back is the point)", PW in res.speech)
    check("A5 ...but what the audit stores is redacted, outcome success", not leaked(str(row)) and row["outcome"] == "success" and row["ok"] == 1, str(row))
    seen_before = len(events)
    res, row = await ontime_skill({"raise": RuntimeError(f"login failed, my password is {PW}")})
    check("A6 an exception on time: the audit error is redacted, failure", not leaked(str(row)) and row["outcome"] == "failure" and row["ok"] == 0, str(row))
    err_events = [e for e in events[seen_before:] if e.topic == "skill.error"]
    check("A7 ...and the event published for it carries no canary", len(err_events) == 1 and not leaked(str(err_events[0].data)), str([e.data for e in err_events]))

    # ------------------------------------------------------------------------------------------------ B
    scenario("B. late results from a really abandoned worker")
    cases = [
        ("late success naming a credential", {"speech": f"Done. my password is {PW}", "ok": True}, "success", 1),
        ("late failure, sentence form", {"speech": FAILED_PROSE, "ok": False, "data": {"detail": f"password={PW}"}}, "failure", 0),
        ("late failure, key=value form", {"speech": FAILED_KV, "ok": False}, "failure", 0),
        ("late failure, url token", {"speech": FAILED_URL, "ok": False}, "failure", 0),
        ("late exception", {"raise": RuntimeError(f"login failed, my password is {PW}")}, "failure", 0),
        ("late exception, key=value", {"raise": ValueError(f"bad header Authorization: Bearer {KEY}")}, "failure", 0),
    ]
    for label, plan, want_outcome, want_ok in cases:
        mid, final = await late_skill(plan)
        check(f"B1 {label}: abandoned first -> unknown_outcome (ok NULL)", mid["outcome"] == "unknown_outcome" and mid["ok"] is None, str(mid))
        check(f"B2 {label}: the final row holds no canary", not leaked(str(final)), str(final))
        check(f"B3 {label}: outcome {want_outcome}, ok {want_ok}", final["outcome"] == want_outcome and final["ok"] == want_ok, str(final))
    for label, outcome, want_outcome in (
        ("late shell, sensitive output in a short failure", shellproc.Outcome(shellproc.COMPLETED, returncode=1, stdout=f"password={PW}", stderr=f"my password is {PW}"), "failure"),
        ("late shell, sensitive output in a success", shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout=f"token={KEY}"), "success"),
        ("late shell, long output with a credential", shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout=("x " * 60) + f"my password is {PW}"), "success"),
    ):
        mid, final = await shell_late(outcome)
        check(f"B4 {label}: no canary in the row, outcome {want_outcome}", not leaked(str(final)) and final["outcome"] == want_outcome and mid["outcome"] == "unknown_outcome", str(final))

    # ------------------------------------------------------------------------------------------------ C
    scenario("C. late == on time: the same result stores the same row")
    battery = [
        ("failed sentence", {"speech": FAILED_PROSE, "ok": False}),
        ("failed key=value", {"speech": FAILED_KV, "ok": False}),
        ("failed url token", {"speech": FAILED_URL, "ok": False}),
        ("failed diagnostic", {"speech": "Exit code 3. disk D: is full (needs 2 GB)", "ok": False}),
        ("success sentence", {"speech": f"Saved. my password is {PW}", "ok": True}),
        ("success plain", {"speech": "Opened the file.", "ok": True}),
        ("exception", {"raise": RuntimeError(f"nope, api_key={KEY}")}),
        ("exception diagnostic", {"raise": FileNotFoundError("no such file: C:\\temp\\notes.txt")}),
    ]
    for label, plan in battery:
        _, on_time = await ontime_skill(plan)
        _, late = await late_skill(plan)
        check(f"C1 {label}: result/error/ok/outcome identical late and on time", stored(on_time) == stored(late), f"{stored(on_time)} != {stored(late)}")
    for label, outcome in (
        ("shell failure", shellproc.Outcome(shellproc.COMPLETED, returncode=2, stderr=f"my password is {PW}")),
        ("shell success", shellproc.Outcome(shellproc.COMPLETED, returncode=0, stdout="all fine")),
        ("shell timeout", shellproc.Outcome(shellproc.TIMEOUT, how="forced", stdout=f"password={PW}")),
    ):
        _, on_time = await shell_ontime(outcome)
        _, late = await shell_late(outcome)
        check(f"C2 {label}: identical late and on time", stored(on_time) == stored(late), f"{stored(on_time)} != {stored(late)}")

    # ------------------------------------------------------------------------------------------------ D
    scenario("D. non-sensitive diagnostics stay useful")
    _, late = await late_skill({"speech": "Exit code 3. disk D: is full (needs 2 GB)", "ok": False})
    check("D1 a failed result's diagnostic is stored word for word", late["result"] == "Exit code 3. disk D: is full (needs 2 GB)", str(late))
    _, late = await late_skill({"raise": FileNotFoundError("no such file: C:\\temp\\notes.txt")})
    check("D2 an exception's diagnostic is stored word for word", late["error"] == "no such file: C:\\temp\\notes.txt", str(late))
    _, late = await late_skill({"speech": f"Exit code 1. cannot open C:\\data\\a.txt; my password is {PW}", "ok": False})
    check("D3 around a credential the rest of the sentence survives", "cannot open C:\\data\\a.txt" in late["result"] and PW not in late["result"], str(late))
    _, late = await late_skill({"speech": "Done.", "ok": True})
    check("D4 an ordinary success is untouched", late["result"] == "Done." and late["outcome"] == "success")

    # ------------------------------------------------------------------------------------------------ E
    scenario("E. no consumer ever sees a canary")
    _, final = await late_skill({"speech": FAILED_PROSE, "ok": False})
    check("E1 no late write of this whole run published any event", after_release == [], str([e.topic for e in after_release]))
    check("E2 no event of this whole run carries a canary (arguments and replies included)", not leaked(" ".join(str(e.data) for e in events if e.topic != "skill.done")), "")
    check("E3 no log record written while a late result was landing (message or traceback) carries a canary", not leaked("\n".join(logs.text)), "")
    check("E3b ...and the capture was live: it saw the late-outcome log lines", any("late outcome recorded" in t for t in logs.text))
    check("E4 audit.recent never returns one", not leaked(str(audit.recent(1000))))
    mr = meta.recent(count=200)
    check("E5 meta.recent (speech and data.actions) never returns one", not leaked(mr.speech + str(mr.data)))
    check("E6 not one table of the database holds one", not leaked(dump_db()), str(leaked(dump_db())))

    # ------------------------------------------------------------------------------------------------ F
    scenario("F. failures fail safe")
    originals = (toolview.safe_speech, toolview.sanitize, auditview.scrub_tree, auditview._scrub)

    def boom(*a, **k):
        raise RuntimeError("checker broke")

    try:
        toolview.safe_speech = boom
        _, final = await late_skill({"speech": FAILED_PROSE, "ok": False})
        check("F1 safe_speech broken: nothing unchecked is kept (the audit's own scrub still ran), outcome still failure",
              not leaked(str(final)) and final["outcome"] == "failure" and final["ok"] == 0, str(final))
        _, final = await late_skill({"raise": RuntimeError(f"my password is {PW}")})
        check("F2 safe_speech broken, exception: no canary, outcome failure", not leaked(str(final)) and final["outcome"] == "failure", str(final))
    finally:
        toolview.safe_speech = originals[0]
    try:
        toolview.sanitize = boom
        _, final = await late_skill({"speech": FAILED_PROSE, "ok": False})
        check("F3 the whole sanitizer broken: the row holds a fixed notice, never the canary, outcome failure",
              not leaked(str(final)) and final["outcome"] == "failure" and final["result"], str(final))
        _, final = await late_skill({"raise": RuntimeError(f"my password is {PW}")})
        check("F4 ...same for an exception", not leaked(str(final)) and final["outcome"] == "failure" and final["error"], str(final))
        _, final = await late_skill({"speech": f"Done. my password is {PW}", "ok": True})
        check("F5 ...and for a success: no canary, outcome success", not leaked(str(final)) and final["outcome"] == "success", str(final))
    finally:
        toolview.sanitize = originals[1]
    r = SkillResult(speech=FAILED_PROSE, ok=False, data={"detail": f"password={PW}"})
    try:
        auditview.scrub_tree = boom
        permissions._check_failed(r)
    finally:
        auditview.scrub_tree = originals[2]
    check("F6 data that cannot be checked is dropped, not handed on", r.data == {} and PW not in r.speech, str((r.speech, r.data)))
    r = SkillResult(speech=FAILED_PROSE, ok=False)
    try:
        toolview.safe_speech = boom
        permissions._check_failed(r)
    finally:
        toolview.safe_speech = originals[0]
    check("F7 speech that cannot be checked is withheld", r.speech == toolview.WITHHELD, r.speech)
    mid, final = await late_skill({"raise": Unreadable()})
    check("F8 an exception whose message cannot be read: fixed notice, outcome failure (the worker's failure is not lost)",
          final["outcome"] == "failure" and final["ok"] == 0 and final["error"] == permissions.LATE_ERROR_UNREADABLE, str(final))
    check("F9 a successful result is never altered by the checker (ok results are left alone)",
          permissions._check_failed(SkillResult(speech=f"my password is {PW}", ok=True)).speech.endswith(PW))
    check("F10 the checkers were all restored", (toolview.safe_speech, toolview.sanitize, auditview.scrub_tree, auditview._scrub) == originals)

    # ------------------------------------------------------------------------------------------------ G
    scenario("G. classification is unchanged")
    for label, outcome, want_outcome, want_ok in (
        ("not_started", shellproc.Outcome(shellproc.NOT_STARTED, detail="no_job"), "not_started", 0),
        ("unknown_outcome", shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed", stdout=f"password={PW}"), "unknown_outcome", None),
        ("timeout", shellproc.Outcome(shellproc.TIMEOUT, how="forced", stdout=f"password={PW}"), "timeout", 0),
        ("cancelled", shellproc.Outcome(shellproc.CANCELLED, how="graceful", stdout=f"password={PW}"), "cancelled", 0),
    ):
        mid, final = await shell_late(outcome)
        check(f"G1 late shell {label}: outcome {want_outcome}, ok {want_ok!r}, no canary",
              final["outcome"] == want_outcome and final["ok"] == want_ok and not leaked(str(final)), str(final))
    for word in ("cancelled", "timeout", "not_started", "unknown_outcome"):
        _, final = await late_skill({"speech": FAILED_PROSE, "ok": False, "data": {"status": word}})
        check(f"G2 another skill's late result carrying status {word!r} is a plain failure", final["outcome"] == "failure" and final["ok"] == 0, str(final))
    original_tree = auditview.scrub_tree
    try:
        auditview.scrub_tree = boom      # the data check fails: the outcome must still come from the result as returned
        statuses = {"timeout": shellproc.Outcome(shellproc.TIMEOUT, how="forced", stdout="x"),
                    "cancelled": shellproc.Outcome(shellproc.CANCELLED, how="graceful", stdout="x"),
                    "unknown_outcome": shellproc.Outcome(shellproc.UNKNOWN, cause=shellproc.TIMEOUT, detail="tree_not_confirmed", stdout="x")}
        for word, outcome in statuses.items():
            _, late = await shell_late(outcome)
            _, on_time = await shell_ontime(outcome)
            check(f"G2b data checker broken, shell {word}: late AND on-time rows keep outcome {word}",
                  late["outcome"] == word and on_time["outcome"] == word, f"{late['outcome']} / {on_time['outcome']}")
    finally:
        auditview.scrub_tree = original_tree
    columns_after = [r["name"] for r in store.conn().execute("PRAGMA table_info(audit)")]
    check("G3 the audit schema is unchanged", columns_before == columns_after)
    check("G4 the whole run left no canary anywhere (database, events, logs)", not leaked(dump_db()) and not leaked("\n".join(logs.text)))

    # ------------------------------------------------------------------------------------------------ H
    scenario("H. the check belongs to the late path itself (it does not depend on the audit's scrubber catching it)")
    original_scrub = auditview._scrub
    try:
        auditview._scrub = lambda text: text      # the audit's own credential scrub switched off
        _, on_time = await ontime_skill({"speech": FAILED_PROSE, "ok": False})
        _, late = await late_skill({"speech": FAILED_PROSE, "ok": False})
        check("H1 failed result, audit scrub off: on time the row holds no canary (the executor's check did it)", not leaked(str(on_time)), str(on_time))
        check("H2 ...and late too -- the late path ran the same check before the audit saw the text", not leaked(str(late)) and stored(late) == stored(on_time), str(late))
        _, late = await late_skill({"raise": RuntimeError(f"login failed, my password is {PW}")})
        check("H3 late exception, audit scrub off: the exception text was checked before it was stored", not leaked(str(late)) and late["outcome"] == "failure", str(late))
        _, late_ok = await late_skill({"speech": f"Done. my password is {PW}", "ok": True})
        _, on_time_ok = await ontime_skill({"speech": f"Done. my password is {PW}", "ok": True})
        check("H4 a late SUCCESS is left alone exactly like an on-time one (the user may have asked for it); only the audit's own storage rules apply",
              leaked(str(late_ok)) == leaked(str(on_time_ok)) and late_ok["outcome"] == "success", f"{late_ok} {on_time_ok}")
    finally:
        auditview._scrub = original_scrub
    _, late = await late_skill({"speech": FAILED_PROSE, "ok": False})
    check("H5 scrub restored: the row is redacted again", not leaked(str(late)) and auditview._scrub is original_scrub)

    permissions.BUS.unsubscribe("*", on_event)
    for lg in (logging.getLogger(), permissions.log):
        lg.removeHandler(logs)


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        asyncio.run(main_async())
    return finish("Phase 97 -- late worker results are checked like on-time ones", time.perf_counter() - t0, min_assertions=70, min_scenarios=7)


if __name__ == "__main__":
    raise SystemExit(main())
