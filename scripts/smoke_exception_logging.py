"""Phase 98 -- an exception logged from an execution path reaches every log handler already checked, and still says where and what.

Before: every `log.exception(...)` on an execution path put the exception OBJECT in the record. Only the two handlers `friday.log.setup` builds
(each with the Phase 64 `RedactingFilter`) credential-checked it, at output time: a record seen by any other handler, or by a test, carried the raw
message, every chained message and a traceback with source lines; and when that check failed the whole line was withheld, type and location with it.

Now `friday.log.safe_exc_text` / `log_exception` describe the exception at the call site: type, credential-checked message (the filter's own check,
`_redact_log_text` -> `auditview.scrub_free_text`), location (file name, line, function -- no source text), and the chain with each link checked.
The record carries that text and NO exception object. The 14 sites on the execution paths (Executor.run + worker completion, the session's direct
call, the orchestrator's tool call, verification, scheduled jobs, undo) use it; the handler filter stays as the second layer.

  A  the formatter alone: a credential in the message / a chained exception / nested frames; diagnostics stay useful; bounds; hostile exceptions
  B  `log_exception`: the record holds no exception object, no traceback text, no raw message
  C  the real paths, each captured by a handler with NO filter: a canary never appears; the type and the place do
  D  failures fail safe: the credential check broken, a message that cannot be read, the whole formatter broken -> fixed diagnostics
  E  unchanged: the audit rows, the speech of a failed call, the exception the caller receives, the outcome classification, the handler filters

Synthetic canary strings only; throwaway databases (`iso_env`); data/ is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import ScriptedPlanner, call, check, done, finish, scenario  # noqa: E402

from friday import audit, auditview, intent, jobs, permissions, store, undo, verify  # noqa: E402
from friday import log as friday_log  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.orchestrator import Orchestrator, _tool_specs  # noqa: E402
from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

REGISTRY.discover()

PW = "CANARYpw98alphaZ"
KEY = "CANARYkey98betaZ"
TOK = "CANARYtok98gammaZ"
LIT = "CANARYlit98deltaZ"        # appears only as a literal in SOURCE lines, never in a message
ALL = (PW, KEY, TOK, LIT)


def leaked(text: str) -> list[str]:
    return [c for c in ALL if c in text]


# ------------------------------------------------------------------------------------------------ fixtures
@skill(name="test.boom98", tier="L0", description="Phase 98 skill that raises with a credential in its message.")
def _boom() -> SkillResult:
    raise RuntimeError(f"login failed, my password is {PW}")


@skill(name="test.chain98", tier="L0", description="Phase 98 skill that raises a chained exception.")
def _chain() -> SkillResult:
    try:
        raise KeyError(f"api_key={KEY}")
    except KeyError as inner:
        raise ValueError(f"could not use the stored settings: token={TOK}") from inner


@skill(name="test.diag98", tier="L0", description="Phase 98 skill that raises an ordinary exception.")
def _diag() -> SkillResult:
    raise FileNotFoundError("no such file: C:\\temp\\notes.txt")


@skill(name="test.ok98", tier="L0", description="Phase 98 skill that succeeds.")
def _ok() -> SkillResult:
    return SkillResult(speech="Did it.")


@skill(name="test.undoable98", tier="L1", action="modify", description="Phase 98 skill with an undo.",
       undo=lambda: {"skill": "test.ok98", "args": {}})
def _undoable() -> SkillResult:
    return SkillResult(speech="Changed it.")


def _bad_risk(**kw) -> bool:
    raise RuntimeError(f"classifier saw password={PW}")


@skill(name="test.risky98", tier="L0", description="Phase 98 skill whose risk classifier raises.", risk=_bad_risk)
def _risky() -> SkillResult:
    return SkillResult(speech="fine")


class Unreadable(Exception):
    def __str__(self) -> str:
        raise RuntimeError(f"__str__ saw {PW}")


class Capture(logging.Handler):
    """A handler with NO filter -- what any handler the project does not build would see. It is placed FIRST among the root's handlers and
    snapshots each record as it arrives: the project's RedactingFilter rewrites the shared record in place for the handlers after it, so
    reading the record later would show the filter's work, not the call site's."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[SimpleNamespace] = []
        self.all: list[SimpleNamespace] = []      # never cleared: the whole run

    def emit(self, record: logging.LogRecord) -> None:
        trace = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info and record.exc_info[0] is not None else ""
        snap = SimpleNamespace(
            name=record.name, levelno=record.levelno, message=record.getMessage(), has_exc=record.exc_info is not None,
            exc_text=record.exc_text or "", stack_info=record.stack_info, trace=trace, raw=repr(record.__dict__),
        )
        self.records.append(snap)
        self.all.append(snap)

    def lines(self) -> list[str]:
        out = []
        for r in self.records:
            out.append(r.message)
            if r.trace:
                out.append(r.trace)
            if r.exc_text:
                out.append(r.exc_text)
        return out

    def text(self) -> str:
        return "\n".join(self.lines())

    def clear(self) -> None:
        self.records.clear()


def deep(n: int, secret: str):
    if n == 0:
        raise RuntimeError(f"deep failure password={secret}")
    return deep(n - 1, secret)


def outer_call():
    return deep(3, PW)            # a source line that would be printed by traceback.format_exc


def raise_with_literal():
    return deep(1, "CANARYlit98deltaZ")      # the literal is in this SOURCE line only


def raised(fn, *a):
    try:
        fn(*a)
    except BaseException as exc:  # noqa: BLE001
        return exc
    return None


async def main_async() -> None:
    cap = Capture()
    root = logging.getLogger()
    root.handlers.insert(0, cap)   # first: it sees each record before any project filter rewrites it
    previous = {n: logging.getLogger(n).level for n in ("friday.verify", "friday.permissions", "friday.orchestrator", "friday.undo")}
    for n in previous:
        logging.getLogger(n).setLevel(logging.DEBUG)
    EXECUTOR.set_confirm_handler(lambda s, a, p: asyncio.sleep(0, result=True))
    original_redact = friday_log._redact_log_text

    # ------------------------------------------------------------------------------------------------ A
    scenario("A. the formatter: credentials out, type and place kept")
    exc = raised(deep, 0, PW)
    text = friday_log.safe_exc_text(exc)
    check("A1 a credential in the message: gone, replaced by the filter's own marker", PW not in text and "[redacted]" in text, text)
    check("A2 ...the exception type and the place it was raised are kept", text.startswith("RuntimeError:") and "smoke_exception_logging.py:" in text and " in deep" in text, text)
    exc = raised(lambda: (_ for _ in ()).throw(ValueError(f"Authorization: Bearer {TOK}")))
    check("A3 a header-shaped credential: gone", TOK not in friday_log.safe_exc_text(exc))
    exc = raised(_chain)
    text = friday_log.safe_exc_text(exc)
    check("A4 chained (`raise ... from ...`): BOTH messages checked, both types named, the link is named", KEY not in text and TOK not in text
          and "ValueError" in text and "KeyError" in text and "caused by" in text, text)
    standard = "".join(traceback.format_exception(exc))
    check("A5 (control) Python's own traceback text prints the chained message verbatim -- the reason for describing each link", KEY in standard)

    def implicit():
        try:
            raise OSError(f"disk said password={PW}")
        except OSError:
            raise RuntimeError("then the handler failed")      # implicit context, no `from`

    exc = raised(implicit)
    text = friday_log.safe_exc_text(exc)
    check("A6 an implicit chain (an error while handling an error): the earlier message is checked too", PW not in text and "OSError" in text and "during handling of" in text, text)

    exc = raised(outer_call)
    text = friday_log.safe_exc_text(exc)
    check("A7 nested frames: the innermost frames are listed, outermost first, as file:line in function", text.index(" in outer_call") < text.index(" in deep") and text.count(" in deep") >= 1, text)
    exc = raised(raise_with_literal)
    text = friday_log.safe_exc_text(exc)
    standard = "".join(traceback.format_exception(exc))
    check("A8 (control) the standard traceback prints the source line holding the literal", LIT in standard)
    check("A9 the description holds NO source text: the literal in a source line is not in it", LIT not in text, text)
    exc = raised(deep, 40, "x")
    text = friday_log.safe_exc_text(exc)
    check("A10 a deep stack is bounded to its last 8 frames and says it was cut", text.count(" in deep") == 8 and "... >" in text, text)

    exc = raised(lambda: (_ for _ in ()).throw(FileNotFoundError("no such file: C:\\temp\\notes.txt")))
    text = friday_log.safe_exc_text(exc)
    check("A11 an ordinary exception stays useful: type, the full message, the place", "FileNotFoundError: no such file: C:\\temp\\notes.txt [" in text and "smoke_exception_logging.py" in text, text)
    exc = raised(lambda: (_ for _ in ()).throw(RuntimeError("x" * 2000)))
    text = friday_log.safe_exc_text(exc)
    check("A12 a long message is bounded and says how much was cut", "...[+1700 chars]" in text and len(text) < 600, str(len(text)))
    exc = raised(lambda: (_ for _ in ()).throw(RuntimeError("line one\nFAKE 2026-01-01 ERROR forged entry\r\nline three")))
    text = friday_log.safe_exc_text(exc)
    check("A13 a message cannot start a new log line (control characters flattened)", "\n" not in text and "\r" not in text and "FAKE" in text)
    a, b = RuntimeError(f"a password={PW}"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    text = friday_log.safe_exc_text(a)
    check("A14 a cycle in the chain terminates and is still checked", PW not in text and text.count("RuntimeError") <= 2, text)
    long_chain = RuntimeError("end")
    for i in range(12):
        nxt = RuntimeError(f"link {i} password={PW}")
        nxt.__cause__ = long_chain
        long_chain = nxt
    text = friday_log.safe_exc_text(long_chain)
    check("A15 a long chain is bounded to 5 links and says more exist", text.count("RuntimeError") == 5 and "more chained exceptions not shown" in text and PW not in text, text)
    with contextlib.suppress(Exception):
        try:
            raise RuntimeError(f"live password={PW}")
        except RuntimeError:
            text = friday_log.safe_exc_text()
    check("A16 with no argument it describes the exception being handled", "RuntimeError" in text and PW not in text, text)
    check("A17 with nothing active it says so, and does not raise", friday_log.safe_exc_text() == "no exception was active")
    exc = raised(lambda: (_ for _ in ()).throw(ExceptionGroup("several", [OSError(f"a password={PW}"), ValueError("b")])))
    check("A18 an exception group does not break it", PW not in friday_log.safe_exc_text(exc))

    def suppressed():
        try:
            raise OSError(f"earlier trouble password={PW}")
        except OSError:
            raise RuntimeError("the new error") from None

    text = friday_log.safe_exc_text(raised(suppressed))
    check("A19 `from None` suppresses the context, as Python's own traceback does: the earlier exception is not described", "OSError" not in text and "during handling" not in text and "RuntimeError: the new error" in text, text)
    noted = RuntimeError("the visible message")
    noted.add_note(f"a note holding password={PW}")
    text = friday_log.safe_exc_text(noted)
    check("A20 PEP 678 notes are never read into the line", PW not in text and "note" not in text and "the visible message" in text, text)
    text = friday_log.safe_exc_text(KeyError(f"password={PW}"))
    check("A21 a KeyError (whose str() is the repr of its key) is checked like any message", PW not in text and text.startswith("KeyError:"), text)
    text = friday_log.safe_exc_text(RuntimeError("a\u0085b\u202ec\u200bd\x1be\u2028f"))
    check("A22 line breaks, bidi marks, zero-width and C1 characters are flattened", all(ch not in text for ch in "\u0085\u202e\u200b\x1b\u2028"), repr(text))
    started = time.perf_counter()
    huge = "a" * 7990 + f"my password is {PW}" + "z" * 5_000_000
    text = friday_log.safe_exc_text(RuntimeError(huge))
    elapsed = time.perf_counter() - started
    check("A23 a multi-megabyte message: the credential at the scan edge is gone, the line is short, it says how much was cut, and it is quick",
          "CANARY" not in text and len(text) < 700 and "...[+" in text and elapsed < 1.0, f"{len(text)} {elapsed:.2f}s")
    straddle = "x" * 7985 + f"token={TOK}" + "y" * 100
    text = friday_log.safe_exc_text(RuntimeError(straddle))
    check("A24 a credential that straddles the scan limit leaves no half-token behind", "CANARY" not in text and "tok98" not in text, text[-120:])
    handed: list[int] = []
    real_redact = friday_log._redact_log_text
    friday_log._redact_log_text = lambda t: (handed.append(len(t)), real_redact(t))[1]
    try:
        friday_log.safe_exc_text(RuntimeError("q" * 3_000_000))
    finally:
        friday_log._redact_log_text = real_redact
    check("A26 the credential check is never handed more than 8000 characters of one message (a bound on the work, not on the clock)", handed == [8000], str(handed))
    shrunk = "see https://example.invalid/p?" + "a=1&" * 2100 + "tail"
    text = friday_log.safe_exc_text(RuntimeError(shrunk))
    check("A25 when the check shortens a message that was cut at the scan limit, the stretch at the edge is dropped too (nothing half-checked survives)",
          "example.invalid" not in text and "...[+" in text, text)

    # ------------------------------------------------------------------------------------------------ B
    scenario("B. log_exception: what the record holds")
    cap.clear()
    probe = logging.getLogger("friday.permissions")
    try:
        raise RuntimeError(f"probe my password is {PW}")
    except RuntimeError:
        friday_log.log_exception(probe, "probe failed for %s (%d)", "thing", 7)
    rec = cap.records[-1]
    check("B1 the record has no exception object, no exception text, no stack", not rec.has_exc and not rec.exc_text and rec.stack_info is None and not rec.trace)
    check("B2 a handler with no filter sees no canary, and sees the formatted arguments", not leaked(cap.text()) and "probe failed for thing (7) -- RuntimeError" in cap.text(), cap.text())
    check("B3 it is an ERROR line by default, like logger.exception", rec.levelno == logging.ERROR)
    friday_log.log_exception(probe, "debug probe", level=logging.DEBUG, exc=ValueError(f"token={TOK}"))
    check("B4 the level and an explicit exception are honoured", cap.records[-1].levelno == logging.DEBUG and not leaked(cap.text()) and "ValueError" in cap.records[-1].message)
    check("B5 nothing on the record, in any attribute, holds a canary", not any(leaked(r.raw) for r in cap.records))
    cap.clear()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        friday_log.log_exception(probe, "100% done")
        friday_log.log_exception(probe, "fail %s")
        friday_log.log_exception(probe, "fail %s %s", "only-one")
    msgs = [r.message for r in cap.records]
    check("B6 a literal % with no arguments, a placeholder with none, or too few arguments: logged as written, never a logging error",
          len(msgs) == 3 and msgs[0].startswith("100% done -- RuntimeError") and msgs[1].startswith("fail %s -- RuntimeError") and msgs[2].startswith("fail %s %s -- RuntimeError"), str(msgs))
    cap.clear()
    counted = {"n": 0}
    real_safe = friday_log.safe_exc_text
    friday_log.safe_exc_text = lambda e=None: (counted.__setitem__("n", counted["n"] + 1), real_safe(e))[1]
    off = logging.getLogger("friday.switched_off98")
    off.setLevel(logging.CRITICAL)
    try:
        friday_log.log_exception(off, "never", exc=RuntimeError("x"), level=logging.DEBUG)
        friday_log.log_exception(off, "never either", exc=RuntimeError("x"))
        none_when_off = counted["n"] == 0 and not cap.records
        off.setLevel(logging.DEBUG)
        friday_log.log_exception(off, "now", exc=RuntimeError("x"), level=logging.DEBUG)
    finally:
        friday_log.safe_exc_text = real_safe
    check("B7 a level that is switched off costs nothing: the exception is not even described; switched on, it is", none_when_off and counted["n"] == 1 and len(cap.records) == 1)

    # ------------------------------------------------------------------------------------------------ C
    scenario("C. the real execution paths")
    cap.clear()
    caught = None
    try:
        await EXECUTOR.run("test.boom98", {})
    except RuntimeError as e:
        caught = e
    mine = [r for r in cap.records if r.name == "friday.permissions" and "skill test.boom98 failed" in r.message]
    check("C1 Executor.run: a skill that raised is logged with its type and place, no canary",
          len(mine) == 1 and not leaked(cap.text()) and "RuntimeError" in mine[0].message and "_boom" in mine[0].message, cap.text())
    check("C2 ...as an error record with no exception object attached", mine and mine[0].levelno == logging.ERROR and not mine[0].has_exc)
    check("C3 ...and the caller still receives the original exception, unchanged", isinstance(caught, RuntimeError) and PW in str(caught))
    cap.clear()
    with contextlib.suppress(ValueError):
        await EXECUTOR.run("test.chain98", {})
    check("C4 Executor.run: a chained exception is logged with both links checked", not leaked(cap.text()) and "ValueError" in cap.text() and "KeyError" in cap.text(), cap.text())
    cap.clear()
    with contextlib.suppress(FileNotFoundError):
        await EXECUTOR.run("test.diag98", {})
    check("C5 Executor.run: an ordinary failure stays useful in the log (type, message, place)", "FileNotFoundError: no such file: C:\\temp\\notes.txt" in cap.text() and "_diag" in cap.text(), cap.text())

    # worker completion: the late write fails with a credential in the database error
    cap.clear()
    original_complete = audit.complete

    def broken_complete(*a, **kw):
        raise RuntimeError(f"database error, password={PW}")

    rid = audit.record(actor="text", skill="test.ok98", tier="L0", args={}, decision="auto")
    audit.mark_outcome_unknown(rid, detached=True)
    audit.complete = broken_complete
    try:
        fut = SimpleNamespace(cancelled=lambda: False, exception=lambda: None, result=lambda: SkillResult(speech="Done."))
        permissions._record_late_outcome(rid, "test.ok98", fut, time.perf_counter())
    finally:
        audit.complete = original_complete
    check("C6 worker completion: a failed late write is logged with type and place, no canary", "could not record the late outcome" in cap.text() and "RuntimeError" in cap.text() and not leaked(cap.text()), cap.text())

    # the cancel branch's audit close fails
    cap.clear()
    original_publish = permissions.BUS.publish

    async def cancelled_publish(topic, **kw):
        if topic == "skill.start":
            raise asyncio.CancelledError()
        return await original_publish(topic, **kw)

    permissions.BUS.publish = cancelled_publish
    audit.complete = broken_complete
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await EXECUTOR.run("test.ok98", {})
    finally:
        permissions.BUS.publish = original_publish
        audit.complete = original_complete
    check("C7 Executor.run: a failed audit close (cancel before start) is logged without the canary", "could not close the audit row" in cap.text() and not leaked(cap.text()), cap.text())
    leftovers = [r for r in audit.recent(500) if r["skill"] == "test.ok98" and r["ok"] is None and r["error"] is None and r["decision"] == "auto"]
    for r in leftovers:
        audit.complete(r["id"], ok=False, outcome=audit.NOT_STARTED, error="closed by the test")

    # undo registration fails
    cap.clear()
    original_register = audit.register_undo
    audit.register_undo = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"journal error api_key={KEY}"))
    try:
        await EXECUTOR.run("test.undoable98", {})
    finally:
        audit.register_undo = original_register
    check("C8 Executor.run: a failed undo registration is logged without the canary", "undo registration failed for test.undoable98" in cap.text() and not leaked(cap.text()), cap.text())

    # action recording fails
    cap.clear()
    original_record = INTEL.record_action

    def bad_record(*a, **k):
        raise RuntimeError(f"state error password={PW}")

    INTEL.record_action = bad_record
    try:
        await EXECUTOR.run("test.ok98", {})
    finally:
        INTEL.record_action = original_record
    check("C9 Executor.run: a failed action recording is logged without the canary", "action recording failed" in cap.text() and not leaked(cap.text()), cap.text())

    # the risk classifier raises
    cap.clear()
    res = await EXECUTOR.run("test.risky98", {})
    check("C10 Executor.run: a risk classifier that raised is logged without the canary, and still fails safe (the call ran under L3 policy)",
          "risk classifier failed for test.risky98" in cap.text() and not leaked(cap.text()) and res.ok, cap.text())

    # the orchestrator's tool call
    cap.clear()
    CFG.planner.structured_output = False
    CFG.desktop_observer.enabled = False
    INTEL.reset()

    async def raising_runner(tool, args, actor):
        raise RuntimeError(f"tool crashed, my password is {PW}")

    goal = "Look at the boom98 thing"
    orch = Orchestrator(tools=["test.boom98"], runner=raising_runner, actor="test", llm_provider=ScriptedPlanner([call("test.boom98"), done("could not")]),
                        tool_specs=_tool_specs(["test.boom98"]), max_steps=4)
    result = await orch.run_goal(goal, action_scope=intent.derive_scope(goal), max_replans=0)
    check("C11 the orchestrator's tool call: logged with type and place, no canary", "orchestrator: tool test.boom98 raised" in cap.text() and "RuntimeError" in cap.text() and not leaked(cap.text()), cap.text())
    check("C11b ...and the run's outcome is what it was: the step failed, the goal did not succeed", result is not None and not result.ok)

    # the session's direct call
    cap.clear()
    res = await SESSION._run("test.boom98", {}, actor="text")
    check("C12 the session's direct call: logged with type and place, no canary", "skill test.boom98 failed" in cap.text() and not leaked(cap.text()), cap.text())
    check("C12b ...and its spoken reply is exactly what it was (the library text, credential-checked)", res.ok is False and res.speech.startswith("That failed:") and PW not in res.speech and "[redacted]" in res.speech, res.speech)

    # verification
    cap.clear()

    def prepare(args):
        raise RuntimeError(f"baseline read failed password={PW}")

    def check_fn(args, data, before):
        raise RuntimeError(f"read-back failed token={TOK}")

    verify.register("test.verified98", verify.Verifier(check_fn, prepare=prepare))
    before = await verify.capture_before("test.verified98", {}, timeout_s=2)
    vres = await verify.verify_call("test.verified98", {}, {}, before, timeout_s=2)
    check("C13 verification: a baseline that raised is logged (debug) without the canary", "baseline for test.verified98 unavailable" in cap.text() and not leaked(cap.text()), cap.text())
    check("C14 verification: a check that raised is logged (debug) without the canary", "check for test.verified98 raised" in cap.text() and "RuntimeError" in cap.text(), cap.text())
    dbg = [r for r in cap.records if r.name == "friday.verify"]
    check("C14a both verification lines are still DEBUG records, with no exception attached", len(dbg) == 2 and all(r.levelno == logging.DEBUG and not r.has_exc for r in dbg), str([(r.levelno, r.has_exc) for r in dbg]))
    check("C14b ...and the verification result is what it was: unverified, never an exception", before == {} and vres is not None and vres.status is verify.VerifyStatus.UNVERIFIED)

    # a scheduled job's action
    cap.clear()
    fake_job = SimpleNamespace(id=1, name="nightly98", enabled=True, actions=[{"skill": "test.boom98", "args": {}}], trigger_spec={"notify": False})
    original = (jobs.get_job, jobs._record_run, jobs.notify.send)
    jobs.get_job, jobs._record_run, jobs.notify.send = (lambda i: fake_job), (lambda *a, **k: None), (lambda *a, **k: None)
    try:
        ok = await jobs.run_job(1)
    finally:
        jobs.get_job, jobs._record_run, jobs.notify.send = original
    # the skill raised inside Executor.run, so that failure is logged by BOTH Executor.run and the job
    check("C15 a scheduled job's failing action: logged without the canary (Executor.run's line and the job's own)",
          "job 'nightly98' action test.boom98 failed" in cap.text() and not leaked(cap.text()), cap.text())
    check("C15b ...and the job still reports failure", ok is False)

    # undo
    cap.clear()
    aid = audit.record(actor="text", skill="test.boom98", tier="L0", args={}, decision="auto")
    audit.register_undo(aid, "test.boom98", {"skill": "test.boom98", "args": {}})
    lines = await undo.undo_last()
    check("C16 undo: a reversal that raised is logged without the canary", "failed to undo test.boom98" in cap.text() and not leaked(cap.text()), cap.text())
    check("C16b ...and undo still says it could not reverse it", any("reverse" in line_ and "Reversed" not in line_ for line_ in lines), str(lines))
    cap.clear()
    c = store.conn()
    cur = c.execute("INSERT INTO undo (at, audit_id, skill, inverse) VALUES (?,?,?,?)", (store.now(), aid, "test.boom98", '{"password": "' + PW))
    c.commit()
    lines = await undo.undo_last()
    mal = [r for r in cap.records if "malformed inverse" in r.message]
    check("C17 undo: an unreadable journal entry is logged with its type, no exception attached, without the canary",
          len(mal) == 1 and "JSONDecodeError" in mal[0].message and not mal[0].has_exc and not mal[0].trace and not leaked(cap.text()), cap.text())
    check("C18 across every real path above, no unfiltered handler ever saw a canary or a traceback",
          not leaked("\n".join(r.message + r.trace + r.raw for r in cap.all)) and all(not r.has_exc and not r.trace for r in cap.all if r.name.startswith("friday.")))

    # ------------------------------------------------------------------------------------------------ D
    scenario("D. failures fail safe")
    cap.clear()
    exc = raised(deep, 0, PW)

    def broken(text):
        raise RuntimeError("checker broke")

    friday_log._redact_log_text = broken
    try:
        text = friday_log.safe_exc_text(exc)
    finally:
        friday_log._redact_log_text = original_redact
    check("D1 the credential check broken: the message becomes a fixed notice, type and place stay, the raw text never appears",
          PW not in text and friday_log.EXC_UNREADABLE in text and text.startswith("RuntimeError") and " in deep" in text, text)
    friday_log._redact_log_text = lambda t: friday_log.WITHHELD_LOG
    try:
        text = friday_log.safe_exc_text(exc)
    finally:
        friday_log._redact_log_text = original_redact
    check("D2 the check reporting it could not decide (its own fixed notice): that notice is what is logged", friday_log.WITHHELD_LOG in text and PW not in text, text)
    text = friday_log.safe_exc_text(Unreadable())
    check("D3 an exception whose __str__ raises: type kept, message replaced, no raise, the text it saw never logged", text.startswith("Unreadable") and friday_log.EXC_UNREADABLE in text and PW not in text, text)
    original_one = friday_log._exc_one
    friday_log._exc_one = broken
    try:
        text = friday_log.safe_exc_text(exc)
    finally:
        friday_log._exc_one = original_one
    check("D4 the formatter itself broken: a fixed diagnostic that still names the type", text == f"RuntimeError: {friday_log.EXC_WITHHELD}", text)
    original_where = friday_log._exc_where
    friday_log._exc_where = broken
    try:
        text = friday_log.safe_exc_text(exc)
    finally:
        friday_log._exc_where = original_where
    check("D5 the location unavailable: the checked message is still logged", "location unavailable" in text and PW not in text and "RuntimeError" in text, text)
    cap.clear()
    friday_log._redact_log_text = broken
    try:
        with contextlib.suppress(RuntimeError):
            await EXECUTOR.run("test.boom98", {})
    finally:
        friday_log._redact_log_text = original_redact
    check("D6 through the real path, with the check broken: a fixed log entry, no canary, the failure still raised and audited",
          "skill test.boom98 failed" in cap.text() and not leaked(cap.text()) and friday_log.EXC_UNREADABLE in cap.text(), cap.text())
    check("D7 the helpers were restored", friday_log._redact_log_text is original_redact and friday_log._exc_one is original_one and friday_log._exc_where is original_where)

    # ------------------------------------------------------------------------------------------------ G
    scenario("G. the execution-path modules log no raw exception any more")
    import ast
    modules = ["permissions", "registry", "orchestrator", "session", "verify", "jobs", "undo", "shellproc", "skills/plan", "skills/shell"]
    offenders: list[str] = []
    stray: list[str] = []
    uses: dict[str, int] = {}
    for mod in modules:
        tree = ast.parse((ROOT / "friday" / f"{mod}.py").read_text(encoding="utf-8"))
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._parent = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            on_logger = isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in ("log", "logger", "logging")
            if on_logger and (fn.attr == "exception" or any(k.arg == "exc_info" for k in node.keywords)):
                offenders.append(f"{mod}:{node.lineno}")
            if isinstance(fn, ast.Name) and fn.id == "log_exception":
                uses[mod] = uses.get(mod, 0) + 1
                up, in_except = node, False
                while getattr(up, "_parent", None) is not None:
                    up = up._parent
                    if isinstance(up, ast.ExceptHandler):
                        in_except = True
                        break
                if not in_except and not any(k.arg == "exc" for k in node.keywords):
                    stray.append(f"{mod}:{node.lineno}")
    check("G1 no `log.exception(...)` and no `exc_info=` on a logger in any of the execution-path modules", not offenders, ", ".join(offenders))
    check("G2 the sites converted are really there (52 calls across the modules)", sum(uses.values()) == 52, str(uses))
    check("G3 every converted call is inside an `except` block or names its exception (so none logs 'no exception was active')", not stray, ", ".join(stray))

    # ------------------------------------------------------------------------------------------------ E
    scenario("E. what did not change")
    boom_rows = [r for r in audit.recent(500) if r["skill"] == "test.boom98" and r["outcome"] == "failure"]
    check("E1 the audit row of a failing skill: failure, ok 0, the error redacted -- as before", boom_rows and all(r["ok"] == 0 and PW not in str(r) and "[redacted]" in (r["error"] or "") for r in boom_rows), str(boom_rows[:1]))
    chain_rows = [r for r in audit.recent(500) if r["skill"] == "test.chain98"]
    check("E2 the audit row of a chained failure: failure, nothing leaked", chain_rows and chain_rows[0]["outcome"] == "failure" and not leaked(str(chain_rows)))
    ok_rows = [r for r in audit.recent(500) if r["skill"] == "test.undoable98"]
    check("E3 a success is still a success", ok_rows and ok_rows[0]["outcome"] == "success")
    from friday import toolview
    check("E4 the speech checks are untouched (safe_speech / scrub_tree / audit scrub)", PW not in auditview.scrub_free_text(f"my password is {PW}")
          and PW not in toolview.safe_speech(f"my password is {PW}") and PW not in str(auditview.scrub_tree({"a": f"password={PW}"})))
    handlers = [h for h in logging.getLogger().handlers if h is not cap]
    check("E5 the project's own handlers still carry the Phase 64 filter (the second layer)",
          len(handlers) >= 2 and all(any(isinstance(f, friday_log.RedactingFilter) for f in h.filters) for h in handlers), str(handlers))
    rec = logging.LogRecord("friday.x", logging.ERROR, __file__, 1, "plain log.exception still works: %s", ("x",), None)
    try:
        raise RuntimeError(f"unconverted call site password={PW}")
    except RuntimeError:
        rec.exc_info = sys.exc_info()
    friday_log.RedactingFilter().filter(rec)
    check("E6 a call site that still uses plain log.exception is still redacted by the filter", PW not in rec.getMessage() and "RuntimeError" in rec.getMessage())

    root.removeHandler(cap)
    for n, level in previous.items():
        logging.getLogger(n).setLevel(level)


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        asyncio.run(main_async())
    return finish("Phase 98 -- exceptions are logged checked, with their type and place", time.perf_counter() - t0, min_assertions=70, min_scenarios=6)


if __name__ == "__main__":
    raise SystemExit(main())
