"""Phase 24.9 — per-object evidence grounding of completed-action claims.

Phases 24.1/24.2 ask "did SOME real result report this KIND of action?" (delete/remove for "Deleted") and
24.8 asks "is each named file stated SOMEWHERE in the evidence as a whole value?". Together they still let
"Deleted report.csv and notes.txt." through on the evidence "Deleted report.csv successfully." as soon as the
goal (or any other observation) merely mentioned notes.txt: the action was grounded for one object and the
other was only "present". 24.9 closes that at one place — `ground_answer`'s claim loop — with two small
helpers in `friday.intelligence.discovery`:

  `_claimed_objects`  the files ONE claim verb is about, from two deterministic shapes only (names right after
                      the verb, or right before a passive auxiliary); anything ambiguous yields NO object and
                      the claim is judged exactly as before
  `_grounds_object`   whether ONE real observation's own reported outcome states that action about that file
                      (never its tool name; a negated / hedged / skipped action does not count; the call's args
                      identify the object only for an outcome that states the action and names no file)

  A  a single named object (supported / alias wording / unnamed / different file / different action)
  B  lists and separate sentences: two, three, colon, passive, comma/and — supported, and one missing
  C  one supported + one unsupported: rejected, and the fallback restates only the real evidence
  D  one supported + one failed; a failed observation never becomes support for the object it mentions
  E  a tool ARGUMENT is not an outcome (the 24.2 rule, per object)
  F  conflicts: an answer that claims both is rejected, one that states both object states is accepted
  G  attempt / uncertain / overridden / negated evidence about one object
  H  conservative allowance: ambiguous object boundaries are never a reason to reject
  I  completeness stays a separate layer (24.3), object grounding does not answer for it
  J  numeric and filename grounding are still active around a multi-object claim
  O  what an adversarial review of the first implementation found (honest answers kept, look-alike claims
     rejected, evidence that is not an outcome)
  K  both final-answer routes, read verbs, normalization order
  L  the real Orchestrator.run_goal (composer route, planner `done` route, the kill switch)
  M  a seeded property corpus: an object claim is accepted iff EVERY claimed object has a real outcome
  N  in-suite mutation checks: the per-object check disabled, extraction made too aggressive, and each guard
     removed in turn — each must make this suite's own cases fail

Deterministic and offline: no Ollama, no real side effect (the 24.7 fake tool world for the run_goal cases).
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import sys
import time

import phase21_common as H
import smoke_answer_normalization as N  # the Phase 24.7 fixtures: R(), sg(), world(), drive(), ran(), reset()
from phase21_common import call, check, done, scenario

from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402

R_, N_, D_ = "report.csv", "notes.txt", "data.csv"
G1 = "Delete report.csv."
G_N = "Delete notes.txt."
GOAL2 = "Delete report.csv and notes.txt."
GOAL3 = "Delete report.csv, notes.txt and data.csv."


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None, data: dict | None = None) -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, data or {}, "")


def dele(name: str, speech: str | None = None, **kw) -> Observation:
    """A real 'Deleted <name>' result (each file has its own call args, so nothing is a stale duplicate)."""
    return obs("files.delete", speech or f"Deleted {name} successfully.", args={"path": name}, **kw)


def refuse(name: str) -> Observation:
    return obs("files.delete", f"Failed to delete {name}: permission denied.", ok=False, args={"path": name})


def mention(name: str, speech: str | None = None) -> Observation:
    """A result about the file that is NOT a deletion."""
    return obs("files.read", speech or f"{name} has 5 lines.", args={"path": name})


def still(name: str) -> Observation:
    return obs("files.check", f"{name} still exists.", args={"path": name})


def low(text: str) -> str:
    return (text or "").lower()


def verdict(goal: str, answer: str, evidence: list[Observation], *, retrieval_claims: bool = True) -> str:
    """accept = the answer came back untouched; appended = kept, plus the 24.3 / 24.4 'Also: ...' tail for a real result it left
    out; closed = the fail-closed 'insufficient' message; changed = anything else."""
    out = discovery.ground_answer(goal, answer, evidence, retrieval_claims=retrieval_claims)
    if out == answer:
        return "accept"
    if "insufficient" in low(out):
        return "closed"
    return "appended" if out.startswith(answer) else "changed"


def kept(got: str) -> bool:
    """Not rejected: the answer stands (untouched, or with completeness / conflict text appended, as before 24.9)."""
    return got in ("accept", "appended")


BOTH = (True, False)  # retrieval_claims: the Phase 23 composer route / the planner `done` route
COMPOSER = (True,)
CASES: dict[str, list] = {}


def case(group: str, label: str, goal: str, answer: str, evidence: list[Observation], expect: str, routes=BOTH) -> None:
    CASES.setdefault(group, []).append((label, goal, answer, evidence, expect, routes))


def failing(cases: list) -> list[str]:
    bad = []
    for label, goal, answer, evidence, expect, routes in cases:
        got = [verdict(goal, answer, evidence, retrieval_claims=r) for r in routes]
        if any(not (kept(g) if expect == "kept" else g == expect) for g in got):
            bad.append(f"{label} [{'/'.join(got)}, wanted {expect}]")
    return bad


def group_ok(*groups: str) -> tuple[bool, str]:
    bad = [b for g in groups for b in failing(CASES[g])]
    return not bad, "; ".join(bad[:3])


TWO = lambda: [dele(R_), dele(N_)]  # noqa: E731
THREE = lambda: [dele(R_), dele(N_), dele(D_)]  # noqa: E731

# ---- A: a single named object -------------------------------------------------------------------------------
case("A+", "one file, supported", G1, "Deleted report.csv successfully.", [dele(R_)], "accept")
case("A+", "first person", G1, "I deleted report.csv for you.", [dele(R_)], "accept")
case("A+", "evidence says 'removed', answer says 'deleted'", G1, "Deleted report.csv.",
     [obs("files.delete", "Successfully removed report.csv.", args={"path": R_})], "accept")
case("A+", "evidence says 'deleted', answer says 'removed'", G1, "Removed report.csv.", [dele(R_)], "accept")
case("A+", "no concrete object in the answer", G1, "I deleted the requested file.", [dele(R_)], "accept")
case("A+", "a path-qualified name in the evidence", G1, "Deleted report.csv.",
     [obs("files.delete", "Deleted C:/docs/report.csv.", args={"path": "C:/docs/report.csv"})], "accept")
case("A+", "the object is stated only in the returned data", G1, "Deleted report.csv.",
     [obs("files.delete", "Deleted 1 file.", data={"deleted": [R_]})], "accept")
case("A-", "a different file with a similar name (report-final.csv)", G1, "Deleted report-final.csv.", [dele(R_)], "closed")
case("A-", "the file was moved, not deleted", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.move", "Moved notes.txt to Archive.", args={"path": N_})], "closed")
case("A-", "the file was only read", G1, "Deleted report.csv and notes.txt.", [dele(R_), mention(N_)], "closed")


def section_a() -> None:
    scenario("A: a single named object — same object, any action wording; different file or different action is not support")
    ok, why = group_ok("A+")
    check("A1: 'Deleted report.csv.' is accepted whatever the wording (removed/deleted, first person, path-qualified, named "
          "in the returned data) — and an answer that names no concrete object is left exactly as before", ok, why)
    ok, why = group_ok("A-")
    check("A2: report-final.csv is not report.csv, a file that was MOVED (or only read) was not deleted — the claim fails closed",
          ok, why)


# ---- B: lists and separate sentences ------------------------------------------------------------------------
B_PHRASINGS = [
    ("and-list", GOAL2, "Deleted report.csv and notes.txt."),
    ("colon list", GOAL2, "Deleted 2 files: report.csv and notes.txt."),
    ("passive", GOAL2, "Both report.csv and notes.txt were deleted."),
    ("perfect passive", GOAL2, "The files report.csv and notes.txt have been deleted."),
    ("separate sentences", GOAL2, "Deleted report.csv. Deleted notes.txt."),
    ("parenthetical sizes", GOAL2, "Deleted report.csv (2 KB) and notes.txt (1 KB)."),
    ("markdown bold names", GOAL2, "Deleted **report.csv** and **notes.txt**."),
    ("backticked names", GOAL2, "Deleted `report.csv` and `notes.txt`."),
    ("an adjective inside the list", GOAL2, "Deleted report.csv and the old notes.txt."),
    ("'have both been' passive", GOAL2, "report.csv and notes.txt have both been deleted."),
    ("comma + Oxford and", GOAL3, "Deleted report.csv, notes.txt, and data.csv."),
    ("comma + and", GOAL3, "Deleted report.csv, notes.txt and data.csv."),
]
for _label, _goal, _answer in B_PHRASINGS:
    _have = THREE if _goal == GOAL3 else TWO
    case("B+", f"{_label}, every object evidenced", _goal, _answer, _have(), "accept")
    case("B-", f"{_label}, the LAST object has no result", _goal, _answer, _have()[:-1], "closed")
case("B+", "two clauses, two verbs, each grounded", "Delete report.csv and move notes.txt.",
     "Deleted report.csv and moved notes.txt to Archive.", [dele(R_), obs("files.move", "Moved notes.txt to Archive.", args={"path": N_})], "accept")
case("B+", "one tool result that names both files", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted report.csv and notes.txt.", args={"paths": [R_, N_]})], "accept")
case("B+", "both files stated in the returned data", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted 2 files.", data={"deleted": [R_, N_]})], "accept")
case("B+", "'Done.' with the deletions stated in the returned data", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Done.", args={"path": "all"}, data={"deleted": [R_, N_]})], "accept")
case("B+", "'X from A and Y from B', both deleted", GOAL2, "Deleted report.csv from Downloads and notes.txt from Documents.",
     [obs("files.delete", "Deleted report.csv from Downloads.", args={"path": R_}), obs("files.delete", "Deleted notes.txt from Documents.", args={"path": N_})], "accept")
case("B-", "'X from A and Y from B', only X deleted", GOAL2, "Deleted report.csv from Downloads and notes.txt from Documents.",
     [obs("files.delete", "Deleted report.csv from Downloads.", args={"path": R_}), mention(N_)], "closed")
case("B-", "the speech names one file, the data merely lists another as remaining", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted report.csv successfully.", args={"path": R_}, data={"remaining": [N_]})], "closed")
case("B-", "two clauses, the actions swapped between the files", "Delete report.csv and move notes.txt.",
     "Deleted report.csv and moved notes.txt.", [dele(N_), obs("files.move", "Moved report.csv to Archive.", args={"path": R_})], "closed")
case("B3", "three objects, notes.txt missing", GOAL3, "Deleted report.csv, notes.txt, and data.csv.", [dele(R_), dele(D_)], "closed")
case("B3", "three objects, data.csv missing", GOAL3, "Deleted report.csv, notes.txt and data.csv.", [dele(R_), dele(N_)], "closed")
case("B3", "three objects as passive, report.csv missing", GOAL3, "Report.csv, notes.txt and data.csv were deleted.", [dele(N_), dele(D_)], "closed")
case("B3", "three objects, only one evidenced", GOAL3, "Deleted report.csv, notes.txt, and data.csv.", [dele(N_)], "closed")


def section_b() -> None:
    scenario("B: two and three objects — lists, colon lists, passive, separate sentences, clauses with their own verb")
    ok, why = group_ok("B+")
    check("B1: every phrasing of a multi-object claim is accepted when EACH object has its own real result — and-list, colon "
          "list, passive, 'have been', separate sentences, sizes in parentheses, comma lists, two clauses with two verbs, one "
          "result naming both files, both files in the returned data", ok, why)
    ok, why = group_ok("B-")
    check("B2: the same phrasings fail closed when the LAST object has no result — and when the actions are swapped between "
          "the files (delete evidence for one, move evidence for the other)", ok, why)
    ok, why = group_ok("B3")
    check("B3: three objects, only two evidenced — rejected wherever the missing one sits (first, middle, last), in a "
          "comma list and as a passive", ok, why)


# ---- C: one supported + one unsupported ---------------------------------------------------------------------
case("C", "goal names both, only report.csv was deleted", GOAL2, "Deleted report.csv and notes.txt.", [dele(R_)], "closed")
case("C", "goal names one, another observation merely mentions the second", G1, "Deleted report.csv and notes.txt.",
     [dele(R_), mention(N_)], "closed")
case("C", "goal names one, the second only appears in a search result", G1, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.find", "Found notes.txt in Documents.", args={"name": N_})], "closed")


def section_c() -> None:
    scenario("C: one supported + one unsupported — the affirmative claim about the unsupported object is rejected")
    ok, why = group_ok("C")
    check("C1: 'Deleted report.csv and notes.txt.' over 'Deleted report.csv successfully.' fails closed on both routes — "
          "whether the goal or a different observation is what mentions notes.txt (a request, a read or a search hit is "
          "not a deletion)", ok, why)
    out = discovery.ground_answer(GOAL2, "Deleted report.csv and notes.txt.", [dele(R_)])
    check("C2: the fallback restates only what the evidence shows — the real deletion of report.csv — and never repeats "
          "the claim about notes.txt", "deleted report.csv successfully" in low(out) and "notes.txt" not in low(out), out)
    single = discovery.ground_answer(G1, "Deleted notes.txt.", [dele(R_), mention(N_)])
    check("C3: the same holds for a single named object — a file another observation merely mentions was not deleted "
          "(before 24.9 the mention alone let 'Deleted notes.txt.' through)", "insufficient" in low(single), single)


# ---- D: failed evidence -------------------------------------------------------------------------------------
case("D-", "one deleted, one refused (ok=False)", GOAL2, "Deleted report.csv and notes.txt.", [dele(R_), refuse(N_)], "closed")
case("D-", "only a failure about the file (ok=False)", G_N, "Deleted notes.txt.",
     [obs("files.delete", "Delete failed: notes.txt was not removed.", ok=False, args={"path": N_})], "closed")
case("D-", "a failure phrased as an ok result, next to a real deletion", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.delete", "Delete failed: notes.txt was not removed.", args={"path": N_})], "closed")
case("D-", "a failure about the file only in the goal-named evidence", GOAL2, "I deleted report.csv and notes.txt.",
     [dele(R_), obs("files.delete", "Permission denied deleting notes.txt.", ok=False, args={"path": N_})], "closed")
case("D+", "honest: one deleted, one refused, both stated", GOAL2, "Deleted report.csv. Failed to delete notes.txt: permission denied.",
     [dele(R_), refuse(N_)], "accept")
case("D+", "honest: contrast clause", GOAL2, "Deleted report.csv, but notes.txt could not be deleted: permission denied.",
     [dele(R_), refuse(N_)], "accept")
case("D+", "honest: 'couldn't delete'", GOAL2, "I deleted report.csv but couldn't delete notes.txt: permission denied.",
     [dele(R_), refuse(N_)], "accept")


def section_d() -> None:
    scenario("D: failed evidence — a failure about an object never turns into support for it")
    ok, why = group_ok("D-")
    check("D1: one deleted + one refused — 'Deleted report.csv and notes.txt.' fails closed (a failed or failure-worded "
          "observation is not in the grounding pool, so the object it mentions has no result)", ok, why)
    out = discovery.ground_answer(GOAL2, "Deleted report.csv and notes.txt.", [dele(R_), refuse(N_)])
    check("D2: the fallback preserves the REAL evidence — the deletion that happened and the failure that happened — and "
          "no 'deleted notes.txt' claim survives", "deleted report.csv successfully" in low(out) and "permission denied" in low(out)
          and "deleted notes.txt" not in low(out) and "insufficient" in low(out), out)
    ok, why = group_ok("D+")
    check("D3: the honest answers — deleted one, refused the other, said so in any of three wordings — are accepted "
          "untouched (an object named in a failure clause is not an object of the deletion claim)", ok, why)


# ---- E: tool arguments --------------------------------------------------------------------------------------
case("E", "the call was aimed at notes.txt, but its outcome says only 'Done.'", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.delete", "Done.", args={"path": N_})], "closed")
case("E", "the call was aimed at notes.txt, but its outcome names report.csv", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted report.csv successfully.", args={"path": N_})], "closed")
case("E", "single object: args name it, the outcome does not report a deletion", G_N, "Deleted notes.txt.",
     [obs("files.delete", "Done.", args={"path": N_})], "closed")
case("E", "the tool NAME contains the verb and the args name the file", G_N, "Deleted notes.txt.",
     [obs("files.delete_file", "Ok.", args={"path": N_})], "closed")
case("E", "the outcome states another action ('Moved the file.') and the args name the file", G_N, "Deleted notes.txt.",
     [obs("files.move", "Moved the file.", args={"path": N_})], "closed")
case("E", "the outcome says the action did NOT happen ('The file was not deleted.') and the args name the file", G_N, "Deleted notes.txt.",
     [obs("files.delete", "The file was not deleted.", args={"path": N_})], "closed")
case("E+", "'File deleted.' (no file named) + the call was aimed at notes.txt", G_N, "Deleted notes.txt.",
     [obs("files.delete", "File deleted.", args={"path": N_})], "accept")
case("E+", "two calls with differently worded file-less results, each aimed at one of the files", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted the file.", args={"path": R_}), obs("files.delete", "File deleted.", args={"path": N_})], "accept")
case("E+", "a batch call: 'Deleted 2 files.' aimed at both", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted 2 files.", args={"paths": [R_, N_]})], "accept")


def section_e() -> None:
    scenario("E: tool arguments alone do not establish an action on an object (the 24.2 rule, applied per object)")
    ok, why = group_ok("E")
    check("E1: a file named only in a call's ARGUMENTS is not deleted by the outcome of that call — 'Done.', an outcome "
          "about another file, or a tool whose own name says delete: only the reported outcome (speech + data) counts", ok, why)
    ev = [dele(R_), obs("files.delete", "Done.", args={"path": N_})]
    out = discovery.ground_answer(GOAL2, "Deleted report.csv.", ev)
    ok, why = group_ok("E+")
    check("E2: what the arguments DO still do — an outcome that states the action but names NO file at all ('File deleted.', "
          "'Deleted 2 files.') is about the call it answers, so the file(s) that call was aimed at are its objects (a terse "
          "tool is not punished); and an answer that claims only report.csv is not rejected (the 24.3 completeness tail it "
          "gets is the pre-24.9 behaviour)", ok and out.startswith("Deleted report.csv.") and "insufficient" not in low(out), why or out)


# ---- F: conflicts -------------------------------------------------------------------------------------------
case("F+", "accurate: contrast clause with the real state", GOAL2, "Deleted report.csv, but notes.txt still exists.",
     [dele(R_), still(N_)], "accept")
case("F+", "accurate: two sentences", GOAL2, "Deleted report.csv. notes.txt still exists.", [dele(R_), still(N_)], "accept")
case("F+", "accurate: passive + contrast", GOAL2, "report.csv was deleted, but notes.txt still exists.",
     [dele(R_), still(N_)], "accept")


def section_f() -> None:
    scenario("F: conflicting object state — never silently resolved")
    ev = [dele(R_), still(N_)]
    out = discovery.ground_answer(GOAL2, "Deleted report.csv and notes.txt.", ev)
    check("F1: 'Deleted report.csv and notes.txt.' next to 'notes.txt still exists.' is rejected, and the fallback shows the "
          "user BOTH real facts", "insufficient" in low(out) and "notes.txt still exists" in low(out) and "deleted report.csv" in low(out), out)
    ok, why = group_ok("F+")
    check("F2: 'Deleted report.csv, but notes.txt still exists.' — every object state accurately represented — is accepted "
          "unchanged in all three shapes", ok, why)
    ev = [obs("files.delete", "Deleted notes.txt.", args={"path": N_}), obs("files.verify", "Delete failed: notes.txt is locked.", ok=False, args={"path": N_})]
    out = discovery.ground_answer(G_N, "Deleted notes.txt.", ev)
    check("F3: two DIFFERENT tools disagree about the very object the answer claims — the claim itself is grounded (one "
          "real deletion result exists), so the 24.4 conflict layer, not this check, appends the failure: never closed, "
          "never silently resolved", out.startswith("Deleted notes.txt.") and "locked" in low(out) and "insufficient" not in low(out), out)


# ---- G: unusable evidence about one object ------------------------------------------------------------------
case("G-", "notes.txt: only an attempt line", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.delete", "Attempting to delete notes.txt.", args={"path": N_})], "closed")
case("G-", "notes.txt: the tool flagged its result uncertain", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), dele(N_, data={"uncertain": True})], "closed")
case("G-", "notes.txt: a later failure of the same tool overrode the success", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), dele(N_), obs("files.delete", "Delete failed: notes.txt is locked.", ok=False, args={"path": N_, "again": True})], "closed")
case("G-", "notes.txt: 'was not removed' (an ok result that says it did NOT happen)", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.check", "notes.txt was not removed.", args={"path": N_})], "closed")
case("G-", "notes.txt: \"wasn't deleted\"", GOAL2, "Deleted report.csv and notes.txt.",
     [dele(R_), obs("files.check", "notes.txt wasn't deleted.", args={"path": N_})], "closed")
case("G+", "notes.txt: refused first, deleted on a retry (same tool, later result wins)", GOAL2, "Deleted report.csv and notes.txt.",
     [refuse(N_), dele(R_), obs("files.delete", "Deleted notes.txt successfully.", args={"path": N_, "retry": True})], "accept")


def section_g() -> None:
    scenario("G: unusable evidence about one object — an attempt, an uncertain, an overridden or a negated result grounds nothing")
    ok, why = group_ok("G-")
    check("G1: the second object's only evidence is an attempt line, a tool-flagged uncertain result, a success a later same-"
          "tool failure overrode, or an ok result that says it was NOT removed / wasn't deleted — the claim fails closed",
          ok, why)
    ok, why = group_ok("G+")
    check("G2: a refusal followed by a successful retry of the same operation IS support (the later result wins), so the "
          "answer stands", ok, why)
    real_two = [dele(R_), obs("files.check", "Removed notes.txt.", args={"path": N_})]
    check("G3: a real result in different words ('Removed notes.txt.' from another tool) supports the object — matching is "
          "by action family and file, not by the answer's exact verb",
          verdict(GOAL2, "Deleted report.csv and notes.txt.", real_two) == "accept", "")


# ---- H: conservative allowance ------------------------------------------------------------------------------
_KEEP = [dele(R_), obs("files.read", "notes.txt is locked.", args={"path": N_})]
case("H1", "'... and notes.txt is locked' (a new clause, not an object)", GOAL2, "Deleted report.csv and notes.txt is locked.", _KEEP, "accept")
case("H1", "comma splice: the next clause starts with the file", GOAL2, "Deleted report.csv, notes.txt was locked.",
     [dele(R_), obs("files.read", "notes.txt was locked.", args={"path": N_})], "accept")
case("H1", "'Deleted the old cache and kept notes.txt.'", "Delete the cache.", "Deleted the old cache and kept notes.txt.",
     [obs("files.delete", "Deleted the old cache.", args={"path": "cache"}), mention(N_, "notes.txt is intact.")], "accept")
case("H1", "passive + a new clause about the other file", GOAL2, "report.csv was deleted and notes.txt is locked.", _KEEP, "accept")
case("H1", "a relative clause hangs off the deleted file", GOAL2, "Deleted report.csv, which was 2 KB. notes.txt is untouched.",
     [dele(R_), mention(N_, "notes.txt is untouched.")], "accept")
case("H2", "'only report.csv, not notes.txt'", GOAL2, "Deleted only report.csv, not notes.txt.", [dele(R_), mention(N_)], "accept")
case("H2", "'instead of notes.txt'", GOAL2, "Deleted report.csv instead of notes.txt.", [dele(R_), mention(N_)], "accept")
case("H2", "'everything except notes.txt'", GOAL2, "Deleted everything except notes.txt.",
     [obs("files.delete", "Deleted 3 items.", args={"path": "all"}), mention(N_)], "accept")
case("H2", "an alternative ('or') asserts neither file, so either one satisfies it", GOAL2, "Deleted report.csv or notes.txt.", [dele(N_)], "accept")
case("H2", "the same alternative as a passive", GOAL2, "report.csv or notes.txt was deleted.", [dele(R_)], "accept")
case("H2", "a copy DESTINATION is not an object of the verb", "Copy report.csv into backup.zip.", "Copied report.csv into backup.zip.",
     [obs("files.copy", "Copied report.csv.", args={"path": R_, "dest": "backup.zip"})], "accept")
case("H2", "an honest partial SEARCH is not held to its objects", "Find report.csv and notes.txt.",
     "I searched for report.csv and notes.txt and found only report.csv.",
     [obs("files.find", "Found report.csv.", args={"name": R_}), obs("files.find", "No such file: notes.txt.", ok=False, args={"name": N_})],
     "kept", COMPOSER)


def section_h() -> None:
    scenario("H: conservative allowance — when object boundaries are ambiguous the existing grounded answer stands")
    ok, why = group_ok("H1")
    check("H1: a file that starts a NEW clause (subject of 'is locked', comma splice, 'and kept notes.txt', passive + new "
          "clause, a relative clause) is not read as an object of the deletion — the honest answer is accepted untouched "
          "even though only report.csv was deleted", ok, why)
    ok, why = group_ok("H2")
    check("H2: 'only … not', 'instead of', 'except', 'or', a copy destination, a file after 'from Downloads and', and an "
          "honest partial search are all left to the pre-24.9 behaviour — no false rejection", ok, why)


# ---- I: completeness stays separate -------------------------------------------------------------------------
def section_i() -> None:
    scenario("I: goal completeness (24.3) is not this check's job — grounding and completeness stay separate layers")
    ev = [dele(R_), refuse(N_)]
    out = discovery.ground_answer(GOAL2, "I deleted report.csv.", ev)
    check("I1: 'I deleted report.csv.' is GROUNDED (not rejected by the object check), and the completeness layer, as before, "
          "appends the real result the answer left out — the refusal for notes.txt",
          "insufficient" not in low(out) and out.startswith("I deleted report.csv.") and "permission denied" in low(out), out)
    out = discovery.ground_answer(GOAL2, "I deleted report.csv.", [dele(R_)])
    check("I2: with no evidence about notes.txt at all there is nothing for completeness to append, and the object check does "
          "not invent a requirement from the GOAL: the grounded answer comes back untouched", out == "I deleted report.csv.", out)


# ---- J: numeric and filename grounding still active ---------------------------------------------------------
def section_j() -> None:
    scenario("J: numeric and filename grounding (24.1 / 24.8) still guard a multi-object claim")
    ev = [dele(R_), dele(N_, "Deleted notes.txt successfully; 1200 MB freed.")]
    good = "Deleted report.csv and notes.txt, freeing 1200 MB."
    bad = "Deleted report.csv and notes.txt, freeing 9200 MB."
    check("J1: both objects are grounded, but an invented 4-digit figure still fails the whole answer closed (the real figure "
          "is accepted), and a filename the evidence never states (secret.txt) or states only inside a longer name "
          "(old-notes.txt) still fails closed whichever object it is listed as",
          verdict(GOAL2, good, ev) == "accept" and verdict(GOAL2, bad, ev) == "closed"
          and verdict(GOAL2, "Deleted report.csv and secret.txt.", [dele(R_)]) == "closed"
          and verdict(GOAL2, "Deleted report.csv and old-notes.txt.", TWO()) == "closed"
          and verdict(GOAL2, "Deleted report.csv, notes.txt and secret.txt.", TWO()) == "closed", "")


# ---- O: what the adversarial review of the first implementation found ---------------------------------------
BS = chr(92)  # a backslash, kept out of the source text
_FOUND_R = obs("files.find", "Found report.csv.", args={"name": R_})
_MISSING_N = obs("files.find", "No such file: notes.txt.", ok=False, args={"name": N_})
case("O+", "'... and report.csv now has 3 rows' is a new clause", G_N, "Deleted notes.txt, and report.csv now has 3 rows.",
     [dele(N_), mention(R_, "report.csv now has 3 rows.")], "accept")
case("O+", "'... and notes.txt remained untouched'", GOAL2, "Deleted report.csv and notes.txt remained untouched.",
     [dele(R_), mention(N_, "notes.txt remained untouched.")], "accept")
case("O+", "'notes.txt appears to be missing' (composer route)", "Find report.csv and notes.txt.",
     "I found report.csv, and notes.txt appears to be missing.", [_FOUND_R, _MISSING_N], "kept", COMPOSER)
case("O+", "a FUTURE passive is not a completed claim", GOAL2, "Deleted report.csv. notes.txt will be deleted once you confirm.", [dele(R_)], "accept")
case("O+", "a POSSIBLE passive is not a completed claim", GOAL2, "Deleted report.csv. notes.txt can be deleted too if you like.", [dele(R_)], "accept")
case("O+", "'Neither notes.txt nor data.csv was deleted.'", "Delete notes.txt and data.csv.", "Neither notes.txt nor data.csv was deleted.",
     [dele(R_), refuse(N_), refuse(D_)], "kept")
case("O+", "'None of notes.txt and data.csv were deleted.'", "Delete notes.txt and data.csv.", "None of notes.txt and data.csv were deleted.",
     [dele(R_), refuse(N_), refuse(D_)], "kept")
case("O+", "a reassurance in the evidence: 'No errors - deleted notes.txt.'", G_N, "Deleted notes.txt.",
     [obs("files.delete", "No errors - deleted notes.txt.", args={"path": N_})], "accept")
case("O+", "a non-ASCII name that only the returned data states", "Delete résumé.txt.", "Deleted résumé.txt.",
     [obs("files.delete", "Deleted 1 file.", args={"path": "x"}, data={"deleted": ["résumé.txt"]})], "accept")
case("O+", "one line per file: the next line's file is not an object of this line's verb", GOAL2, "Deleted report.csv\nnotes.txt: permission denied",
     [dele(R_), refuse(N_)], "kept")
case("O+", "'the copy that notes.txt pointed to' names no object", "Delete the copy of notes.txt.", "Deleted the copy that notes.txt pointed to.",
     [obs("files.delete", "Deleted the copy.", args={"path": "copy"}), mention(N_)], "accept")
case("O-", "single-quoted names", GOAL2, "Deleted 'report.csv' and 'notes.txt'.", [dele(R_)], "closed")
case("O-", "drive-and-backslash paths", GOAL2, f"Deleted C:{BS}docs{BS}report.csv and C:{BS}docs{BS}notes.txt.", [dele(R_)], "closed")
case("O-", "an appositive after a count: 'Deleted 2 files, X and Y.'", GOAL2, "Deleted 2 files, report.csv and notes.txt.", [dele(R_)], "closed")
case("O-", "'Deleted the following: X and Y.'", GOAL2, "Deleted the following: report.csv and notes.txt.", [dele(R_)], "closed")
case("O-", "'Moved X to Archive and Y to Backup.'", "Move report.csv and notes.txt.", "Moved report.csv to Archive and notes.txt to Backup.",
     [obs("files.move", "Moved report.csv to Archive.", args={"path": R_})], "closed")
case("O-", "'Deleted only notes.txt.' over the deletion of another file", GOAL2, "Deleted only notes.txt.", [dele(R_)], "closed")
case("O-", "'have all been deleted' passive", GOAL2, "report.csv and notes.txt have all been deleted.", [dele(R_)], "closed")
for _label, _speech in (
    ("'Nothing was deleted.' + the file named as unchanged", "Nothing was deleted. notes.txt is unchanged."),
    ("'0 files deleted.' + the file named as read-only", "0 files deleted. notes.txt is read-only."),
    ("'Skipped deleting notes.txt.'", "Skipped deleting notes.txt."),
    ("a lead-in, then 'Skipped notes.txt (in use).'", "Deleted 1 file. Skipped notes.txt (in use)."),
    ("a lead-in, then 'notes.txt was left alone.'", "Deleted 1 file. notes.txt was left alone."),
    ("a dry run: 'would delete notes.txt'", "Dry run: would delete notes.txt."),
    ("an offer: 'I can delete notes.txt if you confirm.'", "I can delete notes.txt if you confirm."),
    ("a longer name: 'Deleted notes.txt.bak.' is not notes.txt", "Deleted notes.txt.bak."),
):
    case("O=", f"evidence is not an outcome — {_label}", G_N, "Deleted notes.txt.", [obs("files.delete", _speech, args={"path": "x"})], "closed")
for _label, _speech in (
    ("a sibling state in the same speech: 'Deleted report.csv, notes.txt still exists.'", "Deleted report.csv, notes.txt still exists."),
    ("a contrast in the same speech: 'Deleted report.csv but left notes.txt in place.'", "Deleted report.csv but left notes.txt in place."),
    ("an exclusion in the same speech: 'Deleted report.csv and kept notes.txt.'", "Deleted report.csv and kept notes.txt."),
):
    case("O=", f"evidence names the file but not as deleted — {_label}", GOAL2, "Deleted report.csv and notes.txt.",
         [obs("files.delete", _speech, args={"path": R_})], "closed")
case("O=", "the data files the name under 'failed'", GOAL2, "Deleted report.csv and notes.txt.",
     [obs("files.delete", "Deleted 1 file.", args={"path": "x"}, data={"deleted": [R_], "failed": [N_]})], "closed")


def section_o() -> None:
    scenario("O: regressions found by the adversarial review of the first implementation — honest answers kept, look-alike claims rejected")
    ok, why = group_ok("O+")
    check("O1: honest answers stay accepted — a later clause about another file ('and report.csv now has 3 rows', 'remained "
          "untouched', 'appears to be missing'), a future or possible passive, 'Neither X nor Y was deleted', a reassurance in "
          "the evidence, a non-ASCII name in the data, one line per file, a relative clause", ok, why)
    ok, why = group_ok("O-", "O=")
    check("O2: look-alike claims are rejected — single-quoted names, backslash paths, 'Deleted 2 files, X and Y', 'the following:', "
          "'X to Archive and Y to Backup', 'Deleted only Y', 'have all been' — and evidence that is not an outcome (nothing / 0 "
          "deleted, skipped, a dry run, an offer, notes.txt.bak, a sibling state / contrast / exclusion in the same speech, a "
          "name filed under 'failed')", ok, why)


# ---- K: routes, read verbs, normalization -------------------------------------------------------------------
def section_k() -> None:
    scenario("K: both final-answer routes, read verbs, and the normalization that follows grounding")
    ev = [dele(R_), refuse(N_)]
    answer = "Deleted report.csv and notes.txt."
    outs = [discovery.finalize_answer(GOAL2, answer, ev, retrieval_claims=r) for r in BOTH]
    check("K1: `finalize_answer` (grounding, then removal-only normalization) rejects the multi-object claim identically on the "
          "composer route and the planner `done` route, and normalization does not bring the claim back",
          all("insufficient" in low(o) and "deleted notes.txt" not in low(o) and "deleted report.csv and notes.txt" not in low(o) for o in outs), str(outs))
    find_ev = [obs("files.find", "Found report.csv.", args={"name": R_}), obs("files.find", "No such file: notes.txt.", ok=False, args={"name": N_})]
    both_found = [obs("files.find", "Found report.csv.", args={"name": R_}), obs("files.find", "Found notes.txt.", args={"name": N_})]
    goal = "Find report.csv and notes.txt."
    claim = "Found report.csv and notes.txt."
    check("K2: a READ verb is held per object on the composer route ('Found report.csv and notes.txt.' with a not-found for "
          "notes.txt is rejected; with both found it is accepted), while the planner `done` route keeps its unchanged 24.5 read-verb "
          "paraphrase exemption (not rejected)", verdict(goal, claim, find_ev) == "closed" and verdict(goal, claim, both_found) == "accept"
          and kept(verdict(goal, claim, find_ev, retrieval_claims=False)), "")
    dup = "Deleted report.csv and notes.txt. Deleted report.csv and notes.txt."
    laundered = "Attempting to delete notes.txt. Deleted report.csv and notes.txt."
    check("K3: order is unchanged — a grounded two-object answer is deduplicated by 24.7 and keeps both names; an answer whose second "
          "object is ungrounded is closed BEFORE normalization can tidy it (an attempt line does not launder the claim)",
          discovery.finalize_answer(GOAL2, dup, TWO()) == "Deleted report.csv and notes.txt."
          and "insufficient" in low(discovery.finalize_answer(GOAL2, laundered, [dele(R_), obs("files.delete", "Attempting to delete notes.txt.", args={"path": N_})])), "")


# ---- L: the real Orchestrator.run_goal ----------------------------------------------------------------------
def section_l() -> None:
    scenario("L: real Orchestrator.run_goal — scripted model, recording tool world (composer route, `done` route, kill switch)")
    original = CFG.planner.answer_grounding_guard
    try:
        # composer route: report.csv and notes.txt really deleted, data.csv only READ; the composed answer claims all three
        N.reset()
        N.world(delete=[N.R("Deleted report.csv successfully."), N.R("Deleted notes.txt successfully.")], read=[N.R("data.csv has 5 lines.")])
        subs = [N.sg("Delete report.csv"), N.sg("Delete notes.txt"), N.sg("Read data.csv"), N.sg("Tell me what happened", "answer")]
        res, pl = N.drive("Delete report.csv and notes.txt, read data.csv and tell me what happened.", [
            call("test.nq.delete", {"path": R_}), call("test.nq.delete", {"path": N_}), call("test.nq.read", {"path": D_})],
            answer="Deleted report.csv, notes.txt and data.csv.", subgoals=subs)
        check("L1: Phase 23 composer — the model claims a third deletion (data.csv was only READ): the answer fails closed, the two "
              "real deletions are restated, and 'deleted data.csv' does not reach the user",
              pl.answer_calls == 1 and "insufficient" in low(res.summary) and "deleted report.csv successfully" in low(res.summary)
              and "deleted notes.txt successfully" in low(res.summary) and "deleted data.csv" not in low(res.summary) and "data.csv, and" not in low(res.summary),
              f"{pl.answer_calls} {N.ran()} {res.summary!r}")

        # `done` route: one deleted, one refused, the planner's own summary claims both
        N.reset()
        N.world(delete=[N.R("Deleted report.csv successfully."), N.R("Failed to delete notes.txt: permission denied.", ok=False)])
        res, _ = N.drive(GOAL2, [call("test.nq.delete", {"path": R_}), call("test.nq.delete", {"path": N_}), done("Deleted report.csv and notes.txt.")])
        check("L2: planner `done` route — the summary claims both files but notes.txt was refused: it fails closed and the user "
              "hears the real deletion AND the real refusal",
              N.ran() == ["delete", "delete"] and "insufficient" in low(res.summary) and "permission denied" in low(res.summary)
              and "deleted report.csv successfully" in low(res.summary) and "deleted notes.txt" not in low(res.summary), f"{N.ran()} {res.summary!r}")

        # both succeed: accepted verbatim on both routes
        N.reset()
        N.world(delete=[N.R("Deleted report.csv successfully."), N.R("Deleted notes.txt successfully.")])
        res_d, _ = N.drive(GOAL2, [call("test.nq.delete", {"path": R_}), call("test.nq.delete", {"path": N_}), done("Deleted report.csv and notes.txt.")])
        N.reset()
        N.world(delete=[N.R("Deleted report.csv successfully."), N.R("Deleted notes.txt successfully.")])
        subs = [N.sg("Delete report.csv"), N.sg("Delete notes.txt"), N.sg("Tell me what happened", "answer")]
        res_c, pl_c = N.drive("Delete report.csv and notes.txt and tell me what happened.",
                              [call("test.nq.delete", {"path": R_}), call("test.nq.delete", {"path": N_})],
                              answer="Deleted report.csv and notes.txt.", subgoals=subs)
        check("L3: both files really deleted — the multi-object answer reaches the user untouched on both routes (no false rejection)",
              res_d.summary == "Deleted report.csv and notes.txt." and res_c.summary == "Deleted report.csv and notes.txt." and pl_c.answer_calls == 1,
              f"{res_d.summary!r} | {res_c.summary!r}")

        # the kill switch: no Phase 24 grounding at all
        N.reset()
        CFG.planner.answer_grounding_guard = False
        N.world(delete=[N.R("Deleted report.csv successfully."), N.R("Failed to delete notes.txt: permission denied.", ok=False)])
        res, _ = N.drive(GOAL2, [call("test.nq.delete", {"path": R_}), call("test.nq.delete", {"path": N_}), done("Deleted report.csv and notes.txt.")])
        check("L4: with answer_grounding_guard off the planner's claim passes ungrounded — the per-object check is part of the same "
              "switch, not a second one", res.summary == "Deleted report.csv and notes.txt.", res.summary)
    finally:
        CFG.planner.answer_grounding_guard = original
        N.reset()


# ---- M: seeded property corpus ------------------------------------------------------------------------------
FILES = [R_, N_, D_, "log.txt", "todo.md"]
STATES = ["supported"] * 5 + ["failed", "attempt", "mention", "absent", "uncertain", "negated"]


def evidence_for(state: str, name: str) -> Observation | None:
    if state == "supported":
        return dele(name)
    if state == "failed":
        return refuse(name)
    if state == "attempt":
        return obs("files.delete", f"Attempting to delete {name}.", args={"path": name})
    if state == "mention":
        return mention(name)
    if state == "uncertain":
        return dele(name, data={"uncertain": True})
    if state == "negated":
        return obs("files.check", f"{name} was not removed.", args={"path": name})
    return None  # absent: named by the goal and the answer, never by the evidence


def _join(names: list[str], oxford: bool) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + (", and " if oxford else " and ") + names[-1]


PHRASINGS = {
    "and-list": lambda L, ox: f"Deleted {_join(L, ox)}.",
    "first person": lambda L, ox: f"I deleted {_join(L, ox)} for you.",
    "removed": lambda L, ox: f"Removed {_join(L, ox)} successfully.",
    "sentences": lambda L, ox: " ".join(f"Deleted {n}." for n in L),
    "colon": lambda L, ox: f"Deleted {len(L)} files: {_join(L, ox)}.",
    "passive": lambda L, ox: f"{_join(L, ox)} {'was' if len(L) == 1 else 'were'} deleted.",
    "have been": lambda L, ox: f"The files {_join(L, ox)} have been deleted.",
}
_SEED = 249


def corpus():
    rng = random.Random(_SEED)
    for _ in range(400):
        k = rng.randint(1, 4)
        names = rng.sample(FILES, k)
        states = {n: rng.choice(STATES) for n in names}
        phrasing = rng.choice([p for p in PHRASINGS if not (p == "colon" and k == 1)])
        yield names, states, phrasing, rng.random() < 0.5


def corpus_mismatches() -> tuple[list, dict]:
    """(runs where the guard disagreed with the independent oracle, coverage counters)."""
    wrong: list = []
    seen = {"accept": 0, "closed": 0, "states": set(), "phrasings": set(), "flips": 0}
    for names, states, phrasing, oxford in corpus():
        answer = PHRASINGS[phrasing](names, oxford)
        goal = "Delete " + _join(names, False) + "."
        evidence = [e for n in names if (e := evidence_for(states[n], n)) is not None]
        expect = "accept" if all(states[n] == "supported" for n in names) else "closed"
        seen["states"].update(states.values())
        seen["phrasings"].add(phrasing)
        seen[expect] += 1
        for routes in BOTH:
            got = verdict(goal, answer, evidence, retrieval_claims=routes)
            if got != expect:
                wrong.append((goal, answer, sorted(states.items()), got, expect))
        if expect == "accept" and len(names) > 1:  # each object is load-bearing: take one away and the claim must fall
            for victim in names:
                rest = [e for n in names if n != victim and (e := evidence_for(states[n], n)) is not None]
                seen["flips"] += 1
                if verdict(goal, answer, rest) != "closed":
                    wrong.append((goal, answer, f"without {victim}", verdict(goal, answer, rest), "closed"))
    return wrong, seen


def section_m() -> None:
    scenario("M: seeded property corpus (400 draws x 2 routes) — an object claim holds iff EVERY claimed object has a real outcome")
    wrong, seen = corpus_mismatches()
    check("M1 (invariant): over 400 seeded draws — 1-4 files, seven phrasings, each file supported / refused / only attempted / "
          "only read / unmentioned / uncertain / negated — the guard accepts exactly when every claimed file has a real deletion "
          "result, on both routes", not wrong, str(wrong[:1]))
    check("M2 (load-bearing): in every accepted multi-object draw, removing ANY single object's evidence turns the answer into a "
          f"fail-closed one ({seen['flips']} removals)", not wrong and seen["flips"] > 40, str(wrong[:1]))
    check("M3: the corpus is not vacuous — both outcomes are well represented, every evidence state and every phrasing is drawn",
          seen["accept"] >= 25 and seen["closed"] >= 150 and len(seen["states"]) == 7 and len(seen["phrasings"]) == len(PHRASINGS),
          f"{seen['accept']}/{seen['closed']} {sorted(seen['states'])} {sorted(seen['phrasings'])}")


# ---- N: in-suite mutation checks ----------------------------------------------------------------------------
@contextlib.contextmanager
def patched(**attrs):
    saved = {name: getattr(discovery, name) for name in attrs}
    for name, value in attrs.items():
        setattr(discovery, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(discovery, name, value)


def _all_cases() -> list:
    return [c for group in CASES.values() for c in group]


def _damage() -> tuple[list[str], int]:
    """(this suite's failing table cases, corpus disagreements) with whatever mutation is currently applied."""
    return failing(_all_cases()), len(corpus_mismatches()[0])


def section_n() -> None:
    scenario("N: in-suite mutation checks — a disabled or over-eager object check must be caught by THIS suite")
    base_cases, base_corpus = _damage()
    real_grounds, real_has = discovery._grounds_object, discovery._has_action

    with patched(_grounds_object=lambda o, stems, name: True):
        off_cases, off_corpus = _damage()
    check("N1: the per-object check disabled in-process — the headline case ('Deleted report.csv and notes.txt.' over one "
          f"deletion) is accepted again, and {len(off_cases)} table cases and {off_corpus} corpus draws fail (baseline "
          f"{len(base_cases)}/{base_corpus})",
          not base_cases and not base_corpus and len(off_cases) >= 12 and off_corpus > 100
          and any("goal names both, only report.csv" in c for c in off_cases), str(base_cases[:2]))

    every = lambda answer, s, e: [m.group(0).lower() for m in discovery._FILENAME_CLAIM_RE.finditer(answer)]  # noqa: E731
    with patched(_claimed_objects=every):
        eager_cases, eager_corpus = _damage()
    false_rejects = [c for c in eager_cases if "wanted accept" in c]
    check("N2: the inverse — extraction made too aggressive (EVERY filename in the answer is an object of every verb) is caught "
          f"as FALSE REJECTIONS: {len(false_rejects)} honest-answer cases fail (contrast, new clause, destination, 'only … not', "
          "honest partial failure …)", len(false_rejects) >= 6, str(false_rejects[:3]))

    def counts_args(o, stems, name):
        args = json.dumps(o.step.args).lower() if o.step is not None and o.step.args else ""
        return real_grounds(o, stems, name) or (name in args and real_has(discovery._observation_outcome_text(o), stems))

    def _negation_blind(text, stems):  # the real action detector minus the 24.2 negation window ("was not removed")
        return [m.start() for stem in stems for m in re.finditer(r"\b" + re.escape(stem), text)
                if not discovery._EVIDENCE_HEDGED_RE.search(text[max(0, m.start() - 40):m.start()])]

    mutants = {
        "arguments count as an outcome": {"_grounds_object": counts_args},
        "action-blind (a mention of the file suffices)": {"_has_action": lambda text, stems: True},
        "negation-blind ('was not removed' counts)": {"_action_starts": _negation_blind},
        "first-object gap unchecked ('… and kept notes.txt')": {"_object_gap_ok": lambda gap: True},
        "'notes.txt is …' read as an object": {"_OBJECT_SUBJECT_RE": re.compile(r"(?!)")},
        "passive lead-in not recognised": {"_OBJECT_PASSIVE_RE": re.compile(r"(?!)")},
        "markdown / quote decoration not stripped": {"_OBJECT_DECOR_RE": re.compile(r"(?!)")},
        "path prefix not stripped": {"_OBJECT_PATH_RE": re.compile(r"(?!)")},
        "a line break is not a boundary": {"_OBJECT_BREAK_RE": discovery._SENTENCE_BREAK_RE},
        "negated passive read as a claim": {"_OBJECT_NEG_LEAD_RE": re.compile(r"(?!)")},
        "offers / plans / skips / zero count as an action": {"_EVIDENCE_HEDGED_RE": re.compile(r"(?!)")},
        "a file named as a state's subject counts": {"_EVIDENCE_STATE_RE": re.compile(r"(?!)")},
        "contrast between the action and the file ignored": {"_EVIDENCE_CONTRAST_RE": re.compile(r"(?!)")},
        "args never identify an object (strict)": {"_grounds_object": lambda o, stems, name: real_grounds(o, stems, name) and name in discovery._observation_outcome_text(o)},
    }
    caught = {}
    for name, attrs in mutants.items():
        with patched(**attrs):
            cases_bad, corpus_bad = _damage()
        caught[name] = len(cases_bad) + corpus_bad
    check("N3: each individual guard is load-bearing — removing any one of them (arguments as outcome, action-blind, negation-blind, "
          "gap unchecked, subject clause, passive lead-in, decoration, path prefix, line break, negated passive, hedged "
          "evidence, state subject, contrast, args identity) makes at least one table case or corpus draw fail: "
          + ", ".join(f"{n.split(' (')[0]}={c}" for n, c in caught.items()), all(c >= 1 for c in caught.values()), str(caught))

    after_cases, after_corpus = _damage()
    check("N4: every mutation was undone — the helpers are the originals and the suite is back to zero failing cases",
          discovery._grounds_object is real_grounds and discovery._has_action is real_has and not after_cases and not after_corpus,
          str(after_cases[:2]))


def main() -> int:
    t0 = time.perf_counter()
    N.reset()
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()
    section_g()
    section_h()
    section_i()
    section_j()
    section_o()
    section_k()
    section_l()
    section_m()
    section_n()
    return H.finish("Phase 24.9 — per-object evidence grounding", time.perf_counter() - t0, min_assertions=30, min_scenarios=12)


if __name__ == "__main__":
    sys.exit(main())
