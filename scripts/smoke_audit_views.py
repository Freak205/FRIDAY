"""Phase 49 -- an "outcome unknown" audit row (Phase 48) is visible and distinguishable in the views that read the audit log.

Readers checked: the CLI audit table (`friday.cli._audit_table`) and the conversational history skill (`meta.recent`, what
"what did you just do" speaks). Both are driven with synthetic rows on a throwaway database; nothing real is read.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import iso_env  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import audit, cli  # noqa: E402
from friday.skills import meta  # noqa: E402

SECRET = "hunter2-SECRET-body"


def add(skill: str, *, decision: str = "auto", ok: bool | None = None, result: str | None = None,
        error: str | None = None, unknown: bool = False, args: dict | None = None) -> int:
    rid = audit.record(actor="text", skill=skill, tier="L1", args=args or {}, decision=decision)
    if ok is not None:
        audit.complete(rid, ok=ok, result=result, error=error, duration_ms=3)
    elif unknown:
        assert audit.mark_outcome_unknown(rid)
    return rid


def table_lines() -> dict[str, str]:
    buf = io.StringIO()
    saved = cli.con
    cli.con = Console(file=buf, width=220, force_terminal=False, color_system=None)
    try:
        cli._audit_table(50)
    finally:
        cli.con = saved
    out: dict[str, str] = {}
    for line in buf.getvalue().splitlines():
        for name in ("t.success", "t.failed", "t.denied", "t.blocked", "t.unfinished", "t.unknown", "t.secret"):
            if name in line:
                out[name] = line
    return out


def main() -> int:
    t0 = time.perf_counter()
    with iso_env.isolated_runtime():
        add("t.success", ok=True, result="All done.")
        add("t.failed", ok=False, error="it failed")
        add("input.type", unknown=True, args={"text": SECRET})
        add("t.denied", decision="denied")
        add("t.blocked", decision="blocked")
        add("t.unfinished")
        add("t.unknown", unknown=True)
        rows = {r["skill"]: r for r in audit.recent(50)}

        scenario("A. the CLI audit table")
        lines = table_lines()
        check("A0 every synthetic row is shown", set(lines) >= {"t.success", "t.failed", "t.denied", "t.blocked", "t.unfinished", "t.unknown"},
              str(sorted(lines)))
        unk = lines.get("t.unknown", "")
        check("A1 the unknown row says 'outcome unknown' in words", "outcome unknown" in unk, unk)
        check("A2 and has its own marker in the ok column, which is neither the success nor the failure mark",
              "?" in unk and "✓" not in unk and "✗" not in unk, unk)
        check("A3 success keeps its check mark and failure its cross, neither says unknown",
              "✓" in lines.get("t.success", "") and "✗" in lines.get("t.failed", "")
              and "unknown" not in lines.get("t.success", "") + lines.get("t.failed", ""))
        unf = lines.get("t.unfinished", "")
        check("A4 an ordinary unfinished row is still blank: no mark, no 'unknown'",
              "✓" not in unf and "✗" not in unf and "?" not in unf and "unknown" not in unf, unf)
        den, blk = lines.get("t.denied", ""), lines.get("t.blocked", "")
        check("A5 denied and blocked rows keep their policy word and are not labelled unknown",
              "denied" in den and "blocked" in blk and "unknown" not in den + blk and "?" not in den + blk, f"{den!r} {blk!r}")
        check("A6 the failed row's own error text is unchanged", "it failed" in lines.get("t.failed", ""))
        check("A7 the redacted content never reaches the table", SECRET not in "".join(lines.values()))

        scenario("B. the spoken history (meta.recent)")
        res = meta.recent(count=20)
        speech = res.speech
        check("B1 the unknown action is spoken as having an unknown outcome",
              "t.unknown (outcome unknown)" in speech, speech)
        check("B2 no other action is described that way",
              speech.count("outcome unknown") == 2, speech)  # t.unknown and input.type
        # (the skill has always named only the five newest actions; success and failure are covered by the table, A3)
        check("B3 denied, blocked and unfinished rows are listed as before (name only)",
              all(n in speech for n in ("t.denied", "t.blocked", "t.unfinished"))
              and not any(f"{n} (" in speech for n in ("t.denied", "t.blocked", "t.unfinished")), speech)
        check("B4 the spoken text contains none of the redacted content", SECRET not in speech)
        check("B5 the raw rows in `data` are unchanged (same fields, marker text intact)",
              all("outcome unknown" in (a.get("error") or "") for a in res.data["actions"] if a["skill"] == "t.unknown")
              and set(res.data["actions"][0]) == set(rows["t.unknown"]))

        scenario("C. the shared test for 'unknown'")
        check("C1 only a started row with the exact marker counts",
              audit.is_outcome_unknown(rows["t.unknown"]) is True
              and not any(audit.is_outcome_unknown(rows[n]) for n in ("t.success", "t.failed", "t.denied", "t.blocked", "t.unfinished")))
        check("C2 a row whose marker was replaced by a real failure is not unknown",
              audit.is_outcome_unknown({**rows["t.unknown"], "ok": 0, "error": "it failed"}) is False)
        check("C3 nor is a row that merely mentions the words in a real error",
              audit.is_outcome_unknown({**rows["t.failed"], "ok": None, "error": "outcome unknown: something else"}) is False)

    return finish("Phase 49 -- audit views show unknown outcomes", time.perf_counter() - t0, min_assertions=16, min_scenarios=3)


if __name__ == "__main__":
    sys.exit(main())
