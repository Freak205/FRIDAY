"""Phase 50 -- machine-readable audit rows carry an explicit `status`, so a client never has to parse text.

Before this, an outcome-unknown row (Phase 48) and an ordinary unfinished row were both `ok: null` with a `decision` of
auto/confirmed; the only difference was the wording of `error`. `audit.recent` (and so the daemon's `GET /audit` and the
`data.actions` of `meta.recent`) now adds a derived `status` to each row and changes nothing else.

Synthetic rows on a throwaway database; the daemon handler is called directly (no server, no model, no real database).
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit  # noqa: E402
from friday.skills import meta  # noqa: E402

SECRET = "hunter2-SECRET-body"
COLUMNS = {"id", "at", "actor", "skill", "tier", "args", "decision", "ok", "result", "error", "duration_ms"}
STATUSES = {"succeeded", "failed", "denied", "blocked", "unfinished", "outcome_unknown", "interrupted"}


def add(skill: str, *, decision: str = "auto", ok: bool | None = None, result: str | None = None,
        error: str | None = None, unknown: bool = False, args: dict | None = None) -> int:
    rid = audit.record(actor="text", skill=skill, tier="L1", args=args or {}, decision=decision)
    if ok is not None:
        audit.complete(rid, ok=ok, result=result, error=error, duration_ms=3)
    elif unknown:
        assert audit.mark_outcome_unknown(rid)
    return rid


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        add("t.success", ok=True, result="All done.")
        add("t.confirmed_ok", decision="confirmed", ok=True, result="Done.")
        add("t.failed", ok=False, error="it failed")
        add("t.cancelled", ok=False, error="interrupted before it finished (cancelled)")
        add("t.denied", decision="denied")
        add("t.blocked", decision="blocked")
        add("t.unfinished")
        add("t.unfinished_confirmed", decision="confirmed")
        add("t.unknown", unknown=True)
        add("t.unknown_confirmed", decision="confirmed", unknown=True)
        add("t.lookalike", ok=False, error="outcome unknown: the server said so")
        add("t.lookalike_open", ok=None, error=None)
        add("input.type", unknown=True, args={"text": SECRET})
        audit_open = audit.recent(1)[0]
        rows = {r["skill"]: r for r in audit.recent(50)}

        scenario("A. every category has its own stable status")
        want = {
            "t.success": "succeeded", "t.confirmed_ok": "succeeded", "t.failed": "failed", "t.cancelled": "failed",
            "t.denied": "denied", "t.blocked": "blocked", "t.unfinished": "unfinished",
            "t.unfinished_confirmed": "unfinished", "t.unknown": "outcome_unknown", "t.unknown_confirmed": "outcome_unknown",
            "t.lookalike": "failed", "t.lookalike_open": "unfinished",
        }
        for skill_name, status in want.items():
            check(f"A-{skill_name} -> {status}", rows[skill_name].get("status") == status, str(rows[skill_name].get("status")))
        check("A1 only the documented values ever appear", {r.get("status") for r in rows.values()} <= STATUSES)

        scenario("B. outcome unknown versus ordinary unfinished, denied and blocked, without reading any text")
        check("B1 unknown and unfinished are told apart by `status` alone",
              rows["t.unknown"]["status"] != rows["t.unfinished"]["status"])
        check("B2 denied and blocked rows are never reported as unfinished or unknown",
              {rows["t.denied"]["status"], rows["t.blocked"]["status"]} == {"denied", "blocked"})
        check("B3 a real failure whose text only resembles the marker is a failure, not unknown",
              rows["t.lookalike"]["status"] == "failed")
        check("B4 `ok` is never inferred: still NULL for unknown, unfinished, denied and blocked",
              all(rows[n]["ok"] is None for n in ("t.unknown", "t.unfinished", "t.denied", "t.blocked")))

        scenario("C. nothing existing changed")
        check("C1 every original column is present with its original value (status, and from Phase 52 the owning session_id, are the only additions)",
              all(COLUMNS <= set(r) and set(r) - COLUMNS == {"status", "session_id"} for r in rows.values()))
        check("C2 the unknown row still carries the Phase 48 marker in `error`, unchanged",
              rows["t.unknown"]["error"] == audit.OUTCOME_UNKNOWN and rows["t.unknown"]["ok"] is None)
        check("C3 the status was not written to the database (derived on read)",
              "status" not in {r[1] for r in audit.store.conn().execute("PRAGMA table_info(audit)").fetchall()})
        check("C4 repeated reads give the same statuses",
              {r["skill"]: r["status"] for r in audit.recent(50)} == {k: v["status"] for k, v in rows.items()})

        scenario("D. the daemon's GET /audit and meta.recent expose it, with redaction intact")
        from friday import daemon

        served = asyncio.run(daemon.recent_audit(limit=50))
        by = {r["skill"]: r for r in served}
        check("D1 the daemon handler returns the statuses", by["t.unknown"]["status"] == "outcome_unknown"
              and by["t.unfinished"]["status"] == "unfinished" and by["t.success"]["status"] == "succeeded")
        blob = json.dumps(served)
        check("D2 the response is JSON-serialisable and holds none of the redacted content", SECRET not in blob)
        data_rows = meta.recent(count=20).data["actions"]
        check("D3 meta.recent's structured rows carry the same status", all("status" in r for r in data_rows)
              and next(r for r in data_rows if r["skill"] == "t.unknown")["status"] == "outcome_unknown")
        check("D4 the stored `args` of the sensitive row are still the redacted form",
              SECRET not in by["input.type"]["args"])

        scenario("E. a later real outcome replaces the unknown status")
        audit.complete(rows["t.unknown"]["id"], ok=True, result="Written.", duration_ms=4)
        after = next(r for r in audit.recent(50) if r["id"] == rows["t.unknown"]["id"])
        check("E1 once the real result is recorded the row is simply succeeded", after["status"] == "succeeded")
        del audit_open

    return finish("Phase 50 -- structured audit status", time.perf_counter() - t0, min_assertions=24, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
