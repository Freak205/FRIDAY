"""Phase 24.8 — adversarial interaction hardening of the whole Phase 24 answer-reliability pipeline.

Phases 24.1-24.7 each pin ONE behaviour: what an answer may claim (grounding), which observation may
speak for it (progress / supersession / conflicts), whether it answers the goal (completeness), when a
run may stop, how the final text reads (normalization). This suite pins what happens when several of
them meet at once — 2-4 behaviours per case — through the same seams the real system uses:

  helper level   `discovery.finalize_answer` (= ground_answer, then normalize_answer) on BOTH routes:
                 the Phase 23 composer (retrieval_claims=True) and the planner `done` summary (False)
  real run_goal  the real `Orchestrator.run_goal` with a scripted model and a recording fake tool
                 world (the Phase 24.7 fixtures, imported from smoke_answer_normalization)

  A  progress + an unsupported claim          F  multi-clause goal + partial evidence + progress
  B  progress + success + a duplicate         G  multi-clause goal + complete evidence + irrelevant failure
  C  progress + failure + a stale success     H/I invented number / invented filename x normalization
  D  failure + retry + success + duplicate    J  failed observation + a false success
  E  conflicting tools                        K  uncertain observation x normalization
  L  the kill switch                          M  an already-clean answer
  N  conflict x normalization                 O  empty / degenerate cleanup
  P  real run_goal combinations               Q  seeded property corpus (two invariants)

The hardening found real defects, each fixed at its smallest responsible place and pinned here
(marked "24.8 fix" below):
  grounding   numbers and filenames were matched by plain substring ("999" rode on "1999",
              "report.csv" on "myreport.csv"); a number with a unit glued to it ("500GB") was never
              checked; the read-verb exemption of the `done` route let "Found X." through with only a
              progress line or failures behind it
  conflicts   an observation saying the file "still exists" was invisible to conflict detection;
              "no errors" / "nothing went wrong" read as the answer saying it FAILED, so a confident
              success hid a conflicting failure
  stops       the discovery sufficiency gate accepted a result the tool itself flagged uncertain; the
              deterministic summary read out the orchestrator's own ALREADY_TRIED / intent_mismatch notes
  normalizer  dedup stranded a list marker ("1. X 2."), merged identical-looking sentences that only
              differ by a pronoun's referent or a symbol ("-5"/"5", "50%"/"$50", "C++"/"C#"), dropped a
              reported state ("The backup is in progress.") as if it were an announcement, and dropped
              a failure about another file when the goal asks WHY (it is the cause)

Deterministic and offline: no Ollama, no real side effect.
"""

from __future__ import annotations

import random
import re
import sys
import time

import phase21_common as H
import smoke_answer_normalization as N  # the Phase 24.7 fixtures: obs(), norm(), R(), world(), drive(), ran(), ...
from phase21_common import call, check, done, scenario

from friday import orchestrator  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.orchestrator import Observation, PlanStep, ToolSpec  # noqa: E402
from friday.registry import SkillResult  # noqa: E402

# one more tool for the fake world (the 24.7 set has no existence check)
if not any(s.name == "test.nq.check" for s in N.SPECS):
    N.SPECS.append(ToolSpec(name="test.nq.check", description="check whether a file exists", tier="L0",
                            params="path (str)", action="read"))

R, REPORT, DEL, FIND_ROWS = N.R, N.REPORT, N.DEL, N.FIND_ROWS
GOAL_DEL = "Delete report.csv."


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None, data: dict | None = None) -> Observation:
    """N.obs plus `data` (an observation a tool itself flags uncertain carries data={"uncertain": True})."""
    return Observation(PlanStep(tool, args or {}), ok, speech, data or {}, "")


def both(goal: str, answer: str, evidence: list[Observation]) -> tuple[str, str]:
    """The finished answer on both final-answer routes: Phase 23 composer / planner `done` summary."""
    return (discovery.finalize_answer(goal, answer, evidence),
            discovery.finalize_answer(goal, answer, evidence, retrieval_claims=False))


def low(text: str) -> str:
    return (text or "").lower()


def closed(text: str) -> bool:
    """Either fail-closed message ('...treating it as insufficient...' / '...evidence gathered so far is insufficient...')."""
    return "insufficient" in low(text)


def claims_removal(text: str) -> bool:
    return bool(re.search(r"\b(?:deleted|removed)\b", low(text)))


def has_number(text: str, n: str) -> bool:
    return re.search(rf"(?<!\d){n}(?!\d)", text or "") is not None


# ================================================================================
# A — progress + an unsupported claim
# ================================================================================


def section_a() -> None:
    scenario("A: progress + an unsupported claim — an attempt line never grounds the claim")
    attempt = [obs("files.delete", "Attempting to delete report.csv.", args=REPORT)]
    outs = [o for ans in ("Deleted report.csv successfully.", "I deleted report.csv.",
                          "Attempting to delete report.csv. Deleted report.csv successfully.")
            for o in both(GOAL_DEL, ans, attempt)]
    check("A1: 'Deleted report.csv successfully.' over nothing but 'Attempting to delete report.csv.' fails closed on both "
          "routes (three phrasings) — the progress observation grounds nothing, and no deletion claim comes back",
          all(closed(o) and not claims_removal(o) for o in outs), str(outs[:2]))

    progress_only = [obs("files.find", "Searching for report.csv...", args={"name": "report.csv"})]
    failure_only = [obs("files.read", "Permission denied opening notes.txt.", ok=False, args={"path": "notes.txt"})]
    listing = [obs("files.read", "TODO.md lists: fix the parser and update the docs.", args={"path": "TODO.md"})]
    paraphrase = "Two outstanding items found: fix the parser and update the docs."
    found_p = discovery.finalize_answer("Find report.csv.", "Found report.csv.", progress_only, retrieval_claims=False)
    found_f = discovery.finalize_answer("Find report.csv.", "Found report.csv.", failure_only, retrieval_claims=False)
    check("A2 (24.8 fix): on the planner `done` route the read-verb exemption needs a real RESULT behind it — 'Found "
          "report.csv.' over a progress line or over failures alone fails closed, while a genuine paraphrase of what a "
          "read tool listed is still accepted verbatim",
          closed(found_p) and closed(found_f)
          and discovery.finalize_answer("What is outstanding?", paraphrase, listing, retrieval_claims=False) == paraphrase,
          f"{found_p!r} | {found_f!r}")


# ================================================================================
# B — progress + final success + a duplicated result
# ================================================================================


def section_b() -> None:
    scenario("B: progress + final success + a duplicated result — the success is stated once, the progress is gone")
    ev = [obs("files.begin", "Attempting to delete report.csv.", args=REPORT),
          obs("files.delete", "Deleted report.csv successfully.", args=REPORT),
          obs("files.delete", "Deleted report.csv successfully.", args={**REPORT, "force": True})]
    joined = " ".join(o.speech for o in ev)
    outs = both(GOAL_DEL, joined, ev)
    check("B1: 'Attempting... Deleted... Deleted...' -> 'Deleted report.csv successfully.' once, on both routes",
          outs == ("Deleted report.csv successfully.",) * 2, str(outs))
    CFG.planner.answer_grounding_guard = True
    check("B2: the deterministic summary of the same run (`_summarize`, the evidence-stop / done-without-summary text) "
          "reads the same",
          orchestrator._summarize(ev, GOAL_DEL) == "Deleted report.csv successfully.", orchestrator._summarize(ev, GOAL_DEL))


# ================================================================================
# C — progress + final failure + a stale success
# ================================================================================


def section_c() -> None:
    scenario("C: progress + final failure + a stale success — the failure stays visible, the stale success is never the last word")
    same = [obs("files.delete", "Attempting to delete report.csv.", args=REPORT),
            obs("files.delete", "Deleted report.csv successfully.", args={**REPORT, "n": 1}),
            obs("files.delete", "Delete failed: permission denied.", ok=False, args={**REPORT, "n": 2})]
    stale = both(GOAL_DEL, "Deleted report.csv successfully.", same)
    check("C1: one tool reported success and then FAILED the same operation: 'Deleted report.csv successfully.' is stale — "
          "rejected on both routes, the permission failure is what the user hears",
          all(closed(o) and "permission denied" in low(o) and "deleted report.csv successfully" not in low(o) for o in stale),
          str(stale))

    cross = [obs("files.begin", "Attempting to delete report.csv.", args=REPORT),
             obs("files.delete", "Deleted report.csv successfully.", args=REPORT),
             obs("files.remove", "Delete failed: permission denied.", ok=False, args=REPORT)]
    one_sided = both(GOAL_DEL, "Deleted report.csv successfully.", cross)
    check("C2: two different tools disagree (success vs failure): a one-sided success gets the failure appended — the "
          "success is not left standing alone",
          one_sided == ("Deleted report.csv successfully. Also: Delete failed: permission denied.",) * 2, str(one_sided))

    honest = both(GOAL_DEL, "Delete failed: permission denied.", same)
    history = both(GOAL_DEL, " ".join(o.speech for o in cross), cross)
    check("C3: the honest failure-only answer is untouched; the joined history (progress + success + failure) loses only "
          "the progress and ends on the failure",
          honest[0] == honest[1] == "Delete failed: permission denied."
          and history == ("Deleted report.csv successfully. Delete failed: permission denied.",) * 2, f"{honest} {history}")


# ================================================================================
# D — failure + retry success + duplicate
# ================================================================================


def section_d() -> None:
    scenario("D: failure + retry + success + duplicate — the successful final state is reported once")
    ev = [obs("files.delete", "Delete failed: permission denied.", ok=False, args=REPORT),
          obs("files.retry", "Retrying delete...", args=REPORT),
          obs("files.delete", "Deleted report.csv successfully.", args={**REPORT, "force": True}),
          obs("files.delete", "Deleted report.csv successfully.", args={**REPORT, "force": 2})]
    outs = both(GOAL_DEL, " ".join(o.speech for o in ev), ev)
    alone = both(GOAL_DEL, "Deleted report.csv successfully.", ev)
    check("D1: failure -> 'Retrying delete...' -> success -> success: the success appears exactly once, the retry status is "
          "gone, the earlier failure stays as honest history — and the retry's success ALONE is grounded and untouched (the "
          "superseded failure is neither demanded nor appended)",
          all(o.count("Deleted report.csv successfully.") == 1 and "retrying" not in low(o) and "permission denied" in low(o)
              for o in outs) and alone == ("Deleted report.csv successfully.",) * 2, f"{outs} {alone}")


# ================================================================================
# E — conflicting tools
# ================================================================================


def section_e() -> None:
    scenario("E: conflicting tools — no invented resolution: both sides are communicated, or the certainty is not allowed")
    removal = obs("files.delete", "Deleted report.csv successfully.", args=REPORT)
    exists = obs("files.check", "report.csv still exists.", args=REPORT)
    win = "Deleted report.csv successfully."
    still = "report.csv still exists."
    cases = []
    for ev in ([removal, exists], [exists, removal]):  # whichever tool spoke first
        cases += list(both(GOAL_DEL, win, ev)) + list(both(GOAL_DEL, still, ev))
    check("E1 (24.8 fix): an answer that takes ONE side ('Deleted...' alone / 'still exists' alone) gets the other tool's "
          "words appended verbatim, both routes, either evidence order — neither certainty stands alone",
          all(win in o and still in o for o in cases), str(cases[:2]))

    stated = [f"{win} {still}", f"{win} But {still}", "The delete tool says it deleted report.csv, but the check says report.csv still exists."]
    unchanged = [discovery.finalize_answer(GOAL_DEL, a, [removal, exists]) is a for a in stated]
    check("E2: an answer that already communicates the conflict passes untouched (three wordings)",
          all(unchanged), str(unchanged))

    other_file = [removal, obs("files.check", "notes.txt still exists.", args={"path": "notes.txt"})]
    same_tool = [removal, obs("files.delete", "report.csv still exists.", args={**REPORT, "n": 2})]
    made = [obs("files.create", "Created report.csv successfully.", args=REPORT), exists]
    exist_q = [exists]
    check("E3: no false conflicts — another file, the same tool re-checking, a create, an existence question all stay clean",
          discovery.finalize_answer(GOAL_DEL, win, other_file) == win
          and discovery.finalize_answer(GOAL_DEL, win, same_tool) == win
          and discovery.finalize_answer("Create report.csv.", "Created report.csv successfully.", made) == "Created report.csv successfully."
          and discovery.finalize_answer("Does report.csv still exist?", "Yes, report.csv still exists.", exist_q) == "Yes, report.csv still exists.")

    cross = [removal, obs("files.remove", "Delete failed: permission denied.", ok=False, args=REPORT)]
    reassured = ["Deleted report.csv with no errors.", "Deleted report.csv successfully, no issues.", "Deleted report.csv. Nothing went wrong.",
                 "Deleted report.csv without any problem."]
    hidden = [o for a in reassured for o in both(GOAL_DEL, a, cross) if "permission denied" not in low(o)]
    check("E4 (24.8 fix): a confident one-sided success cannot hide the conflicting failure behind reassurance ('no errors', "
          "'no issues', 'nothing went wrong', 'without any problem' are not the answer saying it failed) — the failure is "
          "appended every time, both routes", not hidden, str(hidden[:1]))


# ================================================================================
# F — multi-clause goal + partial evidence + progress (helper level; the run is in P)
# ================================================================================


def section_f() -> None:
    scenario("F: 'Find report.csv and tell me its row count.' + partial evidence + progress — nothing is invented")
    ev = [obs("files.find", "Searching for report.csv...", args={"name": "report.csv"}),
          obs("files.find", "Found report.csv.", args={"name": "report.csv", "where": "."})]
    invented = both(FIND_ROWS, "Found report.csv. It has 1200 rows.", ev)
    honest = both(FIND_ROWS, "Found report.csv.", ev)
    tidy = both(FIND_ROWS, "Searching for report.csv... Found report.csv.", ev)
    check("F1: an invented 3+ digit row count never survives (fail closed, the real 'Found report.csv.' is what is restated); "
          "the honest partial answer is untouched; the progress line before it is dropped",
          all(not has_number(o, "1200") and closed(o) and "found report.csv" in low(o) for o in invented)
          and honest == ("Found report.csv.",) * 2 and tidy == ("Found report.csv.",) * 2, f"{invented} {honest} {tidy}")


# ================================================================================
# G — multi-clause goal + complete evidence + irrelevant failure
# ================================================================================


def section_g() -> None:
    scenario("G: multi-clause goal + complete evidence + an irrelevant failure — the requested results are retained")
    ev = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}),
          obs("files.rows", "report.csv contains 42 rows.", args=REPORT), N.NOTES_FAIL]
    tidy = "Found report.csv. report.csv contains 42 rows."
    full = f"{tidy} Permission denied opening notes.txt."
    outs = both(FIND_ROWS, full, ev)
    check("G1: whichever way the pipeline treats the unrelated notes.txt failure, both requested results survive on both "
          "routes — and the answer without the failure is untouched",
          all("found report.csv" in low(o) and "report.csv contains 42 rows" in low(o) for o in outs)
          and both(FIND_ROWS, tidy, ev) == (tidy, tidy), str(outs))
    goal_notes = "Find report.csv and notes.txt and tell me report.csv's row count."
    check("G2: normalization may drop that failure only because the goal never named notes.txt (its existing rule) — "
          "when the goal names it, it stays",
          N.norm(FIND_ROWS, full, ev) == tidy and N.norm(goal_notes, full, ev) == full, N.norm(FIND_ROWS, full, ev))

    ask = "Find report.csv and tell me how many rows it has."
    ev_q = [obs("files.find", "Searching for report.csv...", args={"name": "report.csv"}),
            obs("files.find", "Found report.csv.", args={"name": "report.csv", "where": "."}),
            obs("files.rows", "report.csv contains 42 rows.", args=REPORT)]
    want = "Found report.csv. Also: report.csv contains 42 rows."
    grounded = discovery.ground_answer(ask, "Found report.csv.", ev_q)
    real_stage = discovery._drop_restated
    discovery._drop_restated = lambda sents, kept, protected=None: kept[:1]  # a stage that would take the restored result away
    try:
        kept = N.norm(ask, grounded, ev_q)
    finally:
        discovery._drop_restated = real_stage
    check("G3: an answer that DROPS a requested result ('how many rows' answered with only 'Found report.csv.') gets the real "
          "result back verbatim, the progress line before it is gone — and normalization refuses to take the restored "
          "result away again",
          both(ask, "Found report.csv.", ev_q) == (want,) * 2 and both(ask, "Searching for report.csv... Found report.csv.", ev_q)
          == (want,) * 2 and grounded == kept == want, f"{grounded!r} {kept!r}")

    ev_p = [obs("system.disk", "Disk C: has 200 GB free.", args={"drive": "C"}), obs("files.rows", "report.csv contains 42 rows.", args=REPORT)]
    tail = discovery.finalize_answer(FIND_ROWS, "report.csv contains 999 rows.", ev_p)
    check("G4: when grounding must restate the evidence (fail closed) the goal-relevant result LEADS and the irrelevant status "
          "trails — evidence is prioritized, not replayed chronologically",
          closed(tail) and "report.csv contains 42 rows" in tail and "Disk C" in tail
          and tail.index("report.csv contains 42 rows") < tail.index("Disk C"), tail)


# ================================================================================
# H / I — invented number / invented filename x normalization
# ================================================================================


def section_hi() -> None:
    scenario("H/I: an invented number or filename survives neither grounding nor normalization")
    rows = [obs("files.rows", "report.csv contains 42 rows.", args=REPORT)]
    ask = "How many rows does report.csv have?"
    inv = both(ask, "report.csv contains 42 rows. Also, it contains 999 rows.", rows)
    glued = [obs("files.stat", "report.csv is 1999KB.", args=REPORT)]
    unit = both("How large is report.csv?", "report.csv is 500GB.", glued)
    check("H1: 'report.csv contains 42 rows. Also, it contains 999 rows.' -> the invented 999 is gone on both routes; the "
          "real 42 is what the user is told — and an invented number with a unit glued to it ('500GB') is checked like '500 GB'",
          all(not has_number(o, "999") and has_number(o, "42") and closed(o) for o in inv)
          and all(closed(o) and not has_number(o, "500") for o in unit), f"{inv} {unit}")
    big = [obs("files.rows", "report.csv contains 1999 rows.", args=REPORT)]
    embedded = both(ask, "report.csv contains 999 rows.", big) + both(ask, "report.csv contains 199 rows.", big)
    comma = [obs("files.rows", "report.csv contains 1,999 rows.", args=REPORT)]
    about = "Tell me about report.csv."
    check("H2 (24.8 fix): a number is judged as a WHOLE value — '999' / '199' do not ride on '1999' — while the real number "
          "still passes: with a unit glued to it in the evidence, and however its thousands are written ('1,999' vs '1999', "
          "either way round)",
          all(not has_number(o, "999") and not has_number(o, "199") and has_number(o, "1999") for o in embedded)
          and both(ask, "report.csv contains 1999 rows.", big) == ("report.csv contains 1999 rows.",) * 2
          and both("How large is report.csv?", "report.csv is 1999 KB.", glued) == ("report.csv is 1999 KB.",) * 2
          and both(about, "report.csv has 1,999 rows.", big) == ("report.csv has 1,999 rows.",) * 2
          and both(about, "report.csv has 1999 rows.", comma) == ("report.csv has 1999 rows.",) * 2
          and all(closed(o) for o in both(about, "report.csv has 2,999 rows.", big)), str(embedded))

    created = [obs("files.create", "Created report.csv.", args=REPORT)]
    inv2 = both("Create report.csv.", "Created report.csv. Also created secret.txt.", created)
    check("I1: 'Created report.csv. Also created secret.txt.' -> secret.txt does not survive; report.csv still does",
          all("secret.txt" not in low(o) and "report.csv" in low(o) and closed(o) for o in inv2), str(inv2))
    mine = [obs("files.find", "Found myreport.csv.", args={"name": "report"})]
    path = [obs("files.find", r"Found C:\Users\me\Documents\report.csv.", args={"name": "report"})]
    a = both("Find my report.", "Found report.csv.", mine)
    b = both("Find my report.", "Found old-report.csv.", mine)
    check("I2 (24.8 fix): a filename is judged as a whole name — 'report.csv' does not ride on 'myreport.csv', 'old-report.csv' "
          "is not 'myreport.csv' — while a path prefix in the evidence still counts",
          all(closed(o) for o in a + b) and both("Find my report.", "Found report.csv.", path) == ("Found report.csv.",) * 2,
          f"{a} {b}")


# ================================================================================
# J — failed observation + false-positive success
# ================================================================================


def section_j() -> None:
    scenario("J: a failed observation + a false-positive success — fail closed, and the real failure is what is said")
    fail = obs("files.delete", "Permission denied deleting report.csv.", ok=False, args=REPORT)
    outs = both(GOAL_DEL, "Deleted report.csv successfully.", [fail])
    mixed = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}), fail]
    outs2 = both("Find report.csv and delete it.", "Deleted report.csv successfully.", mixed)
    check("J1: 'Deleted report.csv successfully.' over 'Permission denied deleting report.csv.' fails closed on both routes and "
          "the failure is what is said — and another RELEVANT, successful observation (the find) does not ground the delete claim either",
          all(closed(o) and "permission denied deleting report.csv" in low(o) and not claims_removal(o) for o in outs)
          and all(closed(o) and not claims_removal(o) for o in outs2), f"{outs} {outs2}")


# ================================================================================
# K — uncertain observation + normalization
# ================================================================================


def section_k() -> None:
    scenario("K: an uncertain observation is never sufficient grounding, and normalization never blurs a hedge into a fact")
    ask = "How many rows does report.csv have?"
    unc = obs("files.rows", "report.csv probably contains 42 rows.", args=REPORT, data={"uncertain": True})
    outs = both(ask, "report.csv contains 42 rows.", [unc])
    check("K1: 'report.csv contains 42 rows.' over an uncertain 'probably 42 rows' -> 'I don't have enough confirmed evidence', "
          "both routes", all("enough confirmed evidence" in low(o) for o in outs), str(outs))
    big = obs("files.rows", "report.csv probably contains 4200 rows.", args=REPORT, data={"uncertain": True})
    with_real = [obs("files.find", "Found report.csv.", args={"name": "report.csv"}), big]
    outs2 = both(FIND_ROWS, "Found report.csv. It has 4200 rows.", with_real)
    hedged = "report.csv probably contains 42 rows. report.csv contains 42 rows."
    check("K2: even with a real observation present, an uncertain 3+ digit value cannot ground the answer (4200 does not "
          "survive); and a hedged sentence next to its firm restatement is never merged by normalization (different claims)",
          all(not has_number(o, "4200") and closed(o) for o in outs2)
          and N.norm(ask, hedged, [obs("files.find", "Found report.csv.", args={"name": "report.csv"})]) == hedged, str(outs2))


# ================================================================================
# L — the kill switch
# ================================================================================


def section_l() -> None:
    scenario("L: the kill switch — with answer_grounding_guard off no Phase 24 grounding or normalization is applied, on any route")
    original = CFG.planner.answer_grounding_guard
    subs = lambda: [N.sg("Create report.csv"), N.sg("Tell me what happened", "answer")]  # noqa: E731
    try:
        N.reset()
        CFG.planner.answer_grounding_guard = False
        N.world(create=[R("Created report.csv successfully.")])
        text = "Created report.csv successfully. Created report.csv successfully. Also created secret.txt."
        res_c, _ = N.drive("Create report.csv and tell me what happened.", [call("test.nq.create", N.CREATE)], answer=text,
                           subgoals=subs())
        check("L1: composer route — the composed answer reaches the user verbatim (its duplicate and its invented secret.txt "
              "included)", res_c.summary == text, res_c.summary)

        N.reset()
        CFG.planner.answer_grounding_guard = False
        N.world(begin=[R("Attempting to delete report.csv.")])
        res_d, _ = N.drive(GOAL_DEL, [call("test.nq.begin", DEL), done("Deleted report.csv successfully.")])
        N.reset()
        CFG.planner.answer_grounding_guard = False
        N.world(begin=[R("Attempting to delete report.csv.")], finish=[R("Deleted report.csv successfully.")])
        res_s, _ = N.drive(GOAL_DEL, [call("test.nq.begin", DEL), call("test.nq.finish", DEL),
                                       call("test.nq.finish", {**DEL, "force": True}), done("")])
        check("L2: `done` route and the deterministic summary — the planner's claim passes ungrounded, and the raw joined "
              "text is not normalized",
              res_d.summary == "Deleted report.csv successfully."
              and res_s.summary == "Attempting to delete report.csv. Deleted report.csv successfully. Deleted report.csv successfully.",
              f"{res_d.summary!r} | {res_s.summary!r}")
    finally:
        CFG.planner.answer_grounding_guard = original
        N.reset()
    check("L3: the switch itself is left exactly as found (this suite only sets it in-process, and it defaults to on)",
          CFG.planner.answer_grounding_guard is original is True, str(CFG.planner.answer_grounding_guard))


# ================================================================================
# M — an already-clean answer
# ================================================================================


def section_m() -> None:
    scenario("M: an already-clean answer is returned unchanged — no unnecessary edits")
    ev = N.FIND_EV + [obs("files.delete", "Deleted report.csv successfully.", args=REPORT)]
    clean = ["Found report.csv.", "Deleted report.csv successfully.", "report.csv contains 42 rows.", "Found report.csv. It has 42 rows.",
             "1. Found report.csv. 2. report.csv contains 42 rows.", "- Found report.csv.\n- report.csv contains 42 rows."]
    same = [all(o is a for o in both(FIND_ROWS, a, ev)) for a in clean]
    check("M1: concise grounded answers — including numbered and bulleted lists — come back as the very same string from "
          "both routes", all(same), str(same))
    twin = "1. Created report.csv successfully. 2. Created report.csv successfully."
    twin_ev = [obs("files.create", "Created report.csv successfully.", args=REPORT),
               obs("files.make", "Created report.csv successfully.", args={**REPORT, "overwrite": True})]
    lists = N.norm("Create report.csv.", "1. Found report.csv. 2. Found report.csv. 1. Found report.csv.", N.FIND_EV)
    check("M2 (24.8 fix): a list marker travels with its item — two numbered items are not the same statement and stay; a "
          "repeated item leaves whole (never a stranded '2.')",
          N.norm("Create report.csv.", twin, twin_ev) == twin and lists == "1. Found report.csv. 2. Found report.csv.", lists)

    generic = [obs("t.x", "ok")]
    cause = [obs("files.read", "report.csv has 300 characters.", args={"path": "report.csv"}),
             obs("files.read", "Cannot open config.ini: no such file.", ok=False, args={"path": "config.ini"})]
    depends = [  # (goal, evidence, text): identical-looking sentences whose meaning depends on what surrounds them
        ("Find report.csv and notes.txt and tell me how many rows each has.", generic,
         "Found report.csv. It has 42 rows. Found notes.txt. It has 42 rows."),           # one pronoun, two referents
        ("What is the temperature?", generic, "The temperature is -5 degrees. The temperature is 5 degrees."),   # a sign
        ("What is the discount?", generic, "The discount is 50%. The discount is $50."),                        # symbols
        ("What languages does my project use?", generic, "The project uses C++. The project uses C#."),
        ("Is the backup finished?", generic, "The backup is in progress. The backup started at 10:15 and covers 3 folders."),
        ("List the files.", generic, "report.csv, notes.txt, todo.md, ... Found report.csv."),                    # a truncated list
        ("Why does report.csv fail to load?", cause,                                                             # the CAUSE, in another file
         "report.csv has 300 characters. Cannot open config.ini: no such file."),
    ]
    lost = [t for g, e, t in depends if N.norm(g, t, e) != t]
    check("M3 (24.8 fix): normalization never merges or drops what only LOOKS repeated or settled — a pronoun with two referents, "
          "-5 vs 5, 50% vs $50, C++ vs C#, a reported state ('...is in progress.'), a truncated list, and a failure about "
          "another file when the goal asks WHY (it is the cause)", not lost, str(lost[:1]))


# ================================================================================
# N — conflict + normalization
# ================================================================================


def section_n() -> None:
    scenario("N: a meaningful conflict x normalization — neither side is removed for merely looking repetitive")
    removal = obs("files.delete", "Deleted report.csv successfully.", args=REPORT)
    exists = obs("files.check", "report.csv still exists.", args=REPORT)
    ev_still = [removal, exists]
    ev_fail = [obs("files.delete", "Deleted report.csv.", args=REPORT),
               obs("files.check", "Could not confirm the deletion: report.csv is still there.", ok=False, args=REPORT)]
    a_still = "Deleted report.csv successfully. Deleted report.csv successfully. report.csv still exists."
    a_fail = "Deleted report.csv. Deleted report.csv. Could not confirm the deletion: report.csv is still there."
    check("N1: with one side stated twice, only the repeat goes — both sides of the conflict stay (two conflict wordings, both routes)",
          both(GOAL_DEL, a_still, ev_still) == ("Deleted report.csv successfully. report.csv still exists.",) * 2
          and both(GOAL_DEL, a_fail, ev_fail)
          == ("Deleted report.csv. Could not confirm the deletion: report.csv is still there.",) * 2)

    real_stage = discovery._drop_restated
    discovery._drop_restated = lambda sents, kept, protected=None: kept[:1]  # a stage that would leave the answer one-sided
    try:
        first = N.norm(GOAL_DEL, f"{'Deleted report.csv successfully.'} report.csv still exists.", ev_still)
        second = N.norm(GOAL_DEL, "Deleted report.csv. Could not confirm the deletion: report.csv is still there.", ev_fail)
    finally:
        discovery._drop_restated = real_stage
    check("N2: a removal that WOULD drop one side is refused by the conflict check — the answer comes back unchanged",
          first == "Deleted report.csv successfully. report.csv still exists."
          and second == "Deleted report.csv. Could not confirm the deletion: report.csv is still there.", f"{first!r} | {second!r}")


# ================================================================================
# O — empty / degenerate output
# ================================================================================


def section_o() -> None:
    scenario("O: cleanup that would leave nothing useful — the original grounded answer survives")
    dup = "Created report.csv successfully. Created report.csv successfully."
    real_stage, real_rebuild = discovery._drop_restated, discovery._rebuild
    discovery._drop_restated = lambda sents, kept, protected=None: []  # would leave NOTHING
    try:
        emptied = N.norm("Create report.csv.", dup, N.CREATE_EV)
    finally:
        discovery._drop_restated = real_stage
    discovery._rebuild = lambda text, spans, kept: ""  # a rebuild that produced an empty answer
    try:
        blank = N.norm("Create report.csv.", dup, N.CREATE_EV)
    finally:
        discovery._rebuild = real_rebuild
    only_status = "Attempting to delete report.csv. Working on it..."
    kept = discovery.finalize_answer(GOAL_DEL, only_status, [obs("files.delete", "Attempting to delete report.csv.", args=REPORT),
                                                              obs("files.progress", "Working on it...", args=REPORT)])
    check("O1: a stage that empties the answer, and a rebuild that yields '', both fall back to the original; empty input stays "
          "empty; an answer that is ONLY status lines, with nothing to settle them, is never emptied or shortened",
          emptied == dup and blank == dup and N.norm("g", "", N.CREATE_EV) == "" and N.norm("g", "  ", N.CREATE_EV) == "  "
          and kept == only_status, f"{emptied!r} {blank!r} {kept!r}")


# ================================================================================
# P — the real Orchestrator.run_goal
# ================================================================================


def section_p() -> None:
    scenario("P: real Orchestrator.run_goal — scripted model, recording tool world, four+ combinations end to end")
    N.reset()
    N.world(begin=[R("Attempting to delete report.csv.")], finish=[R("Deleted report.csv successfully.")])
    res, _ = N.drive(GOAL_DEL, [call("test.nq.begin", DEL), call("test.nq.finish", DEL),
                                 call("test.nq.finish", {**DEL, "force": True}), done("")])
    check("P1: progress -> success -> a duplicated (really executed) result, planner says done with no summary: the "
          "deterministic text is 'Deleted report.csv successfully.' once",
          N.ran() == ["begin", "finish", "finish"] and res.ok and res.summary == "Deleted report.csv successfully.",
          f"{N.ran()} {res.summary!r}")

    N.reset()
    N.world(begin=[R("Attempting to delete report.csv.")], finish=[R("Deleted report.csv successfully.")])
    res, _ = N.drive(GOAL_DEL, [call("test.nq.begin", DEL), call("test.nq.finish", DEL), call("test.nq.finish", DEL), done("")])
    check("P2 (24.8 fix): the same, but the duplicate is a BLOCKED repeat (the repeat guard's own ALREADY_TRIED note) — the "
          "note is not read out as part of the answer",
          N.ran() == ["begin", "finish"] and res.summary == "Deleted report.csv successfully." and "ALREADY_TRIED" not in res.summary,
          f"{N.ran()} {res.summary!r}")

    N.reset()
    N.world(delete=[R("Delete failed: permission denied.", ok=False), R("Deleted report.csv successfully.")],
            progress=[R("Retrying delete...")], finish=[R("Deleted report.csv successfully.")])
    res, _ = N.drive(GOAL_DEL, [
        call("test.nq.delete", DEL), call("test.nq.progress"), call("test.nq.delete", {**DEL, "force": True}),
        call("test.nq.finish", DEL),
        done("Delete failed: permission denied. Retrying delete... Deleted report.csv successfully. Deleted report.csv successfully."),
    ])
    check("P3: failure -> retry status -> retry success -> duplicate, planner `done` echoing all of it: the success once, the "
          "retry status gone, the honest failure kept",
          N.ran() == ["delete", "progress", "delete", "finish"] and res.ok
          and res.summary == "Delete failed: permission denied. Deleted report.csv successfully.", f"{N.ran()} {res.summary!r}")

    N.reset()
    N.world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    find_a, find_b = call("test.nq.find", {"name": "report.csv"}), call("test.nq.find", {"name": "report.csv", "where": "."})
    res, pl = N.drive(FIND_ROWS, [find_a, find_b, done("Found report.csv.")], mode="look")
    n_calls = pl.decision_calls
    N.reset()
    N.world(find=[R("Searching for report.csv..."), R("Found report.csv.")])
    res_inv, _ = N.drive(FIND_ROWS, [find_a, find_b, done("Found report.csv. It has 1200 rows.")], mode="look")
    check("P4: multi-clause goal, partial evidence: progress + 'Found report.csv.' do NOT complete the run (the planner is "
          "consulted again and its premature `done` is nudged back), nothing is invented — and an invented 3+ digit "
          "count on the way out fails closed",
          N.ran() == ["find", "find"] and n_calls == 4 and res.summary == "Found report.csv."
          and closed(res_inv.summary) and not has_number(res_inv.summary, "1200"),
          f"{n_calls} {res.summary!r} | {res_inv.summary!r}")

    N.reset()
    N.world(delete=[R("Deleted report.csv successfully.")], check=[R("report.csv still exists.")])
    res, _ = N.drive(GOAL_DEL, [call("test.nq.delete", DEL), call("test.nq.check", REPORT), done("Deleted report.csv successfully.")])
    check("P5: conflict — one tool deleted it, another says it still exists, the planner asserts only the deletion: the user "
          "hears both", res.summary == "Deleted report.csv successfully. Also: report.csv still exists.", res.summary)

    N.reset()
    N.world(begin=[R("Attempting to delete report.csv.")])
    res, _ = N.drive(GOAL_DEL, [call("test.nq.begin", DEL), done("Deleted report.csv successfully.")])
    check("P6: progress + an unsupported claim — the planner announces a deletion that only ever started: fail closed",
          closed(res.summary) and not claims_removal(res.summary), res.summary)

    N.reset()
    N.world(begin=[R("Attempting to delete report.csv.")], finish=[R("Deleted report.csv successfully.")])
    res, pl = N.drive("Delete report.csv and tell me what happened.", [call("test.nq.begin", DEL), call("test.nq.finish", DEL)],
                      answer="Attempting to delete report.csv. Deleted report.csv successfully. Deleted report.csv successfully. "
                             "Also created secret.txt.",
                      subgoals=[N.sg("Delete report.csv"), N.sg("Tell me what happened", "answer")])
    check("P7: Phase 23 composer with progress + a duplicate + an invented filename at once: it fails closed, secret.txt does not "
          "come back, normalization does not undo that, and the confirmed deletion is restated once",
          pl.answer_calls == 1 and closed(res.summary) and "secret.txt" not in low(res.summary)
          and low(res.summary).count("deleted report.csv successfully") == 1 and "attempting" not in low(res.summary),
          f"{pl.answer_calls} {res.summary!r}")

    N.reset()
    N.world(find=[SkillResult(speech="Found report.csv.", ok=True, data={"uncertain": True})])
    res, pl = N.drive("Find report.csv.", [call("test.nq.find", {"name": "report.csv"}), done("Found report.csv.")], mode="disc")
    check("P8 (24.8 fix): a result the TOOL flagged uncertain does not stop a discovery run as 'sufficient' (the look-only stop "
          "and per-clause coverage already refuse it): the planner is asked again, and its 'Found report.csv.' has nothing "
          "confirmed behind it",
          pl.decision_calls == 2 and "enough confirmed evidence" in low(res.summary), f"{pl.decision_calls} {res.summary!r}")
    N.reset()


# ================================================================================
# Q — a small seeded property corpus (two invariants)
# ================================================================================

# (tool, speech, ok, uncertain, error, file it is about)
EVIDENCE_POOL = [
    ("files.begin", "Attempting to delete report.csv.", True, False, "", "report.csv"),
    ("files.find", "Searching for report.csv...", True, False, "", "report.csv"),
    ("files.retry", "Retrying delete...", True, False, "", "report.csv"),
    ("files.delete", "Deleted report.csv successfully.", True, False, "", "report.csv"),
    ("files.remove", "Deleted report.csv successfully.", True, False, "", "report.csv"),
    ("files.find", "Found report.csv.", True, False, "", "report.csv"),
    ("files.rows", "report.csv contains 42 rows.", True, False, "", "report.csv"),
    ("files.rows", "report.csv has 1999 rows.", True, False, "", "report.csv"),
    ("files.stat", "report.csv is 1999KB.", True, False, "", "report.csv"),
    ("files.delete", "Delete failed: permission denied.", False, False, "", "report.csv"),
    ("files.read", "Permission denied opening notes.txt.", False, False, "", "notes.txt"),
    ("files.check", "report.csv still exists.", True, False, "", "report.csv"),
    ("files.find", "Found myreport.csv.", True, False, "", "myreport.csv"),
    ("files.rows", "report.csv probably contains 4200 rows.", True, True, "", "report.csv"),
    ("files.find", "ALREADY_TRIED: 'files.find' was already run with these exact arguments. Previous result (ok): Found other.csv.",
     False, False, "repeated_call", "report.csv"),
]
ANSWER_POOL = [  # what a composer / planner might say: real speech, its duplicates, and adversarial claims
    "Attempting to delete report.csv.", "Searching for report.csv...", "Retrying delete...", "Deleted report.csv successfully.",
    "Found report.csv.", "report.csv contains 42 rows.", "Delete failed: permission denied.", "Permission denied opening notes.txt.",
    "report.csv still exists.", "Also: Deleted report.csv successfully.",
    "It contains 999 rows.", "report.csv contains 199 rows.", "It has 4200 rows.", "There are 19999 rows.",
    "Also created secret.txt.", "Found old-report.csv.", "Found report.csv in Documents.", "I deleted notes.txt.",
    "Found myreport.csv.", "Yes.",
]
GOALS = [GOAL_DEL, FIND_ROWS, "How many rows does report.csv have?", "Find report.csv and notes.txt.", "Is report.csv still there?"]


def _oracle_text(evidence: list[Observation], goal: str) -> str:
    """Everything an answer may legitimately quote: the goal and every observation that really RAN and was not flagged
    uncertain — successes AND failures (looser than the guard, on purpose: a violation of THIS is unambiguous)."""
    parts = [goal]
    for o in evidence:
        if o.error in orchestrator.NOT_EXECUTED_ERRORS or (o.data or {}).get("uncertain"):
            continue
        parts += [o.speech, o.step.tool, " ".join(str(v) for v in o.step.args.values())]
    return " ".join(parts).lower()


def _concrete_values(text: str) -> tuple[set[str], set[str]]:
    """(3+ digit numbers, filenames) as WHOLE tokens — independent of the guard's own regexes."""
    return set(re.findall(r"\d{3,}", text)), {m.lower() for m in re.findall(r"[\w\-]+\.(?:csv|txt|log|json)\b", text, re.I)}


def section_q() -> None:
    scenario("Q: seeded property corpus (300 combinations x 2 routes) — normalization adds nothing; grounding accepts no unsupported value")
    rng = random.Random(248)
    combos = []
    for _ in range(300):
        picks = rng.choices(EVIDENCE_POOL, k=rng.randint(2, 5))
        evidence = [
            Observation(PlanStep(tool, {"path": file, "i": n}), ok, speech, {"uncertain": True} if unc else {}, err)
            for n, (tool, speech, ok, unc, err, file) in enumerate(picks)
        ]
        answer = " ".join(rng.choices(ANSWER_POOL, k=rng.randint(1, 5)))
        combos.append((rng.choice(GOALS), evidence, answer))

    shape, foreign, exercised, changed = [], [], 0, 0
    for goal, evidence, answer in combos:
        allowed_nums, allowed_files = _concrete_values(_oracle_text(evidence, goal))
        for retrieval in (True, False):
            grounded = discovery.ground_answer(goal, answer, evidence, retrieval_claims=retrieval)
            final = discovery.normalize_answer(goal, grounded, evidence)
            changed += final != grounded
            spans = lambda t: [t[a:b] for a, b in discovery._sentence_spans(t)]  # noqa: E731
            it = iter(spans(grounded))
            if (not final.strip() or not discovery._is_subsequence(final, grounded) or not all(s in it for s in spans(final))
                    or final != discovery.finalize_answer(goal, answer, evidence, retrieval_claims=retrieval)):
                shape.append((goal, answer, grounded, final))
            nums, files = _concrete_values(final)
            if not nums <= allowed_nums or not files <= allowed_files:
                foreign.append((goal, answer, final))
            # is the corpus really probing the substring near-miss? (a claimed token the evidence only CONTAINS inside a longer one)
            claimed_nums, claimed_files = _concrete_values(answer)
            exercised += any(n not in allowed_nums and n in _oracle_text(evidence, goal) for n in claimed_nums) or any(
                f not in allowed_files and f in _oracle_text(evidence, goal) for f in claimed_files)
    check(f"Q1 (invariant 1): normalization only ever removes — over {2 * len(combos)} runs its output is always a non-empty "
          f"character subsequence of the grounded answer, built from its own sentences, and equals finalize_answer "
          f"({changed} runs were actually changed)", not shape and changed > 60, str(shape[:1]))
    check(f"Q2 (invariant 2): no 3+ digit number or filename in any final answer that the evidence and goal do not state as a "
          f"whole value — {exercised} runs claimed a value the evidence only CONTAINS inside a longer one (999 in 1999, "
          f"report.csv in myreport.csv)", not foreign and exercised > 20, f"{len(foreign)} foreign; exercised={exercised}; {foreign[:1]}")

    sample = combos[:60]
    once = [discovery.finalize_answer(g, a, e) for g, e, a in sample]
    twice = [discovery.finalize_answer(g, o, e) for (g, e, _), o in zip(sample, once)]
    check("Q3: finalize_answer is stable — running an already finalized answer through the pipeline again changes nothing "
          "(no nested fail-closed messages, no second 'Also:')", once == twice,
          str([(x, y) for x, y in zip(once, twice) if x != y][:1]))

    dup = "Created report.csv successfully. Created report.csv successfully."
    real_rebuild = discovery._rebuild
    discovery._rebuild = lambda text, spans, kept: real_rebuild(text, spans, kept) + " Also: 999 rows."  # a normalizer that ADDS
    try:
        sabotaged = discovery.finalize_answer("Create report.csv.", dup, N.CREATE_EV)
    finally:
        discovery._rebuild = real_rebuild
    check("Q4: invariant 1 is enforced, not assumed — a normalizer sabotaged to append text is refused; the pipeline hands back "
          "the grounded answer with no 999 in it", sabotaged == dup and not has_number(sabotaged, "999"), sabotaged)


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
    section_hi()
    section_j()
    section_k()
    section_l()
    section_m()
    section_n()
    section_o()
    section_p()
    section_q()
    return H.finish("Phase 24.8 — adversarial interaction hardening of the answer-reliability pipeline",
                    time.perf_counter() - t0, min_assertions=30, min_scenarios=12)


if __name__ == "__main__":
    sys.exit(main())
