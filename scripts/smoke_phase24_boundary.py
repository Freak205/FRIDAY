"""Phase 24.10 — the Phase 24 reliability BOUNDARY, validated end to end.

Phases 24.1-24.9 each pinned their own layer. This suite asks the cross-cutting questions and adds NO
heuristic: do the layers behave as ONE contract (see the "Phase 24 reliability contract" block in
`friday/intelligence/discovery.py`) across every route a final answer can take, with the kill switch,
in the right order, idempotently, and without ever introducing a claim of their own?

  A  kill-switch matrix        composer / planner `done` / deterministic summary  x  guard ON / OFF
  B  idempotence               ground(ground(x)) == ground(x); normalize(normalize(x)) == normalize(x);
                               finalize(finalize(x)) == finalize(x)  (hand cases + two seeded corpora)
  C  ordering                  compose -> overclaim protection -> ground -> normalize -> answer, each step
                               fed exactly the previous step's output; nothing runs after normalization
  D  cross-route equivalence   seven scenarios through the composer, the `done` route and (where the run
                               can end that way) the deterministic route, all judged by the SAME contract
                               predicates rather than by identical wording
  E  no-new-claims property    seeded corpora: the final answer is a character subsequence of the grounded
                               answer; every word is the answer's, the evidence's or a fixed fallback's;
                               no new filename / 3+ digit number; no completed-action claim naming an
                               object without a real outcome
  F  ordinary behaviour        conversational / no-evidence replies and untouched honest answers are not
                               made defensive by any of it

Entirely deterministic and offline: a scripted model behind the `llm.get_provider` seam and a recording fake
tool world (the 24.7 fixtures). No Ollama, no real side effect.
"""

from __future__ import annotations

import random
import re
import sys
import time

import phase21_common as H
import smoke_answer_normalization as N  # the 24.7 fixtures: R(), sg(), world(), drive(), ran(), reset()
import smoke_answer_object_grounding as OG  # the 24.9 seeded object corpus
import smoke_answer_reliability_adversarial as ADV  # the 24.8 seeded evidence / answer pools
from phase21_common import call, check, done, scenario

from friday.config import CFG  # noqa: E402
from friday.intelligence import discovery  # noqa: E402
from friday.orchestrator import Observation, PlanStep  # noqa: E402

R, sg, drive, ran = N.R, N.sg, N.drive, N.ran

DEL = {"path": "report.csv"}
NOTES = {"path": "notes.txt"}
FILE_RE = re.compile(r"[\w\-]+\.(?:csv|txt|log|json|md)\b", re.I)
NUM_RE = re.compile(r"\d{3,}")
WORD_RE = re.compile(r"[a-z0-9][a-z0-9.\-]*", re.I)
FIXED_WORDS = {w.rstrip(".-") for w in WORD_RE.findall((
    "I don't have enough confirmed evidence to answer that — the evidence gathered so far is insufficient, so I won't guess. "
    "Part of that isn't something the evidence I gathered actually shows, so I can't confirm it -- treating it as "
    "insufficient rather than guessing. What the evidence actually confirms: Also:").lower())}
DELETED = re.compile(r"\b(?:deleted|removed)\b", re.I)


def low(text: str) -> str:
    return (text or "").lower()


def words(text: str) -> set[str]:
    return {w.rstrip(".-") for w in WORD_RE.findall(low(text))}


def obs(tool: str, speech: str, *, ok: bool = True, args: dict | None = None) -> Observation:
    return Observation(PlanStep(tool, args or {}), ok, speech, {}, "")


# ================================================================================
# Routes. Each returns the FINAL answer string a user would hear.
# ================================================================================

GOAL_DEL = "Delete report.csv and tell me what happened."


def composer(goal: str, decisions: list[str], answer: str, subgoals: list, mode: str = "plain") -> str:
    return drive(goal, decisions, answer=answer, subgoals=subgoals, mode=mode)[0].summary


def planner_done(goal: str, decisions: list[str], mode: str = "plain") -> str:
    return drive(goal, decisions, mode=mode)[0].summary


def del_subs() -> list:
    return [sg("Delete report.csv"), sg("Tell me what happened", "answer")]


# ================================================================================
# A — the kill-switch matrix
# ================================================================================

UNSUPPORTED = "I deleted report.csv and notes.txt."
REDUNDANT = "Deleted report.csv successfully. Deleted report.csv successfully."
PROGRESS_THEN_RESULT = ["Searching for report.csv...", "Found report.csv."]


def _cell(route: str, guard: bool, answer: str) -> str:
    """One matrix cell: the delete tool really reports ONLY 'Deleted report.csv successfully.'"""
    N.reset()
    CFG.planner.answer_grounding_guard = guard
    N.world(delete=[R("Deleted report.csv successfully.")])
    if route == "composer":
        return composer(GOAL_DEL, [call("test.nq.delete", DEL)], answer, del_subs())
    return planner_done("Delete report.csv.", [call("test.nq.delete", DEL), done(answer)])


def _det(guard: bool) -> tuple[str, list]:
    N.reset()
    CFG.planner.answer_grounding_guard = guard
    N.world(find=[R(PROGRESS_THEN_RESULT[0]), R(PROGRESS_THEN_RESULT[1])])
    decisions = [call("test.nq.find", {"name": "report.csv"}),
                 call("test.nq.find", {"name": "report.csv", "where": "Downloads"}), done("unused")]
    res = drive("Find report.csv.", decisions, mode="look")[0]
    return res.summary, [o.speech for o in res.observations]


def section_a() -> None:
    scenario("A: kill-switch matrix — every final-answer route x CFG.planner.answer_grounding_guard ON / OFF")
    for route in ("composer", "done"):
        on_bad, on_red = _cell(route, True, UNSUPPORTED), _cell(route, True, REDUNDANT)
        off_bad, off_red = _cell(route, False, UNSUPPORTED), _cell(route, False, REDUNDANT)
        n = "1" if route == "composer" else "3"
        check(f"A{n}/{route} + guard ON: an unsupported claim (notes.txt 'deleted' on evidence for report.csv only) is "
              f"protected — no notes.txt claim reaches the user, the real deletion is still stated",
              "notes.txt" not in on_bad and "insufficient" in low(on_bad) and "Deleted report.csv successfully." in on_bad, on_bad)
        check(f"A{n}/{route} + guard ON: a redundant restatement is tidied to one (removal only)",
              on_red == "Deleted report.csv successfully.", on_red)
        n = "2" if route == "composer" else "4"
        check(f"A{n}/{route} + guard OFF: Phase 24 does not touch the answer at all — the unsupported claim and the "
              f"duplicate come back byte for byte (the switch is the ONLY difference)",
              off_bad == UNSUPPORTED and off_red == REDUNDANT, f"{off_bad!r} | {off_red!r}")

    on_text, speech = _det(True)
    off_text, _ = _det(False)
    check("A5/deterministic summary + guard ON: the status line a later result settled is dropped — the summary is "
          "the tools' own final words, nothing the evidence did not say",
          on_text == "Found report.csv." and on_text in speech, on_text)
    check("A6/deterministic summary + guard OFF: no tidying — every step's own words, in order",
          off_text == " ".join(PROGRESS_THEN_RESULT), off_text)
    # deterministic text is evidence text under BOTH settings: never a word the tools did not say
    check("A7: the deterministic route is evidence-only either way — every word of both summaries is a word a tool said",
          words(on_text) <= words(" ".join(speech)) and words(off_text) <= words(" ".join(speech)), f"{on_text!r} {off_text!r}")
    def all_routes():
        _cell("composer", False, UNSUPPORTED)
        _cell("done", False, UNSUPPORTED)
        _det(False)

    _, calls = _trace(all_routes)
    check("A8: with the guard OFF neither ground_answer nor normalize_answer is even CALLED on any of the three routes "
          "(only Phase 17's separate overclaim softening still runs, on the composer)",
          [c[0] for c in calls] == ["overclaim"], str([c[0] for c in calls]))
    _, calls_on = _trace(lambda: (_cell("composer", True, UNSUPPORTED), _cell("done", True, UNSUPPORTED), _det(True)))
    check("A9: ...and with it ON the same three runs call them (ground on both model routes, normalize on all three)",
          [c[0] for c in calls_on].count("ground") == 2 and [c[0] for c in calls_on].count("normalize") == 3, str([c[0] for c in calls_on]))
    N.reset()


# ================================================================================
# B — idempotence
# ================================================================================

HAND = [  # (goal, answer, evidence): one per way ground_answer can respond (accept / fail closed / 'Also:' / conflict)
    ("Delete report.csv.", "I deleted report.csv.", N.DELETE_EV),
    ("Delete report.csv.", "I deleted report.csv and notes.txt.", N.DELETE_EV),
    ("Delete report.csv.", "Attempting to delete report.csv. Deleted report.csv successfully. Deleted report.csv successfully.", N.DELETE_EV),
    (N.FIND_ROWS, "Found report.csv.", N.FIND_EV),
    (N.FIND_ROWS, "Found report.csv. It has 999 rows.", N.FIND_EV),
    ("Delete report.csv.", "I deleted report.csv.", [obs("files.delete", "Deleted report.csv successfully.", args=DEL),
                                                     obs("files.check", "report.csv still exists.", args=DEL)]),
    ("Delete report.csv.", "Deleted.", [obs("files.delete", "Delete failed: permission denied.", ok=False, args=DEL)]),
    ("Tell me a joke.", "Why did the chicken cross the road?", []),
]


def _corpus(seed: int, n: int) -> list[tuple[str, list[Observation], str]]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        picks = rng.choices(ADV.EVIDENCE_POOL, k=rng.randint(2, 5))
        evidence = [Observation(PlanStep(t, {"path": f, "i": i}), ok, sp, {"uncertain": True} if unc else {}, err)
                    for i, (t, sp, ok, unc, err, f) in enumerate(picks)]
        answer = " ".join(rng.choices(ADV.ANSWER_POOL, k=rng.randint(1, 5)))
        out.append((rng.choice(ADV.GOALS), evidence, answer))
    return out


def _object_corpus() -> list[tuple[str, list[Observation], str, bool]]:
    out = []
    for names, states, phrasing, oxford in OG.corpus():
        answer = OG.PHRASINGS[phrasing](names, oxford)
        goal = "Delete " + OG._join(names, False) + "."
        evidence = [e for n in names if (e := OG.evidence_for(states[n], n)) is not None]
        out.append((goal, evidence, answer, all(states[n] == "supported" for n in names)))
    return out


def section_b() -> None:
    scenario("B: idempotence — a second pass over an already-processed answer changes nothing")
    bad_g, bad_n, bad_f = [], [], []
    every = [(g, e, a) for g, a, e in HAND] + _corpus(2410, 300) + [(g, e, a) for g, e, a, _ in _object_corpus()]
    moved = 0
    for goal, evidence, answer in every:
        for rc in (True, False):
            g1 = discovery.ground_answer(goal, answer, evidence, retrieval_claims=rc)
            g2 = discovery.ground_answer(goal, g1, evidence, retrieval_claims=rc)
            moved += g1 != answer
            if g2 != g1:
                bad_g.append((goal, answer, g1, g2))
            n1 = discovery.normalize_answer(goal, g1, evidence)
            n2 = discovery.normalize_answer(goal, n1, evidence)
            if n2 != n1:
                bad_n.append((goal, g1, n1, n2))
            f1 = discovery.finalize_answer(goal, answer, evidence, retrieval_claims=rc)
            f2 = discovery.finalize_answer(goal, f1, evidence, retrieval_claims=rc)
            if f2 != f1 or f1 != n1:
                bad_f.append((goal, answer, f1, f2))
    total = 2 * len(every)
    check(f"B1: ground_answer is idempotent — over {total} runs (hand cases + 300 evidence-pool draws + 400 object draws, both "
          f"routes; {moved} runs were actually changed by the first pass) a second pass adds nothing", not bad_g and moved > 300, str(bad_g[:1]))
    check(f"B2: normalize_answer is idempotent — normalize(normalize(x)) == normalize(x) over the same {total} runs", not bad_n, str(bad_n[:1]))
    check("B3: finalize_answer is idempotent, and equals normalize(ground(x)) — the pipeline can be re-applied safely", not bad_f, str(bad_f[:1]))

    # the real routes: an answer that came back from run_goal is a fixed point of the pipeline
    N.reset()
    N.world(delete=[R("Deleted report.csv successfully.")])
    ans = composer(GOAL_DEL, [call("test.nq.delete", DEL)], REDUNDANT, del_subs())
    obs_ = [obs("test.nq.delete", "Deleted report.csv successfully.", args=DEL)]
    check("B4: the string run_goal returned is a fixed point of finalize_answer",
          discovery.finalize_answer(GOAL_DEL, ans, obs_) == ans, ans)
    # regression pin (found by B1 at 24.10): restating a result can change what the checks see, so ONE look was not
    # enough -- an appended failure gave the neutral answer a negative polarity and only then exposed a success-vs-failure
    # conflict; a clause with two unmet numbers reported one per look. Both used to need a second pass.
    conflict_ev = [obs("files.read", "Permission denied opening notes.txt.", ok=False, args=NOTES),
                   obs("files.remove", "Deleted report.csv successfully.", args=DEL),
                   obs("files.check", "report.csv still exists.", args=DEL)]
    g = discovery.ground_answer("Find report.csv and notes.txt.", "report.csv contains 42 rows.", conflict_ev)
    rows_ev = [obs("files.rows", "report.csv has 1999 rows.", args=DEL), obs("files.count", "report.csv contains 42 rows.", args={"path": "r.csv"})]
    g2 = discovery.ground_answer("How many rows does report.csv have?", "Retrying delete...", rows_ev)
    check("B5 (regression): the 'Also:' completion is complete in ONE pass — the appended failure AND the conflicting success, "
          "both numbers of a quantity clause — and a second pass is a no-op",
          "Permission denied opening notes.txt." in g and "Deleted report.csv successfully." in g and "1999" in g2 and "42" in g2
          and discovery.ground_answer("Find report.csv and notes.txt.", g, conflict_ev) == g
          and discovery.ground_answer("How many rows does report.csv have?", g2, rows_ev) == g2, f"{g!r} | {g2!r}")
    N.reset()


# ================================================================================
# C — ordering
# ================================================================================


def _trace(fn):
    """Run `fn` with all three Phase 24 steps spied; returns (result, [(step, input, output)])."""
    trace: list[tuple[str, str, str]] = []
    real = (discovery.guard_against_overclaiming, discovery.ground_answer, discovery.normalize_answer)

    def spy(name, f, pos):
        def inner(*a, **kw):
            out = f(*a, **kw)
            trace.append((name, a[pos], out))
            return out
        return inner

    discovery.guard_against_overclaiming = spy("overclaim", real[0], 0)
    discovery.ground_answer = spy("ground", real[1], 1)
    discovery.normalize_answer = spy("normalize", real[2], 1)
    try:
        return fn(), trace
    finally:
        discovery.guard_against_overclaiming, discovery.ground_answer, discovery.normalize_answer = real


def _chained(trace: list, names: list[str], raw: str, final: str) -> bool:
    if [t[0] for t in trace] != names or trace[0][1] != raw:
        return False
    return all(trace[i][1] == trace[i - 1][2] for i in range(1, len(trace))) and trace[-1][2] == final


def section_c() -> None:
    scenario("C: ordering — compose -> overclaim protection -> ground -> normalize -> final; nothing after, nothing before")

    def comp():
        N.reset()
        N.world(delete=[R("Deleted report.csv successfully.")])
        return composer(GOAL_DEL, [call("test.nq.delete", DEL)], REDUNDANT, del_subs())

    final, tr = _trace(comp)
    check("C1: composer route — overclaim, ground, normalize run in exactly that order, each fed the previous step's exact "
          "output, and the answer the user gets IS normalize's output",
          _chained(tr, ["overclaim", "ground", "normalize"], REDUNDANT, final) and final == "Deleted report.csv successfully.", str(tr))

    def dn():
        N.reset()
        N.world(delete=[R("Deleted report.csv successfully.")])
        return planner_done("Delete report.csv.", [call("test.nq.delete", DEL), done(REDUNDANT)])

    final, tr = _trace(dn)
    check("C2: planner `done` route — ground then normalize, chained; no step out of order",
          _chained(tr, ["ground", "normalize"], REDUNDANT, final) and final == "Deleted report.csv successfully.", str(tr))

    def det():
        N.reset()
        N.world(find=[R(PROGRESS_THEN_RESULT[0]), R(PROGRESS_THEN_RESULT[1])])
        d = [call("test.nq.find", {"name": "report.csv"}), call("test.nq.find", {"name": "report.csv", "where": "Downloads"}), done("x")]
        return drive("Find report.csv.", d, mode="look")[0].summary

    final, tr = _trace(det)
    check("C3: deterministic route — the summary is evidence text, so it is only ever normalized (never 'grounded' against "
          "itself), and the summary is normalize's output",
          [t[0] for t in tr] == ["normalize"] and tr[0][2] == final == "Found report.csv.", str(tr))

    # a rejected claim is not brought back by the step after it
    final, tr = _trace(lambda: _cell("composer", True, UNSUPPORTED))
    ground_out = next(o for n, _, o in tr if n == "ground")
    check("C4: nothing after grounding reintroduces rejected text — the fail-closed answer passes through normalize with "
          "no 'notes.txt' and no 'deleted ... notes.txt' anywhere on the way out",
          "notes.txt" not in ground_out and "notes.txt" not in final and discovery._is_subsequence(final, ground_out), final)

    # ... and the structure that guarantees it: the source of both routes ends in finalize_answer, nothing after it
    import inspect

    from friday import orchestrator

    src = inspect.getsource(orchestrator._ground_done_summary).rstrip().splitlines()[-1].strip()
    comp_src = inspect.getsource(orchestrator.Orchestrator._answer_from_evidence)
    tail = comp_src[comp_src.index("discovery.finalize_answer"):]
    check("C5: structurally — both routes end in finalize_answer with nothing after it but `return answer`; no caller runs "
          "normalize_answer on its own ahead of ground_answer",
          src.startswith("return discovery.finalize_answer(") and tail.split("\n", 1)[1].strip().startswith("return answer")
          and comp_src.index("guard_against_overclaiming") < comp_src.index("discovery.finalize_answer"), f"{src!r}")
    N.reset()


# ================================================================================
# D — cross-route equivalence: one contract, three routes
# ================================================================================
#
# Each scenario is judged by contract predicates, never by wording:
#   claim      a completed-action claim ("deleted") in the final answer names only objects with a real outcome
#   evidence   the final answer states nothing concrete (file / 3+ digit number) the evidence did not
#   reported   the real result the goal asked for is present (failure, missing part, conflict side ...)


def _facts(text: str) -> tuple[set[str], set[str]]:
    return {m.lower() for m in FILE_RE.findall(text)}, set(NUM_RE.findall(text))


def _judge(name: str, final: str, evidence_text: str, must: list[str], must_not: list[str]) -> list[str]:
    bad = []
    files, nums = _facts(final)
    ev_files, ev_nums = _facts(evidence_text)
    if not files <= ev_files | {"report.csv"} or not nums <= ev_nums:
        bad.append(f"foreign fact {sorted((files - ev_files) | (nums - ev_nums))}")
    bad += [f"missing {m!r}" for m in must if m not in low(final)]
    bad += [f"forbidden {m!r}" for m in must_not if re.search(m, low(final))]
    return [f"{name}: {b}" for b in bad]


def _route_finals(goal: str, decisions: list[str], answer: str, subs, world_kw: dict, det_mode: str | None = None) -> dict[str, str]:
    """The same scenario through each route (fresh scripted results each time: a queue is consumed as it is read). The
    deterministic route is only reported when the run really ENDED there (the planner never got to say `done`)."""
    def fresh() -> dict:
        N.reset()
        N.world(**{k: list(v) for k, v in world_kw.items()})
        return world_kw

    fresh()
    out = {"composer": composer(goal, decisions + [done(answer)], answer, subs or del_subs())}
    fresh()
    out["done"] = planner_done(goal, decisions + [done(answer)])
    if det_mode:
        fresh()
        text = planner_done(goal, decisions + [done("planner-said-done")], mode=det_mode)
        if "planner-said-done" not in text:
            out["deterministic"] = text
    return out


def section_d() -> None:
    scenario("D: cross-route equivalence — composer / planner `done` / deterministic, the same contract on each")
    problems: list[str] = []
    routes_seen: dict[str, int] = {}

    def run(label: str, finals: dict[str, str], ev_text: str, must, must_not, *, allow: dict[str, tuple] | None = None) -> None:
        for route, text in finals.items():
            routes_seen[route] = routes_seen.get(route, 0) + 1
            m, mn = (allow or {}).get(route, (must, must_not))
            problems.extend(_judge(f"{label}/{route}", text, ev_text, m, mn))

    # 1 supported action --------------------------------------------------------------------------------
    f1 = _route_finals("Delete report.csv.", [call("test.nq.delete", DEL)], "I deleted report.csv.", None,
                       {"delete": [R("Deleted report.csv successfully.")]})
    f1["composer"] = _route_finals(GOAL_DEL, [call("test.nq.delete", DEL)], "I deleted report.csv.", del_subs(),
                                   {"delete": [R("Deleted report.csv successfully.")]})["composer"]
    run("1 supported", f1, "Deleted report.csv successfully.", ["deleted", "report.csv"], ["insufficient", "couldn't|could not|failed"])
    check("D1: supported action — every route states the deletion of report.csv, unchanged, without fail-closing",
          all(t == "I deleted report.csv." for t in f1.values()), str(f1))

    # 2 unsupported action (the tool only ATTEMPTED) ------------------------------------------------------
    d2 = [call("test.nq.delete", DEL)]
    f2 = _route_finals("Delete report.csv.", d2, "I deleted report.csv.", None, {"delete": [R("Attempting to delete report.csv.")]}, det_mode="disc")
    f2["composer"] = _route_finals(GOAL_DEL, d2, "I deleted report.csv.", del_subs(), {"delete": [R("Attempting to delete report.csv.")]})["composer"]
    run("2 unsupported", f2, "Attempting to delete report.csv.", [], [r"\bi deleted\b", r"^deleted"])
    check("D2: unsupported action (attempt only) — no route tells the user it was deleted",
          not any(re.search(r"\bi deleted\b|(?<!attempting to )\bdeleted report", low(t)) for t in f2.values()), str(f2))

    # 3 failed action -----------------------------------------------------------------------------------
    w3 = {"delete": [R("Delete failed: permission denied.", ok=False)]}
    f3 = _route_finals("Delete report.csv.", d2, "I deleted report.csv.", None, w3, det_mode="disc")
    f3["composer"] = _route_finals(GOAL_DEL, d2, "I deleted report.csv.", del_subs(), w3)["composer"]
    run("3 failed", f3, "Delete failed: permission denied.", ["permission denied"], [r"\bi deleted\b"])
    check("D3: failed action — every route carries the real failure and none claims success",
          all("permission denied" in low(t) and "i deleted" not in low(t) for t in f3.values()), str(f3))

    # 4 multi-object ------------------------------------------------------------------------------------
    goal4 = "Delete report.csv and notes.txt."
    d4 = [call("test.nq.delete", DEL), call("test.nq.delete", NOTES)]
    w4 = {"delete": [R("Deleted report.csv successfully."), R("Delete failed: permission denied for notes.txt.", ok=False)]}
    subs4 = [sg("Delete report.csv"), sg("Delete notes.txt"), sg("Tell me what happened", "answer")]
    f4 = _route_finals(goal4, d4, "I deleted report.csv and notes.txt.", subs4, w4, det_mode="disc")
    run("4 multi", f4, "Deleted report.csv successfully. Delete failed: permission denied for notes.txt.",
        ["permission denied"], [r"deleted report\.csv and notes\.txt", r"deleted notes\.txt"])
    check("D4: multi-object — no route claims notes.txt was deleted (its own result was a refusal); the refusal is reported",
          all(not re.search(r"deleted (?:report\.csv and )?notes\.txt", low(t)) and "permission denied" in low(t) for t in f4.values()), str(f4))

    GOAL5 = "Find report.csv and tell me how many rows it has."
    # 5 partial multi-clause result ---------------------------------------------------------------------
    w5 = {"find": [R("Found report.csv.")], "rows": [R("report.csv contains 42 rows.")]}
    d5 = [call("test.nq.find", {"name": "report.csv"}), call("test.nq.rows", DEL)]
    subs5 = [sg("Find report.csv"), sg("Count the rows in report.csv"), sg("Tell me the row count", "answer")]
    f5 = _route_finals(GOAL5, d5, "Found report.csv.", subs5, w5, det_mode="disc")
    run("5 partial", f5, "Found report.csv. report.csv contains 42 rows.", ["42"], [])
    check("D5: partial multi-clause — an answer that omits the requested count still ends up stating 42 on every route",
          all("42" in t for t in f5.values()), str(f5))

    # 6 progress -> final result --------------------------------------------------------------------------
    w6 = {"begin": [R("Attempting to delete report.csv.")], "finish": [R("Deleted report.csv successfully.")]}
    d6 = [call("test.nq.begin", DEL), call("test.nq.finish", DEL)]
    subs6 = [sg("Start deleting report.csv"), sg("Finish deleting report.csv"), sg("Tell me what happened", "answer")]
    f6 = _route_finals("Delete report.csv.", d6, "Attempting to delete report.csv. I deleted report.csv.", subs6, w6, det_mode="disc")
    run("6 progress", f6, "Attempting to delete report.csv. Deleted report.csv successfully.", ["deleted"], [r"^attempting"])
    check("D6: progress -> final — the final result is stated on every route and no route ends on / leads with the status line",
          all(DELETED.search(t) and not low(t).startswith("attempting") for t in f6.values()), str(f6))

    # 7 conflicting results -----------------------------------------------------------------------------
    w7 = {"delete": [R("Deleted report.csv successfully.")], "read": [R("report.csv still exists.")]}
    d7 = [call("test.nq.delete", DEL), call("test.nq.read", DEL)]
    subs7 = [sg("Delete report.csv"), sg("Check report.csv"), sg("Tell me what happened", "answer")]
    f7 = _route_finals("Delete report.csv and confirm it is gone.", d7, "I deleted report.csv.", subs7, w7, det_mode="disc")
    run("7 conflict", f7, "Deleted report.csv successfully. report.csv still exists.", ["still exists"], [])
    check("D7: conflicting results — no route silently picks the deletion: 'still exists' is reported on every route",
          all("still exists" in low(t) for t in f7.values()), str(f7))

    check("D8: every scenario satisfied the shared predicates (no foreign file/number, required result present, forbidden claim absent)",
          not problems, "; ".join(problems[:3]))
    check("D9: coverage — the composer and `done` routes ran for all 7 scenarios; the deterministic route is judged wherever the run "
          "really ended there without the planner (the failed run and the fully-covered multi-clause run; in the others the "
          "sufficiency stop does not fire, by design, and the run reaches the `done` route instead)",
          routes_seen["composer"] == 7 and routes_seen["done"] == 7 and routes_seen.get("deterministic", 0) >= 2, str(routes_seen))
    N.reset()


# ================================================================================
# E — the no-new-claims property
# ================================================================================


def _oracle(evidence: list[Observation], goal: str) -> str:
    return ADV._oracle_text(evidence, goal)


def section_e() -> None:
    scenario("E: no-new-claims property — seeded corpora; the pipeline only ever removes, restates evidence, or adds a fixed fallback")
    sub_bad, word_bad, fact_bad, exercised, changed, fallbacks = [], [], [], 0, 0, 0
    for goal, evidence, answer in _corpus(2411, 300):
        oracle = _oracle(evidence, goal)
        allowed_words = words(answer) | words(oracle) | FIXED_WORDS
        for rc in (True, False):
            grounded = discovery.ground_answer(goal, answer, evidence, retrieval_claims=rc)
            final = discovery.normalize_answer(goal, grounded, evidence)
            changed += final != answer
            fallbacks += grounded != answer
            if not discovery._is_subsequence(final, grounded):
                sub_bad.append((goal, grounded, final))
            if not words(final) <= allowed_words:
                word_bad.append((goal, answer, final, sorted(words(final) - allowed_words)))
            files, nums = _facts(final)
            o_files, o_nums = _facts(oracle)
            if not files <= o_files or not nums <= o_nums:
                fact_bad.append((goal, answer, final))
            a_files, a_nums = _facts(answer)
            exercised += bool((a_files - o_files) or (a_nums - o_nums))
    check(f"E1: normalization is a subsequence operation — over 600 runs ({changed} changed the answer, {fallbacks} took a "
          f"grounding fallback) the final answer is always a character subsequence of the grounded answer", not sub_bad, str(sub_bad[:1]))
    check("E2: no new words — every word of every final answer is the composer's, the evidence's, or the fixed fallback's "
          "(the pipeline never authors content)", not word_bad, str(word_bad[:1]))
    check(f"E3: no new filename and no new 3+ digit number — none appears in a final answer that the evidence and goal do not "
          f"state (the corpus tried to slip one in on {exercised} runs)", not fact_bad and exercised > 60, f"{exercised} {fact_bad[:1]}")

    bad_claim, accepted, rejected = [], 0, 0
    for goal, evidence, answer, all_real in _object_corpus():
        for rc in (True, False):
            final = discovery.finalize_answer(goal, answer, evidence, retrieval_claims=rc)
            kept_claim = answer in final
            accepted += kept_claim
            rejected += not kept_claim
            if kept_claim != all_real:
                bad_claim.append((goal, answer, final, all_real))
            if not all_real and any(DELETED.search(s) and any(n in s for n in FILE_RE.findall(goal) if n not in low(evidence_txt(evidence)))
                                    for s in re.split(r"(?<=[.!?])\s+", final)):
                bad_claim.append((goal, answer, final, "object-claim survived"))
    check(f"E4: no completed-action claim survives for an object without a real outcome — over 800 object-corpus runs "
          f"({accepted} accepted, {rejected} rejected) the claim is kept iff EVERY named file has a real deletion result, "
          f"through the full pipeline (grounding + normalization)", not bad_claim and accepted > 40 and rejected > 400, str(bad_claim[:1]))
    N.reset()


def evidence_txt(evidence: list[Observation]) -> str:
    return " ".join(o.speech for o in evidence if o.ok and not (o.data or {}).get("uncertain"))


# ================================================================================
# F — ordinary, non-Phase-24 behaviour is not made defensive
# ================================================================================


def section_f() -> None:
    scenario("F: ordinary behaviour is untouched — no evidence means no grounding, honest answers pass verbatim")
    N.reset()
    joke = "Why did the chicken cross the road? To get to the other side."
    from friday import orchestrator

    check("F1: a reply over ZERO observations (plain conversation) is returned verbatim by the `done` route's entry point — no "
          "evidence to ground against, so no 'insufficient' message",
          orchestrator._ground_done_summary("Tell me a joke.", joke, []) == joke)

    honest = [
        ("Find report.csv and tell me how many rows it has.", "I found report.csv and it has 42 rows.", N.FIND_EV),
        ("Delete report.csv.", "I couldn't delete report.csv: permission denied.",
         [obs("files.delete", "Delete failed: permission denied.", ok=False, args=DEL)]),
        ("What time is it?", "It is 3:45 PM.", [obs("system.time", "It is 3:45 PM.")]),
        ("Delete report.csv and notes.txt.", "I deleted report.csv and notes.txt.", [OG.dele("report.csv"), OG.dele("notes.txt")]),
    ]
    got = [(a, discovery.finalize_answer(g, a, e, retrieval_claims=rc)) for g, a, e in honest for rc in (True, False)]
    check("F2: honest, fully supported answers (a count, a reported failure, a time, a two-file deletion) come back verbatim "
          "on both routes", all(a == f for a, f in got), str([x for x in got if x[0] != x[1]][:1]))

    N.world(find=[R("Found report.csv.")])
    res = drive("Find report.csv.", [call("test.nq.find", {"name": "report.csv"}), done("I found report.csv.")])[0]
    check("F3: a plain run_goal that finds a file and says done reports it verbatim (ok, completed)",
          res.ok and res.stopped == "completed" and res.summary == "I found report.csv.", f"{res.stopped} {res.summary!r}")
    N.reset()


def main() -> int:
    t0 = time.perf_counter()
    N.reset()
    section_a()
    section_b()
    section_c()
    section_d()
    section_e()
    section_f()
    return H.finish("Phase 24.10 — Phase 24 reliability boundary and stability validation",
                    time.perf_counter() - t0, min_assertions=30, min_scenarios=6)


if __name__ == "__main__":
    sys.exit(main())
