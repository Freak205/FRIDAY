"""Phase 71 -- the user-facing goal status after a restart, through the real routing path.

Phase 70 proved that a separate OS process reconstructs a goal exactly as the process that ran it left it -- but it called the functions
BEHIND `meta.goal_status`. This suite asks the question the way a user does, in a fresh process:

  process A   (Phase 70's, reused unchanged) runs one scenario through the real `plan.run` against a throwaway database and EXITS HARD.
  process S   a fresh process, same database. It types each of the status skill's own example sentences into the real `Session.handle`
              (BRAIN match -> direct-route gate -> `run_verified` -> `EXECUTOR.run("meta.goal_status")`, permission layer and audit row
              included), then calls the Executor directly. It records what answered, what ran, and exactly what changed in the database.
  process T   another fresh process, started after S: the Executor only. The status rows S left in the audit log must change nothing
              about the goal or the answer.
  process C/D (once, last) the boot hook (`audit.start_session`) followed by routed questions, then a plain read-only process.

Scenarios: Phase 70's six (verified; a failed read-back that stays failed; one recovered by a later verified step; an unresolved failure; an
unknown outcome; a process killed inside a tool) plus a late result (a step that outlived its timeout and finished afterwards: the one case
where the stored account and the audit log differ). Every answer is compared with what A saw and with hand-written meaning (Phase 70's
`semantics`, reused). Side effects are ruled
out several ways, not one: no fixture tool body ran (`calls.log`, in-process invocation lists), the only skill that went through the Executor
was `meta.goal_status` (every other skill is hard-denied for these processes, so anything else would also leave a `denied` audit row), every
audit row that existed before the question is byte-identical afterwards, the new rows are exactly the status rows (tier L0, auto, no goal
attached), every other table is untouched, and the goal's stages, step states, read-back verdicts, recovery flags and its own ledger of audit
rows are unchanged.

Not covered, and not claimed: the real embedding model (BRAIN is driven by a bag-of-words stand-in here, so only the skill's own example
sentences are shown to route, not paraphrases), the GUI / voice / daemon front ends (they hand text to the same `Session.handle`, but are
not started), a graceful shutdown, and an external kill. The parent imports nothing from `friday`; every child redirects its paths into a
temp folder first and watches `data/` with an audit hook (Phase 70's harness).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import smoke_process_restart as P70  # noqa: E402   (standard library only: `friday` is imported inside the child functions)

SCRIPT = Path(__file__).resolve()
STATUS = "meta.goal_status"

# One scenario beyond Phase 70's: a step that outlives its timeout (stored `unknown`), then FINISHES while process A is still alive, so its
# real outcome lands in the audit log after the goal's progress was saved. That is the one case where the stored account and the audit
# log differ, which is what makes "asking changes nothing" observable for the stored evidence (only the boot hook may bring it up to date).
# Registered before `killed_mid_step`, which must stay the newest goal for the boot-hook part.
_LATE = {"step_timeout": 1.0, "slow_s": 2.5, "linger_s": 3.0, "decisions": [(P70.SL, {"name": "l"}, 0), (P70.S, {"name": "l"}, 1)]}
_ordered = {}
for _k, _v in P70.SPECS.items():
    if _k == "killed_mid_step":
        _ordered["late_result"] = _LATE
    _ordered[_k] = _v
P70.SPECS.clear()
P70.SPECS.update(_ordered)
RESTART_DIFFERS = {"late_result"}      # the live report predates the late result, so a restart rightly differs from it
SETTLED_AT_BOOT = {"late_result", "killed_mid_step"}   # goals whose stored account the boot hook (and only it) brings up to date

OTHER_TABLES =("goals", "undo", "episodes", "jobs", "job_runs", "memory", "corrections", "phrasings", "kb_documents", "kb_chunks")


# =============================================================================================================================
# child side
# =============================================================================================================================
def _snapshot(upto: int) -> dict:
    """Every table a hidden re-execution would touch, as (row count, hash); the audit log only up to `upto` (the rows that existed before)."""
    import hashlib

    from friday import store

    c = store.conn()

    def sha(rows: list) -> str:
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    out = {}
    for table in OTHER_TABLES:
        rows = [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
        out[table] = {"n": len(rows), "sha": sha(rows)}
    rows = [tuple(r) for r in c.execute("SELECT * FROM audit WHERE id<=? ORDER BY id", (upto,)).fetchall()]
    out["audit_before"] = {"n": len(rows), "sha": sha(rows)}
    return out


def _new_audit(after_id: int) -> list[dict]:
    from friday import store

    cols = "id, actor, skill, tier, args, decision, ok, error, goal_id"
    return [dict(r) for r in store.conn().execute(f"SELECT {cols} FROM audit WHERE id>? ORDER BY id", (after_id,)).fetchall()]


def child_status(root: Path, tag: str, mode: str, boot: bool, ids: list[str]) -> None:
    """A fresh process asks for the status of the latest goal. mode 'routed' = every example sentence through `Session.handle`, then the
    Executor directly; mode 'direct' = the Executor only. boot = run FRIDAY's boot hook first (the real order at startup)."""
    P70._bootstrap(root, tag)
    import asyncio

    from friday import audit, steps, store
    from friday.brain.engine import BRAIN
    from friday.config import CFG
    from friday.intelligence import goals as goals_mod
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY
    from friday.session import Session

    REGISTRY.discover()
    fx = P70.Fixtures(root, tag, None)              # registered (a restarted FRIDAY knows its tools), never run
    CFG.desktop_observer.enabled = False
    # Anything the router might wrongly send elsewhere -- a real skill, a fixture, the planner -- is refused, not run. The status skill
    # keeps its real tier and policy (L0, auto): nothing is granted to it.
    CFG.permissions.overrides = {**CFG.permissions.overrides, **{s.name: "deny" for s in REGISTRY.all() if s.name != STATUS}}
    examples = list(REGISTRY.get(STATUS).examples)

    calls_log = root / "calls.log"
    calls_before = calls_log.read_bytes() if calls_log.exists() else b""
    max_id = store.conn().execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()[0]
    latest = goals_mod.recent(1)[0].id
    gids = list(dict.fromkeys([latest, *ids]))

    def views() -> dict:
        return {g: P70._goal_view(g) for g in gids}

    def ledger() -> dict:
        return {g: [a for s in steps.ledger_for_goal(g) for a in s.audit] for g in gids}

    views0 = views()
    if boot:
        audit.start_session("cli")                  # reconcile dead owners, settle goals left running, refresh stored progress
    max_id_after_boot = store.conn().execute("SELECT COALESCE(MAX(id), 0) FROM audit").fetchone()[0]
    snap0, views1, ledger1 = _snapshot(max_id_after_boot), views(), ledger()   # the baseline the QUESTION is measured against

    ran: list[str] = []
    real_run = EXECUTOR.run

    async def spy(name, *a, **k):  # noqa: ANN001
        ran.append(name)
        return await real_run(name, *a, **k)

    EXECUTOR.run = spy  # type: ignore[method-assign]
    asks: list[dict] = []

    async def go() -> dict:
        sess = Session()
        if mode == "routed":
            for text in examples:
                n = len(ran)
                r = await sess.handle(text, actor="text")
                asks.append({"text": text, "skill": sess.last_skill, "ran": ran[n:], "ok": r.ok, "speech": r.speech,
                             "has_report": bool((r.data or {}).get("report"))})
        n = len(ran)
        r = await EXECUTOR.run(STATUS, {}, actor="text")
        return {"ran": ran[n:], "ok": r.ok, "speech": r.speech, "has_report": bool((r.data or {}).get("report"))}

    try:
        direct = asyncio.run(go())
    finally:
        EXECUTOR.run = real_run  # type: ignore[method-assign]

    snap1, views2, ledger2 = _snapshot(max_id_after_boot), views(), ledger()
    unrelated = BRAIN.understand("what time is it")     # a control: the router is selective (pure, runs nothing)
    out = {
        "latest": latest, "examples": examples, "asks": asks, "direct": direct, "pure_speech": steps.describe_goal()[0],
        "ran_all": ran, "invocations": list(fx.invocations),
        "calls_unchanged": calls_before == (calls_log.read_bytes() if calls_log.exists() else b""),
        "snap_before": snap0, "snap_after": snap1, "views_pre_boot": views0, "views_before": views1, "views_after": views2,
        "ledger_before": ledger1, "ledger_after": ledger2, "new_audit": _new_audit(max_id_after_boot),
        "unrelated_skill": unrelated.skill, "unrelated_action": unrelated.action.value,
    }
    (root / f"out_{tag}.json").write_text(json.dumps(out), encoding="utf-8")


# =============================================================================================================================
# parent side (imports nothing from friday)
# =============================================================================================================================
RESULTS: list[tuple[bool, str]] = []
SCENARIO_TITLES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((bool(cond), label))
    suffix = f" -- {detail}" if detail and not cond else ""
    print(f"  {'OK  ' if cond else 'MISS'} {label}{suffix}", flush=True)
    return bool(cond)


def scenario(title: str) -> None:
    SCENARIO_TITLES.append(title)
    print(f"\n--- {title} ---\n", flush=True)


P70.check = check          # Phase 70's hand-written meaning checks (`semantics`) report into this suite


def spawn(root: Path, *args: str, timeout: int = 170) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_ALLOW_REAL_DB", "FRIDAY_DB")}
    return subprocess.run([sys.executable, str(SCRIPT), "--child", *args, "--root", str(root)], capture_output=True, text=True,
                          timeout=timeout, env=env, encoding="utf-8", errors="replace")


def status_proc(root: Path, tag: str, mode: str, ids: list[str], boot: bool = False) -> tuple[subprocess.CompletedProcess, dict | None]:
    args = ["status", "--tag", tag, "--mode", mode, "--ids", json.dumps(ids)] + (["--boot"] if boot else [])
    p = spawn(root, *args)
    return p, (P70.load(root, f"out_{tag}.json") if p.returncode == 0 else None)


def routing_checks(label: str, s: dict) -> None:
    asks = s["asks"]
    check(f"{label} each of the skill's own example sentences ({len(asks)}), typed to a fresh process's Session, is routed to meta.goal_status",
          len(asks) >= 5 and all(a["skill"] == STATUS for a in asks), str([(a["text"], a["skill"]) for a in asks]))
    check(f"{label} ...and each went through the Executor as meta.goal_status and as nothing else (no planner, no other skill), and answered ok",
          all(a["ran"] == [STATUS] and a["ok"] and a["has_report"] for a in asks) and s["direct"]["ran"] == [STATUS] and s["direct"]["ok"],
          str([(a["text"], a["ran"], a["ok"]) for a in asks]))
    check(f"{label} the same answer comes back from every routed sentence, from the Executor called directly, and from describe_goal itself",
          len({a["speech"] for a in asks} | {s["direct"]["speech"], s["pure_speech"]}) == 1, str({a["speech"] for a in asks}))


def effect_checks(label: str, s: dict, calls_ref: bytes, root: Path, expected_rows: int) -> None:
    check(f"{label} nothing was executed, retried or resumed: no fixture tool body ran in this process, calls.log is byte-identical, and the Executor "
          f"saw only meta.goal_status ({len(s['ran_all'])} calls)",
          s["invocations"] == [] and s["calls_unchanged"] and (root / "calls.log").read_bytes() == calls_ref
          and s["ran_all"] == [STATUS] * expected_rows, f"{s['invocations']} {s['ran_all']}")
    rows = s["new_audit"]
    check(f"{label} the question's own audit rows are told apart from the goal's: exactly {expected_rows}, all meta.goal_status, L0, auto, actor text, "
          "completed ok, args empty, attached to NO goal -- no plan.run, no tool, nothing denied",
          len(rows) == expected_rows and all(r["skill"] == STATUS and r["tier"] == "L0" and r["decision"] == "auto" and r["actor"] == "text"
                                              and r["ok"] == 1 and r["goal_id"] is None and r["args"] in ("{}", "") and not r["error"] for r in rows),
          json.dumps(rows)[:600])
    b, a = s["snap_before"], s["snap_after"]
    check(f"{label} every audit row that existed before the question is byte-identical afterwards, and so is every other table "
          "(goals, undo journal, episodes, jobs, memory, corrections, phrasings, knowledge)",
          b == a, str({k: (b[k], a[k]) for k in b if b[k] != a[k]}))
    check(f"{label} the goal's own ledger of audit rows is unchanged: no status row became a step of the goal",
          s["ledger_before"] == s["ledger_after"] and all(r["id"] not in sum(s["ledger_after"].values(), []) for r in rows),
          f"{s['ledger_before']} {s['ledger_after']}")
    check(f"{label} every goal's status, stages, step states, read-back verdicts, recovery flags and stored account are what they were before the question",
          s["views_before"] == s["views_after"], str({g: (s["views_before"][g], s["views_after"][g]) for g in s["views_after"]
                                                       if s["views_before"][g] != s["views_after"][g]})[:900])


def semantics_late(i: int, view: dict, speech: str) -> None:
    """The late-result goal, pinned by hand from the documented rule (steps.py, Phase 54/68): a step saved as unknown whose row now says
    succeeded is COMPLETED -- it took effect, but nothing read it back, so never `verified`."""
    st = P70.steps_of(view["report"])
    stored = [s["state"] for s in view["stored_steps"]]
    rep = view["report"]
    check(f"{i}.S [late_result] the step that outlived its timeout (the run stopped there) was saved as UNKNOWN and still is in the stored account, yet what the "
          "user is told follows the audit log, where its real outcome has since landed: completed -- not unknown, not failed, not verified; the send stage it "
          "never reached is still not done, so the goal is partial",
          stored == ["unknown"] and st == [("slow", "completed", False)] and rep["outcome"] == "partial" and "Send the file" in rep.get("remaining", []),
          f"{stored} {st} {rep}")
    check(f"{i}.S2 [late_result] the answer does not call the outcome unknown or any step failed, does not claim the step was confirmed, says it did not finish "
          "and names the work still not done, and promises no resume", "can't tell" not in speech and "Failed:" not in speech and "Outcome unknown" not in speech
          and "It did not finish" in speech and "Send the file" in speech and "confirmed" not in speech
          and "I won't resume or retry any of it on my own" in speech, speech)


def main() -> int:
    t0 = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="friday-p71-"))
    parent_pid = os.getpid()
    exp: dict[str, dict] = {}
    gids: dict[str, str] = {}
    answers: dict[str, str] = {}
    try:
        for i, name in enumerate(P70.SPECS, 1):
            scenario(f"{i}. {name}: A runs the goal and exits; a FRESH process asks FRIDAY what happened, the way a user does")
            dies = bool(P70.SPECS[name].get("dies"))
            pa = spawn(root, "write", name)
            check(f"{i}.A process A ran the scenario in its own OS process and exited "
                  + (f"with code {P70.KILL_CODE}, from INSIDE the tool" if dies else "with code 0, by a hard exit"),
                  pa.returncode == (P70.KILL_CODE if dies else 0), f"rc={pa.returncode}\n{pa.stderr[-1500:]}")
            if not dies:
                exp[name] = P70.load(root, f"expected_{name}.json")
            calls_ref = (root / "calls.log").read_bytes()
            ps, s = status_proc(root, f"S_{name}", "routed", list(gids.values()))
            check(f"{i}.S a fresh OS process (started after A had exited) answered the question and exited cleanly", s is not None,
                  f"rc={ps.returncode}\n{ps.stderr[-1800:]}")
            if s is None:
                continue
            gid = s["latest"]
            gids[name] = gid
            e = exp.get(name)
            check(f"{i}.S1 the question is about the goal process A created (the latest goal), not an earlier one",
                  list(gids.values()).count(gid) == 1 and (e is None or gid == e["gid"]), f"{gid} {gids}")
            routing_checks(f"{i}.R", s)
            check(f"{i}.R4 the router is selective: an unrelated sentence ('what time is it') is not sent to meta.goal_status",
                  s["unrelated_skill"] != STATUS, f"{s['unrelated_skill']} {s['unrelated_action']}")
            effect_checks(f"{i}.E", s, calls_ref, root, len(s["asks"]) + 1)
            view = s["views_after"][gid]
            speech = s["direct"]["speech"]
            answers[name] = speech
            if e is not None and name in RESTART_DIFFERS:
                check(f"{i}.W2 the stored account behind the answer is exactly what A saved (stage statuses, goal status, stored steps) -- asking did not bring it up to date",
                      view["stages"] == e["stages"] and view["goal_status"] == e["goal_status"] and view["stored_steps"] == e["stored_steps"],
                      f"{view} VS {e}")
            elif e is not None:
                check(f"{i}.W1 what the user is told is, word for word, what the process that ran the goal said"
                      if e["wording"] is not None else f"{i}.W1 the goal is exactly what process A left (the restart's wording is pinned below: A's own session is alive)",
                      (speech == e["wording"]) if e["wording"] is not None else view["report"] == e["live_report"],
                      f"{speech} VS {e['wording']}")
                check(f"{i}.W2 the persisted evidence behind the answer is A's: live report, stage statuses, goal status, stored step account (verdicts, recovery flags)",
                      view["report"] == e["live_report"] and view["stages"] == e["stages"] and view["goal_status"] == e["goal_status"]
                      and view["stored_steps"] == e["stored_steps"], f"{view} VS {e}")
            if name in RESTART_DIFFERS:
                semantics_late(i, view, speech)
            else:
                P70.semantics(i, name, e, {"speech": speech}, view)
            # a second, later, fresh process: the status rows the first one left in the audit log change nothing
            pt, t = status_proc(root, f"T_{name}", "direct", list(gids.values()))
            check(f"{i}.T a second fresh OS process, started after the first question's audit rows existed, asked again and exited cleanly",
                  t is not None, f"rc={pt.returncode}\n{pt.stderr[-1500:]}")
            if t is not None:
                effect_checks(f"{i}.T1", t, (root / "calls.log").read_bytes(), root, 1)
                check(f"{i}.T2 the answer and the goal are exactly what the first question saw: the earlier status rows did not leak into the goal",
                      t["direct"]["speech"] == speech and t["views_after"][gid] == view and t["latest"] == gid,
                      f"{t['direct']['speech']} VS {speech}")
            check(f"{i}.D no process touched the repository's data/ (opens, mkdirs, listings, reads included)", P70.no_data_access(root))

        # ---- the boot hook, then routed questions, then a final plain process ----------------------------------------------
        scenario("7. FRIDAY's boot hook runs, then the user asks; a further process asks again")
        calls_ref = (root / "calls.log").read_bytes()
        pc, c = status_proc(root, "C", "routed", list(gids.values()), boot=True)
        check("7.C a fresh OS process ran the boot hook and then answered the routed questions", c is not None, f"rc={pc.returncode}\n{pc.stderr[-1800:]}")
        pd, d = status_proc(root, "D", "direct", list(gids.values()))
        check("7.D a further fresh OS process asked again and exited cleanly", d is not None, f"rc={pd.returncode}\n{pd.stderr[-1800:]}")
        if c is not None and d is not None:
            killed = gids.get("killed_mid_step")
            check("7.1 the question was about the newest goal (the one whose process was killed inside a tool)", c["latest"] == killed, f"{c['latest']} {gids}")
            routing_checks("7.R", c)
            effect_checks("7.E", c, calls_ref, root, len(c["asks"]) + 1)
            check("7.2 the boot hook, not the question, is what settled things: before it the killed goal was 'running'; after it, it is not "
                  "(settled, reason recorded), the cut-off step is still unknown, and every other goal is exactly what it was",
                  c["views_pre_boot"][killed]["goal_status"] == "running" and c["views_before"][killed]["goal_status"] in ("partial", "failed")
                  and "interrupted" in c["views_before"][killed]["failure_reason"]
                  and P70.steps_of(c["views_before"][killed]["report"])[-1][:2] == ("die", "unknown")
                  and all(c["views_pre_boot"][g] == c["views_before"][g] for n, g in gids.items() if n not in SETTLED_AT_BOOT),
                  str(c["views_before"][killed])[:500])
            late = gids.get("late_result")
            check("7.2b the boot hook is also what brings the late-result goal's stored account up to date (its step: unknown -> completed), while the answer "
                  "for it is the same before and after, because it was always read from the audit log",
                  late is not None and [s["state"] for s in c["views_pre_boot"][late]["stored_steps"]][:1] == ["unknown"]
                  and [s["state"] for s in c["views_before"][late]["stored_steps"]][:1] == ["completed"]
                  and c["views_pre_boot"][late]["report"] == c["views_before"][late]["report"] == c["views_after"][late]["report"]
                  and d["views_after"][late] == c["views_after"][late], str(c["views_before"].get(late))[:500])
            word = c["direct"]["speech"]
            check("7.3 what the user is told about it: it cannot tell whether part of it took effect, it did not finish, no claim of success, and a promise "
                  "not to resume or retry on its own", "can't tell" in word and "It finished" not in word and "I won't resume or retry any of it on my own" in word, word)
            before_boot = [P70.load(root, f"out_{t}_killed_mid_step.json")["views_after"][killed]["goal_status"] for t in ("S", "T")]
            check("7.4 no question ever settled or healed a goal: the status processes that ran before the boot hook (S, T) left the killed goal 'running'",
                  before_boot == ["running", "running"], str(before_boot))
            effect_checks("7.F", d, (root / "calls.log").read_bytes(), root, 1)
            check("7.5 the further process gives the same answer and sees the same goals", d["direct"]["speech"] == word and d["views_after"] == c["views_after"],
                  f"{d['direct']['speech']} VS {word}")
            for name, g in gids.items():
                if name in SETTLED_AT_BOOT or name not in exp:
                    continue
                v = d["views_after"].get(g)
                check(f"7.6 [{name}] after the boot hook and every question (and the audit rows they left), the goal is still exactly A's"
                      if v is not None else f"7.6 [{name}] goal still readable", v is not None and v["report"] == exp[name]["live_report"]
                      and v["stages"] == exp[name]["stages"] and v["goal_status"] == exp[name]["goal_status"]
                      and v["stored_steps"] == exp[name]["stored_steps"], str(v))
            check("7.7 across every process the fixture tools ran only in process A: calls.log has no line from any status process",
                  all(line.startswith("A_") for line in (root / "calls.log").read_text(encoding="utf-8").splitlines()))
            pids = [ln.split() for ln in (root / "pids.log").read_text(encoding="utf-8").splitlines()]
            # Children run one after another, so Windows may hand a finished child's id to a later one: uniqueness across them proves nothing
            check("7.8 every one of the %d children was its own OS process, none of them this process" % len(pids),
                  str(parent_pid) not in {p[1] for p in pids} and len(pids) == 3 * len(P70.SPECS) + 2, str(pids))
            check("7.9 no process touched data/", P70.no_data_access(root))
        (root / "watched").mkdir()
        ph = spawn(root, "hookcheck")
        hits = (root / "data_hits_hookcheck.log").read_text(encoding="utf-8") if (root / "data_hits_hookcheck.log").exists() else ""
        check("7.10 the isolation hook works: pointed at a temp folder it records the open, the mkdir and the listing (so zero hits on data/ means "
              "something)", ph.returncode == 0 and "open " in hits and "os.mkdir" in hits and "os.listdir" in hits, f"rc={ph.returncode} {hits} {ph.stderr[-500:]}")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    elapsed = time.perf_counter() - t0
    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"Phase 71 -- asking FRIDAY what happened, after a restart, is a read  ({elapsed:.1f}s)")
    print("=" * 72)
    print(f"  assertions : {passed}/{len(RESULTS)} passed")
    print(f"  scenarios  : {len(SCENARIO_TITLES)}")
    gate = len(RESULTS) >= 150 and len(SCENARIO_TITLES) >= 8
    print(f"  gate >=150 assertions / >=8 scenarios: {'OK' if gate else 'MISS'}")
    assert os.getpid() == parent_pid
    if failed:
        print("\n  FAILED:")
        for label in failed:
            print(f"    - {label}")
        print("\nFAILURES ABOVE")
        return 1
    print("\nALL OK" if gate else "\nGATES MISSED")
    return 0 if gate else 1


def _child_main() -> int:
    a = sys.argv
    root = Path(a[a.index("--root") + 1])
    role = a[a.index("--child") + 1]
    if role == "hookcheck":
        P70.child_hookcheck(root)
    elif role == "write":
        P70.child_write(root, a[a.index("write") + 1])
    elif role == "status":
        child_status(root, a[a.index("--tag") + 1], a[a.index("--mode") + 1], "--boot" in a, json.loads(a[a.index("--ids") + 1]))
    return 0


if __name__ == "__main__":
    sys.exit(_child_main() if "--child" in sys.argv else main())
