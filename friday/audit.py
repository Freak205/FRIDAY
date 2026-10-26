"""Append-only audit log.

Every skill invocation is recorded before it runs and updated after. If FRIDAY
does something surprising, this is the record that says what, when, with which
arguments, on whose instruction, and whether you approved it.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Iterator
from typing import Any

from friday import auditview, owner, store
from friday.log import get

log = get(__name__)

# audit id -> the raw content values the policy refuses to store, kept in MEMORY only so that `complete` can
# strip an echo of them ("Composed 'hello ...' to Sam") from the stored result. Bounded; never persisted.
_echo: dict[int, list[str]] = {}
_echo_lock = threading.Lock()
_ECHO_CAP = 256


# Phase 53: the multi-step goal (goals.id) the calls made inside `goal_scope` belong to. Read by `record`, so every audited
# step of a goal names it without any skill or the Executor having to pass it around.
_CURRENT_GOAL: contextvars.ContextVar[str | None] = contextvars.ContextVar("_CURRENT_GOAL", default=None)
# Phase 54: the audit ids written inside the current `goal_scope`, in order (a step reads the ones its own call created).
_SCOPE_IDS: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("_SCOPE_IDS", default=None)


def reset_context(var: contextvars.ContextVar, token: contextvars.Token) -> None:
    """`var.reset(token)`, except that a reset from a different Context is not an error.

    Phase 72: a run abandoned at shutdown (or at a step timeout) is finalised -- by the garbage collector, or at interpreter exit -- from
    another Context than the one that set the variable. The `ValueError` this raised in a `finally` replaced the `GeneratorExit` being
    handled, reached `Executor.run`'s `except Exception`, and wrote `ok=False` over the "outcome unknown" marker of a goal that had not
    failed. There is nothing to restore in a Context nobody will read again."""
    try:
        var.reset(token)
    except ValueError:
        pass


@contextlib.contextmanager
def goal_scope(goal_id: str | None) -> Iterator[list[int]]:
    ids: list[int] = []
    token = _CURRENT_GOAL.set(goal_id or None)
    ids_token = _SCOPE_IDS.set(ids)
    try:
        yield ids
    finally:
        reset_context(_SCOPE_IDS, ids_token)
        reset_context(_CURRENT_GOAL, token)


def scope_ids() -> list[int] | None:
    """The live list of audit ids recorded in the current goal scope, or None outside one."""
    return _SCOPE_IDS.get()


# The decisions that mean the skill was (or is about to be) started. Only these rows can be left open; a denied/blocked row
# never ran, so it has no outcome to be missing.
STARTED_DECISIONS = ("auto", "confirmed")


def record(
    *,
    actor: str,
    skill: str,
    tier: str,
    args: dict[str, Any],
    decision: str,
) -> int:
    """Write the pre-execution row. Returns the audit id.

    Phase 35: `args` is stored in the field-aware form of `friday.auditview` -- what was touched and with which
    non-content options, never the body of a file, a typed text, a message or code.

    Phase 52: the row carries the id of the runtime session (`friday.owner`) that wrote it, and that session's own row is
    written in the SAME transaction, so a row never exists without a recorded owner. For a started action (auto/confirmed)
    a session that cannot be recorded raises `owner.OwnerError` here -- before the skill is called, so nothing has
    happened -- rather than running work nobody could later account for. A denied/blocked row never ran; failing to
    record its owner is logged and the row is still written, without one."""
    c = store.conn()
    started = decision in STARTED_DECISIONS
    session_id: str | None = None
    try:
        session_id = owner.ensure_session(c)
    except Exception as exc:  # noqa: BLE001
        c.rollback()
        if started:
            raise owner.OwnerError(f"could not record which FRIDAY session starts {skill}: {exc}") from exc
        log.warning("owner of a %s row could not be recorded; writing it without one", decision, exc_info=True)
    try:
        cur = c.execute(
            "INSERT INTO audit (at, actor, skill, tier, args, decision, session_id, goal_id) VALUES (?,?,?,?,?,?,?,?)",
            (store.now(), actor, skill, tier, store.dumps(auditview.sanitize_args(skill, args)), decision, session_id,
             _CURRENT_GOAL.get()),
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    audit_id = int(cur.lastrowid)
    scope = _SCOPE_IDS.get()
    if scope is not None:
        scope.append(audit_id)
    values = auditview.content_values(skill, args)
    if values:
        with _echo_lock:
            _echo[audit_id] = values
            while len(_echo) > _ECHO_CAP:
                _echo.pop(next(iter(_echo)))
    return audit_id


def complete(
    audit_id: int,
    *,
    ok: bool,
    result: str | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Fill in the outcome once the skill has run. `ok` is the skill's own verdict, passed through untouched;
    only the free text (what it said, what went wrong) is minimised before it is stored."""
    c = store.conn()
    row = c.execute("SELECT skill FROM audit WHERE id=?", (audit_id,)).fetchone()
    skill = row["skill"] if row is not None else ""
    with _echo_lock:
        echoes = _echo.pop(audit_id, [])
    c.execute(
        "UPDATE audit SET ok=?, result=?, error=?, duration_ms=? WHERE id=?",
        (1 if ok else 0, auditview.sanitize_result(skill, result, echoes), auditview.sanitize_error(error, echoes),
         duration_ms, audit_id),
    )
    c.commit()


# Phase 48: the text of a row whose skill was still running when FRIDAY closed. `ok` stays NULL on purpose -- it did not
# fail (it may well have succeeded) and nothing says it succeeded -- and no undo entry exists for it, because its result
# (and so its inverse) was never seen. The words are fixed; they carry nothing from the request.
OUTCOME_UNKNOWN = (
    "outcome unknown: FRIDAY was closed while this was still running, so whether it finished was not recorded "
    "(check the result yourself; nothing was registered to undo it)"
)


# Phase 53: the text of a row whose skill runs in a worker thread and was abandoned (step timeout, cancelled run) while the
# thread was still going. Python cannot stop that thread, so the side effect may well happen -- recording the row as a
# failure (what Phase 28 did) was a claim nobody could back. Same status (`outcome_unknown`) and the same rule: `ok` stays NULL,
# nothing is retried or registered for undo; unlike OUTCOME_UNKNOWN the process is still here, so when the thread ends its real
# outcome is written over this marker (`Executor.run`).
OUTCOME_UNKNOWN_DETACHED = (
    "outcome unknown: this step was abandoned while it was still running (it ran out of time or the run was cancelled) "
    "and cannot be interrupted, so whether it took effect is not known yet; it was not retried"
)


# Phase 51/52: the text of a row that a PREVIOUS process left open and whose owning process (`friday.owner`) is provably gone.
# Distinct from OUTCOME_UNKNOWN (an orderly quit that saw the work still running): here nobody saw anything. `ok` stays NULL
# -- it may have finished, failed or never run -- and nothing is retried, undone or registered for undo.
INTERRUPTED = (
    "interrupted: the process that started this ended before recording an outcome (crash, kill or restart), so whether it "
    "finished is unknown; nothing was retried or undone"
)


def is_interrupted(row: dict[str, Any]) -> bool:
    return row.get("ok") is None and row.get("error") == INTERRUPTED


def reconcile_orphans(probe: owner.Probe | None = None) -> int:
    """Phase 52: mark started-but-never-recorded rows whose owning process is PROVABLY gone as `interrupted`; returns how many.

    Only a row that names an owner (`session_id`) is considered, and only when `owner.probe` says that owner is DEAD (no such
    process, or the pid now belongs to a process created at a different time). ALIVE -- another FRIDAY process doing real
    work, however long it takes -- and UNKNOWN (another host, no recorded creation time, a process that cannot be inspected,
    any error) leave the row exactly as it is, and so does a row with no owner at all (everything written before this
    phase): neither age nor the boot time is evidence, so nothing is inferred. This process's own session is never touched.

    A dead owner proves the process ended, not what became of the work: ``ok`` stays NULL, no result is invented, and
    nothing is retried, undone or registered for undo. The update names the row's unresolved state in its WHERE clause, so it is
    one atomic decision against whatever another connection has written: a real outcome that landed first is never
    replaced, a denied/blocked row and the Phase 48 shutdown marker never match, repeating it changes nothing, and several
    processes reconciling at once mark each row once. (A later real `complete` still replaces the marker.)"""
    probe = probe or owner.probe
    c = store.conn()
    unresolved = "ok IS NULL AND result IS NULL AND error IS NULL AND decision IN ('auto','confirmed')"
    candidates = c.execute(
        "SELECT id, pid, proc_start, host FROM runtime_sessions WHERE id != ? AND id IN "
        f"(SELECT session_id FROM audit WHERE session_id IS NOT NULL AND {unresolved})",
        (owner.identity().id,),
    ).fetchall()
    c.rollback()  # end the read transaction before probing; the update below is its own short one
    marked = 0
    for s in candidates:
        try:
            verdict = probe(s["pid"], s["proc_start"], s["host"])
        except Exception:  # noqa: BLE001
            log.warning("liveness probe failed for session %s; leaving its rows", s["id"], exc_info=True)
            continue
        if verdict != owner.DEAD:
            continue
        cur = c.execute(f"UPDATE audit SET error=? WHERE session_id=? AND {unresolved}", (INTERRUPTED, s["id"]))
        c.commit()
        marked += int(cur.rowcount or 0)
    if marked:
        log.warning("audit: %d action(s) started by a FRIDAY process that no longer exists are marked interrupted", marked)
    return marked


def start_session(kind: str) -> None:
    """Startup hook shared by the GUI backend, the daemon and the CLI: record this process as a FRIDAY session, then
    reconcile what dead processes left open. Neither step may stop a start -- but if the session cannot be recorded, every
    audited action then refuses to start (see `record`), so the failure is loud where it matters, not silent here."""
    try:
        owner.register(kind)
    except Exception:
        log.exception("this process could not be recorded as a FRIDAY session; audited actions will be refused until it can")
    try:
        reconcile_orphans()
    except Exception:
        log.exception("audit reconciliation failed; continuing")
    try:  # Phase 53: a goal no process is running any more must not stay "running"; nothing is resumed or retried
        from friday import steps

        steps.settle_interrupted_goals()
        steps.refresh_goals()
    except Exception:
        log.exception("goal settlement failed; continuing")


def is_outcome_unknown(row: dict[str, Any]) -> bool:
    """Phase 49: is this audit row (as `recent` returns it) one that `mark_outcome_unknown` marked, and that nothing has
    since given a real outcome? The marker must match exactly, so a genuine error that merely mentions the words is not it."""
    return row.get("ok") is None and row.get("error") in (OUTCOME_UNKNOWN, OUTCOME_UNKNOWN_DETACHED)


def mark_outcome_unknown(audit_id: int, *, detached: bool = False) -> bool:
    """Mark a row that started (an `auto`/`confirmed` decision) and has no outcome at all as "outcome unknown".
    True only if it changed something. It never touches a row that already has an outcome or any text, nor one that never
    ran (denied / blocked), so it is safe to repeat; a real `complete` that arrives later simply replaces the marker."""
    c = store.conn()
    cur = c.execute(
        "UPDATE audit SET error=? WHERE id=? AND ok IS NULL AND result IS NULL AND error IS NULL "
        "AND decision IN ('auto','confirmed')",
        (OUTCOME_UNKNOWN_DETACHED if detached else OUTCOME_UNKNOWN, audit_id),
    )
    c.commit()
    return cur.rowcount > 0


def register_undo(audit_id: int, skill: str, inverse: dict[str, Any]) -> None:
    """Record how to reverse an action. `inverse` is {"skill": ..., "args": {...}}."""
    c = store.conn()
    c.execute(
        "INSERT INTO undo (at, audit_id, skill, inverse) VALUES (?,?,?,?)",
        (store.now(), audit_id, skill, store.dumps(inverse)),
    )
    c.commit()


def status_of(row: dict[str, Any]) -> str:
    """Phase 50: where this row stands, as one stable word, derived from its own fields (never stored, never guessed):

    - ``denied`` / ``blocked`` -- the decision; the skill never ran (``ok`` stays NULL);
    - ``succeeded`` / ``failed`` -- the skill's own verdict (``ok`` 1 / 0), exactly as recorded;
    - ``outcome_unknown`` -- it started and was still running when FRIDAY closed (the Phase 48 marker, matched exactly);
    - ``interrupted`` -- it started in an earlier process that ended without recording it (the Phase 51/52 marker);
    - ``unfinished`` -- it started and nothing has been recorded for it (yet).
    A missing result is never read as success or failure."""
    decision = row.get("decision")
    if decision in ("denied", "blocked"):
        return decision
    ok = row.get("ok")
    if ok is not None:
        return "succeeded" if ok else "failed"
    if is_interrupted(row):
        return "interrupted"
    return "outcome_unknown" if is_outcome_unknown(row) else "unfinished"


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Newest first. Every column as stored, plus a derived ``status`` (see `status_of`) so a machine reader does not have
    to parse ``error``; the daemon's ``GET /audit`` and ``meta.recent``'s ``data.actions`` return these rows."""
    c = store.conn()
    rows = c.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    out = [dict(r) for r in rows]
    for row in out:
        row["status"] = status_of(row)
    return out
