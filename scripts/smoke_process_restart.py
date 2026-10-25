"""Phase 70 -- real process-restart validation: one OS process writes a goal, EXITS, and a separate OS process reads it back.

Phases 54, 67, 68 and 69 made a goal's step states, failed read-back verdicts, recovery flags, stage statuses and final outcome durable and
consistent -- but every one of those tests reconstructed inside the process that had just run the goal (at best with a dropped DB
connection). This suite crosses a genuine process boundary:

  process A   runs ONE scenario through the real `plan.run` (real Executor, real audit, real Goal rows, a scripted model behind the `llm`
              seam, deterministic `test.pr_*` tools with deterministic verifiers), saves what it expects, and EXITS HARD (`os._exit`: no
              atexit, no flush of anything but what was committed -- as after a crash or a kill). One scenario kills A INSIDE a tool.
  process B   starts fresh against the same throwaway database and calls only the read-only status path (`meta.goal_status`'s function,
              `steps.describe_goal`, and `steps.reconstruct`). It must reproduce A's result and change nothing.
  process C   a restart that runs FRIDAY's boot hook (`audit.start_session`: reconcile dead owners, settle goals left running, refresh stored
              progress) -- the only writer of the later steps -- and then reads again.
  process D   a final read-only process: after the boot hook, every goal must still be what A said it was.

The parent process imports nothing from `friday` (importing it opens the log file); each child redirects `friday.paths` (database, logs,
model and cache folders, recovery store) into the temp folder BEFORE importing `friday`, stubs `fastembed` (no model is read or downloaded) and
installs an audit hook that records ANY open / mkdir / listdir / rename / remove of a path under the repository's `data/` -- reads included --
into the temp folder. The suite fails if there is one. Nothing real runs and the only database is a throwaway under the OS temp folder.

Not covered, and not claimed: a graceful shutdown (the Phase 48 "outcome unknown" marker is written by an atexit hook that a hard exit skips),
the GUI, voice and the daemon -- only the goal-progress path they all read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()

W, S, SL, DIE = "test.pr_write", "test.pr_send", "test.pr_slow", "test.pr_die"
STAGES = ["Write the file", "Send the file"]
GOAL = "Write a file, then send it"

# Scenario specs (pure data). decisions: (tool, args, stage index)
SPECS: dict[str, dict] = {
    "verified": {
        "decisions": [(W, {"name": "a", "text": "x"}, 0), (S, {"name": "a"}, 1)],
    },
    "readback_failed": {
        "contra": ["a"],
        "decisions": [(W, {"name": "a", "text": "x"}, 0), (S, {"name": "a"}, 1)],
    },
    "recovered": {
        "contra_once": ["g"],
        "decisions": [(W, {"name": "g", "text": "x"}, 0), (W, {"name": "g", "text": "again"}, 0), (S, {"name": "g"}, 1)],
    },
    "unresolved_failure": {
        "fail": ["u"],
        "decisions": [(W, {"name": "u", "text": "x"}, 0), (S, {"name": "u"}, 1)],
    },
    "unknown_outcome": {
        "step_timeout": 1.0,
        "decisions": [(SL, {"name": "s"}, 0), (S, {"name": "s"}, 1)],
    },
    "killed_mid_step": {
        "dies": True,
        "decisions": [(W, {"name": "k", "text": "x"}, 0), (DIE, {"name": "k"}, 1)],
    },
}
KILL_CODE = 7


# =============================================================================================================================
# child side (everything that imports `friday` lives below this line, inside functions)
# =============================================================================================================================
def _bootstrap(root: Path, role: str, watch: Path | None = None) -> None:
    """Isolation FIRST: the audit hook, then the redirect of every path FRIDAY writes, then the model stub -- all before `friday` is used.
    (`watch` exists only for the hook's own positive control: it points the hook at a temp folder instead of data/.)"""
    data = os.path.normcase(str(watch or ROOT / "data"))
    pid_fd = os.open(str(root / "pids.log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND)
    os.write(pid_fd, f"{role} {os.getpid()}\n".encode())
    os.close(pid_fd)
    fd = os.open(str(root / f"data_hits_{role}.log"), os.O_CREAT | os.O_WRONLY | os.O_APPEND)   # opened BEFORE the hook, written with os.write
    events = {"open", "os.mkdir", "os.listdir", "os.scandir", "os.rename", "os.remove", "os.rmdir", "shutil.copyfile", "os.chmod", "os.utime"}

    def hook(event, args):  # noqa: ANN001
        if event in events and args and isinstance(args[0], (str, bytes, os.PathLike)):
            try:
                s = os.path.normcase(os.path.abspath(os.fsdecode(args[0])))
            except Exception:  # noqa: BLE001
                return
            if s == data or s.startswith(data + os.sep):
                os.write(fd, f"{event} {s}\n".encode())

    sys.addaudithook(hook)
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    from friday import paths

    paths.DATA = root / "data"
    paths.LOGS, paths.MODELS, paths.CACHE = paths.DATA / "logs", paths.DATA / "models", paths.DATA / "cache"
    paths.LEGACY_DB = paths.DATA / "friday.db"
    paths.DB_PATH = root / "friday.db"                      # the same redirect `store.use_temp_db` makes (a test entry point is refused the real one)
    paths.SNAPSHOTS, paths.LEGACY_SNAPSHOTS = root / "snapshots", root / "legacy-snapshots"

    import types

    import numpy as np

    fake = types.ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, model_name=None, cache_dir=None, **kw):  # noqa: ANN001
            self.dim = 512

        def embed(self, texts):  # noqa: ANN001
            import zlib

            for t in texts:
                v = np.zeros(self.dim, dtype=np.float32)
                for w in re.findall(r"[a-z0-9]+", t.lower()):
                    v[zlib.crc32(w.encode()) % self.dim] += 1.0
                yield v

    fake.TextEmbedding = TextEmbedding
    sys.modules["fastembed"] = fake


class Fixtures:
    """The deterministic tools and verifiers. Registered in EVERY child (a restarted FRIDAY has its skills registered too, and the step
    report classifies a tool by its registration) -- but only process A ever runs them. Every run is appended to `calls.log`."""

    def __init__(self, root: Path, role: str, spec: dict | None) -> None:
        from friday import auditview, verify
        from friday.registry import SkillResult, skill

        self.invocations: list[str] = []
        calls_log = root / "calls.log"
        spec = spec or {}
        contra, contra_once, fail = set(spec.get("contra", [])), set(spec.get("contra_once", [])), set(spec.get("fail", []))

        def record(what: str) -> None:
            self.invocations.append(what)
            with open(calls_log, "a", encoding="utf-8") as f:
                f.write(f"{role}:{what}\n")

        @skill(name=W, tier="L1", action="modify", description="write a named file (Phase 70 fixture)")
        def write(name: str = "a", text: str = "") -> SkillResult:
            record(f"write:{name}")
            if name in fail:
                return SkillResult(speech="Could not write the file: disk full.", ok=False)
            return SkillResult(speech="Wrote the file.")

        @skill(name=S, tier="L1", action="communicate", description="send a named file (Phase 70 fixture)")
        def send(name: str = "a") -> SkillResult:
            record(f"send:{name}")
            return SkillResult(speech="Sent the file.")

        @skill(name=SL, tier="L1", action="modify", description="a write that outlives its timeout (Phase 70 fixture)")
        def slow(name: str = "a") -> SkillResult:
            record(f"slow:{name}")
            time.sleep(float(spec.get("slow_s", 60)))       # Phase 71: a spec may make it finish late instead of never
            return SkillResult(speech="Wrote the slow file.")

        @skill(name=DIE, tier="L1", action="modify", description="a write during which the process dies (Phase 70 fixture)")
        def die(name: str = "a") -> SkillResult:
            record(f"die:{name}")
            os._exit(KILL_CODE)

        auditview.POLICY[W] = {"name": auditview.KEEP, "text": auditview.OMIT}
        auditview.POLICY[S] = {"name": auditview.KEEP}
        auditview.POLICY[SL] = {"name": auditview.KEEP}
        auditview.POLICY[DIE] = {"name": auditview.KEEP}

        def readback(args, data, before):  # noqa: ANN001
            name = args.get("name", "")
            bad = name in contra or name in contra_once
            contra_once.discard(name)
            return [verify.Check("fixture effect", not bad, "the file is there", "it is not" if bad else "the file is there")]

        verify.register(W, verify.Verifier(readback))
        verify.register(S, verify.Verifier(lambda a, d, b: [verify.Check("sent", True, "sent", "sent")]))


def _digest() -> dict:
    """What a read must not change: every audit row, every goal row, and how many of each there are."""
    import hashlib

    from friday import store

    c = store.conn()
    out = {}
    for table in ("audit", "goals"):
        rows = [tuple(r) for r in c.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()]
        out[table] = {"n": len(rows), "sha": hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()}
    return out


def _goal_view(gid: str) -> dict:
    from friday import steps
    from friday.intelligence import goals as goals_mod

    g = goals_mod.get(gid)
    rep = steps.reconstruct(gid)
    prog = g.progress if isinstance(g.progress, dict) else {}
    return {
        "gid": gid, "goal_status": g.status.value, "stages": [s.status.value for s in (g.subgoals or [])],
        "report": rep.to_dict(), "tail": rep.tail(), "failure_reason": g.failure_reason or "",
        "stored_steps": [{"tool": s.get("tool"), "state": s.get("state"), "ver": s.get("ver"), "recovered": bool(s.get("recovered"))}
                         for s in prog.get("steps", [])],
        "stored_stopped": prog.get("stopped"),
    }


def child_write(root: Path, name: str) -> None:
    import asyncio
    import threading

    spec = SPECS[name]
    _bootstrap(root, f"A_{name}")
    import phase21_common as H
    from phase21_common import ScriptedPlanner, done, scripted_provider

    from friday import steps
    from friday.config import CFG
    from friday.intelligence import goals as goals_mod
    from friday.llm import LlmResponse
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY

    REGISTRY.discover()
    fx = Fixtures(root, f"A_{name}", spec)
    H.lock_down_real_tools(REGISTRY)
    CFG.desktop_observer.enabled = False
    CFG.planner.subgoal_evidence_advance = True
    CFG.planner.step_timeout_s = float(spec.get("step_timeout", 90.0))
    CFG.planner.total_timeout_s = 120.0

    replies = [json.dumps({"action": "call", "tool": t, "args": a, "subgoal_index": i}) for t, a, i in spec["decisions"]] + [done("All done.")]

    class Planner(ScriptedPlanner):
        n = 0

        async def complete(self, request):  # noqa: ANN001
            self.calls += 1
            system = next((m.content for m in request.messages if m.role == "system"), "")
            if "Break a real-world goal down" in system:
                text = json.dumps({"subgoals": [{"description": d, "rationale": "", "success_evidence": ""} for d in STAGES]})
            else:
                text = self.replies[min(self.n, len(self.replies) - 1)]
                self.n += 1
            return LlmResponse(text=text, model="scripted", provider=self.name)

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    with scripted_provider(Planner(replies)):
        r = asyncio.run_coroutine_threadsafe(EXECUTOR.run("plan.run", {"goal": GOAL}, actor="text"), loop).result(120)
    if spec.get("dies"):
        raise SystemExit("the process was supposed to die inside a tool")
    gid = r.data["goal_id"]
    view = _goal_view(gid)
    # what A saw, to compare with what a separate process sees. `wording` is the status answer as A itself gives it -- but for a goal whose
    # step is still open A's OWN session is alive, so the status would (rightly) say "running" there, not what a restart says: the
    # restart's wording for that scenario is pinned by hand in the parent instead.
    exp = {
        "gid": gid, "live_report": r.data["report"], "ok": r.ok, "stopped": r.data["stopped"], "verified": r.data["verified"],
        "verification": r.data.get("verification", {}).get("status", "not_applicable"), "goal_status": view["goal_status"],
        "stages": view["stages"], "stored_steps": view["stored_steps"],
        "wording": None if name == "unknown_outcome" else steps.describe_goal()[0],
        "audit": _digest()["audit"]["n"], "invocations": list(fx.invocations),
    }
    (root / f"expected_{name}.json").write_text(json.dumps(exp), encoding="utf-8")
    sys.stdout.flush()
    time.sleep(float(spec.get("linger_s", 0)))     # Phase 71: stay alive long enough for an abandoned step's late result to land
    os._exit(0)     # a hard exit, with the slow tool's thread still asleep and no atexit hook run


def child_read(root: Path, tag: str, boot: bool, ids: list[str]) -> None:
    _bootstrap(root, tag)
    from friday import audit, steps
    from friday.intelligence import goals as goals_mod
    from friday.registry import REGISTRY

    REGISTRY.discover()
    fx = Fixtures(root, tag, None)               # registered (a restart knows its tools), never run
    calls_before = (root / "calls.log").read_bytes() if (root / "calls.log").exists() else b""
    before = _digest()
    if boot:
        audit.start_session("cli")               # FRIDAY's boot hook: reconcile dead owners, settle running goals, refresh stored progress
    mid = _digest()
    latest = goals_mod.recent(1)[0].id
    speech, data = REGISTRY.get("meta.goal_status").fn().speech, None   # the production read-only status skill, called directly
    sp2, data = steps.describe_goal()
    views = {gid: _goal_view(gid) for gid in dict.fromkeys([latest, *ids])}
    after = _digest()
    calls_after = (root / "calls.log").read_bytes() if (root / "calls.log").exists() else b""
    out = {
        "latest": latest, "speech": speech, "speech_same_as_describe": speech == sp2, "outcome": (data.get("report") or {}).get("outcome"),
        "views": views, "digest_before": before, "digest_mid": mid, "digest_after": after,
        "calls_unchanged": calls_before == calls_after, "invocations": list(fx.invocations),
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


def spawn(root: Path, *args: str, timeout: int = 150) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("FRIDAY_ALLOW_REAL_DB", "FRIDAY_DB")}
    return subprocess.run([sys.executable, str(SCRIPT), "--child", *args, "--root", str(root)], capture_output=True, text=True,
                          timeout=timeout, env=env, encoding="utf-8", errors="replace")


def load(root: Path, fname: str) -> dict:
    return json.loads((root / fname).read_text(encoding="utf-8"))


def done_clause(tail: str) -> str:
    m = re.search(r"Done: (.*?)(?= Failed:| Ran after| Still running:| Outcome unknown:| Waiting on you:| Not attempted:|$)", tail)
    return m.group(1) if m else ""


def steps_of(report: dict) -> list[tuple]:
    return [(s["tool"].split("_")[-1], s["state"], bool(s.get("recovered"))) for s in report["steps"]]


def no_data_access(root: Path) -> bool:
    return all(p.stat().st_size == 0 for p in root.glob("data_hits_*.log") if "hookcheck" not in p.name)


def main() -> int:
    t0 = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="friday-p70-"))
    parent_pid = os.getpid()
    exp: dict[str, dict] = {}
    gids: dict[str, str] = {}
    outs: dict[str, dict] = {}
    try:
        for i, name in enumerate(SPECS, 1):
            scenario(f"{i}. {name}: process A runs the goal and exits; a SEPARATE process B reads it back")
            dies = bool(SPECS[name].get("dies"))
            pa = spawn(root, "write", name)
            check(f"{i}.A process A ran in its own OS process (not this one) and exited "
                  + (f"with code {KILL_CODE}, from INSIDE the tool" if dies else "with code 0, by a hard exit"),
                  pa.returncode == (KILL_CODE if dies else 0), f"rc={pa.returncode}\n{pa.stderr[-1500:]}")
            if not dies:
                exp[name] = load(root, f"expected_{name}.json")
            calls_after_a = (root / "calls.log").read_bytes()
            pb = spawn(root, "read", name, "--tag", f"B_{name}", "--ids", json.dumps(list(gids.values())))
            check(f"{i}.B process B (a fresh OS process, started after A had exited) read the database and exited cleanly",
                  pb.returncode == 0, f"rc={pb.returncode}\n{pb.stderr[-1500:]}")
            if pb.returncode != 0:
                continue
            b = load(root, f"out_B_{name}.json")
            outs[name] = b
            gid = b["latest"]
            gids[name] = gid
            view = b["views"][gid]
            check(f"{i}.B1 B found a goal that process A created (a new goal, not an earlier one)",
                  list(gids.values()).count(gid) == 1 and (name == "killed_mid_step" or gid == exp[name]["gid"]), f"{gid} {gids}")
            check(f"{i}.B2 reading changed nothing: every audit row and goal row is byte-for-byte what A left, no tool ran, calls.log untouched, "
                  "the fixtures were registered in B but never invoked",
                  b["digest_before"] == b["digest_mid"] == b["digest_after"] and b["calls_unchanged"]
                  and (root / "calls.log").read_bytes() == calls_after_a and b["invocations"] == [], str(b["digest_before"]) + str(b["digest_after"]))
            check(f"{i}.B3 B used no path under the repository's data/ (opens, mkdirs, listings: none, reads included)", no_data_access(root))
            if not dies:
                e = exp[name]
                check(f"{i}.B4 B's reconstruction IS A's live report (outcome, every step state, recovery flags, what remains, next action)",
                      view["report"] == e["live_report"], f"{view['report']} VS {e['live_report']}")
                check(f"{i}.B5 stage statuses and the goal's status are A's", view["stages"] == e["stages"] and view["goal_status"] == e["goal_status"],
                      f"{view['stages']} {view['goal_status']} VS {e['stages']} {e['goal_status']}")
                check(f"{i}.B6 the stored step account (state, read-back verdict, recovery flag) is A's, untouched by the read",
                      view["stored_steps"] == e["stored_steps"], f"{view['stored_steps']} VS {e['stored_steps']}")
                if e["wording"] is not None:
                    check(f"{i}.B7 the user-facing status answer is word for word what A gave", b["speech"] == e["wording"], f"{b['speech']} VS {e['wording']}")
            check(f"{i}.B8 the production status skill (meta.goal_status) says the same as describe_goal", b["speech_same_as_describe"])
            semantics(i, name, exp.get(name), b, view)

        # ---- the restart that runs the boot hook, then a final read-only process ---------------------------------------------
        scenario("7. process C restarts FRIDAY's boot hook (reconcile, settle, refresh); process D then only reads")
        calls_before_c = (root / "calls.log").read_bytes()
        pc = spawn(root, "boot", "--tag", "C", "--ids", json.dumps(list(gids.values())))
        check("7.C a separate OS process ran the boot hook and exited cleanly", pc.returncode == 0, f"rc={pc.returncode}\n{pc.stderr[-1500:]}")
        pd = spawn(root, "read", "x", "--tag", "D", "--ids", json.dumps(list(gids.values())))
        check("7.D a further separate OS process read everything and exited cleanly", pd.returncode == 0, f"rc={pd.returncode}\n{pd.stderr[-1500:]}")
        if pc.returncode == 0 and pd.returncode == 0:
            c, d = load(root, "out_C.json"), load(root, "out_D.json")
            check("7.1 the boot hook added no audit row and no goal, ran no tool and resumed nothing (calls.log identical, nothing invoked)",
                  c["digest_before"]["audit"]["n"] == c["digest_after"]["audit"]["n"] and c["digest_before"]["goals"]["n"] == c["digest_after"]["goals"]["n"]
                  and c["invocations"] == [] and (root / "calls.log").read_bytes() == calls_before_c)
            check("7.2 process D (read-only) changed nothing", d["digest_before"] == d["digest_mid"] == d["digest_after"] and d["calls_unchanged"]
                  and d["invocations"] == [])
            check("7.3 no process touched data/", no_data_access(root))
            for name in SPECS:
                if name == "killed_mid_step" or name not in exp:
                    continue
                v = d["views"][gids[name]]
                e = exp[name]
                check(f"7.4 [{name}] after the boot hook and a further restart the goal is still exactly A's: report, stages, status, "
                      "stored verdicts and recovery flags", v["report"] == e["live_report"] and v["stages"] == e["stages"]
                      and v["goal_status"] == e["goal_status"] and v["stored_steps"] == e["stored_steps"],
                      f"{v['report']} {v['stages']} {v['goal_status']} {v['stored_steps']} VS {e}")
            if "killed_mid_step" in gids:
                k = d["views"][gids["killed_mid_step"]]
                kb = outs["killed_mid_step"]["views"][gids["killed_mid_step"]]
                check("7.5 [killed_mid_step] the boot hook SETTLED the goal that no process was running: it is no longer 'running' (partial, with "
                      "the reason recorded), nothing was resumed, the cut-off step is still unknown", kb["goal_status"] == "running"
                      and k["goal_status"] in ("partial", "failed") and "interrupted" in k["failure_reason"]
                      and steps_of(k["report"])[-1][:2] == ("die", "unknown") and k["report"]["outcome"] == "unknown",
                      f"{kb['goal_status']} -> {k['goal_status']} {k['failure_reason']} {k['report']}")
                check("7.6 ...and the status answer afterwards no longer calls it running, still says it cannot tell, and promises no retry",
                      "still running" not in d["speech"].lower() or d["latest"] != gids["killed_mid_step"], d["speech"])
            check("7.7 across every restart the fixtures ran only in process A: calls.log has no line from B, C or D",
                  all(line.startswith("A_") for line in (root / "calls.log").read_text(encoding="utf-8").splitlines()))
            pids = [ln.split() for ln in (root / "pids.log").read_text(encoding="utf-8").splitlines()]
            check("7.8 every child was a genuinely separate OS process: %d distinct process ids, none of them this process's" % len(pids),
                  len({p[1] for p in pids}) == len(pids) == 2 * len(SPECS) + 2 and str(parent_pid) not in {p[1] for p in pids}, str(pids))
        (root / "watched").mkdir()
        ph = spawn(root, "hookcheck")
        hits = (root / "data_hits_hookcheck.log").read_text(encoding="utf-8") if (root / "data_hits_hookcheck.log").exists() else ""
        check("7.9 the isolation hook works: pointed at a temp folder it records the open, the mkdir and the listing (so zero hits on data/ means "
              "something)", ph.returncode == 0 and "open " in hits and "os.mkdir" in hits and "os.listdir" in hits, f"rc={ph.returncode} {hits} {ph.stderr[-500:]}")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    elapsed = time.perf_counter() - t0
    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = [label for ok, label in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"Phase 70 -- a goal written by one OS process is the same goal in the next  ({elapsed:.1f}s)")
    print("=" * 72)
    print(f"  assertions : {passed}/{len(RESULTS)} passed")
    print(f"  scenarios  : {len(SCENARIO_TITLES)}")
    gate = len(RESULTS) >= 60 and len(SCENARIO_TITLES) >= 7
    print(f"  gate >=60 assertions / >=7 scenarios: {'OK' if gate else 'MISS'}")
    assert os.getpid() == parent_pid
    if failed:
        print("\n  FAILED:")
        for label in failed:
            print(f"    - {label}")
        print("\nFAILURES ABOVE")
        return 1
    print("\nALL OK" if gate else "\nGATES MISSED")
    return 0 if gate else 1


def semantics(i: int, name: str, e: dict | None, b: dict, view: dict) -> None:
    """What each scenario MEANS, pinned by hand and independent of whatever A happened to compute -- so equality of two wrong answers
    could not pass."""
    rep, speech, tail = view["report"], b["speech"], view["tail"]
    st = steps_of(rep)
    if name == "verified":
        check(f"{i}.S [verified] complete, both steps verified, both stages and the goal succeeded, the answer says it finished and every change was confirmed",
              rep["outcome"] == "complete" and [s[1] for s in st] == ["verified", "verified"] and view["stages"] == ["succeeded", "succeeded"]
              and view["goal_status"] == "succeeded" and "It finished, and every step that changed something was confirmed" in speech, speech)
    elif name == "readback_failed":
        wr = rep["steps"][0]
        check(f"{i}.S [readback_failed] the write whose read-back failed is FAILED (not unknown, not completed), not recovered, its failed verdict "
              "is still stored, and the goal is not a success",
              wr["state"] == "failed" and not wr.get("recovered") and view["stored_steps"][0]["ver"]["status"] == "failed"
              and view["goal_status"] != "succeeded" and rep["outcome"] in ("partial", "failed"), str(view))
        check(f"{i}.S2 [readback_failed] the answer names the failed tool under Failed, never under Done, and does not say it finished",
              "Failed: 'test.pr_write'" in speech and "test.pr_write" not in done_clause(speech) and "It finished" not in speech, speech)
    elif name == "recovered":
        check(f"{i}.S [recovered] the contradicted write is failed AND recovered, the retry verified, the recovery decided by a later VERIFIED step "
              "(Phase 69): both stages and the goal succeeded, the run ended completed and ok",
              st[:2] == [("write", "failed", True), ("write", "verified", False)] and view["stages"] == ["succeeded", "succeeded"]
              and view["goal_status"] == "succeeded" and e["stopped"] == "completed" and e["ok"] is True and e["verification"] == "verified"
              and rep["outcome"] == "complete_unverified", f"{view} {e}")
        check(f"{i}.S2 [recovered] the answer says it finished, does not name the recovered step as failed, and does not call the outcome unknown",
              "It finished" in speech and "Failed:" not in speech and "can't tell" not in speech, speech)
    elif name == "unresolved_failure":
        check(f"{i}.S [unresolved_failure] a tool failure nothing redid stays failed and unrecovered; the goal is not succeeded; the outcome is partial",
              st[0] == ("write", "failed", False) and view["goal_status"] != "succeeded" and rep["outcome"] == "partial"
              and view["stages"][0] != "succeeded", str(view))
        check(f"{i}.S2 [unresolved_failure] the answer says it did not finish and names the failure; a later success is not vouched for",
              "It did not finish" in speech and "Failed: 'test.pr_write'" in speech and "I can try the failed step again if you ask" in speech, speech)
    elif name == "unknown_outcome":
        check(f"{i}.S [unknown_outcome] the step that outlived its timeout (its process then died) is UNKNOWN, never failed and never completed; the "
              "outcome is unknown; no stage and not the goal is succeeded; what never ran is listed",
              st[0] == ("slow", "unknown", False) and rep["outcome"] == "unknown" and "succeeded" not in view["stages"]
              and view["goal_status"] != "succeeded" and "Send the file" in rep.get("remaining", []), str(view))
        check(f"{i}.S2 [unknown_outcome] the answer says it cannot tell whether it took effect, tells the user to check, and promises no retry or resume",
              "can't tell whether part of it took effect" in speech and "Check whether 'test.pr_slow' took effect" in speech
              and "I won't resume or retry any of it on my own" in speech, speech)
    elif name == "killed_mid_step":
        check(f"{i}.S [killed_mid_step] the process died INSIDE the second tool: the goal is still 'running' on disk, the first step (checkpointed) "
              "is verified, the cut-off step is found from the audit log and is UNKNOWN, and the outcome is unknown",
              view["goal_status"] == "running" and st[0][:2] == ("write", "verified") and st[-1][:2] == ("die", "unknown")
              and rep["outcome"] == "unknown", str(view))
        check(f"{i}.S2 [killed_mid_step] the answer does not claim success or progress it cannot back: it says it cannot tell, and promises no resume",
              "can't tell" in speech and "I won't resume or retry any of it on my own" in speech and "It finished" not in speech, speech)


def child_hookcheck(root: Path) -> None:
    """The audit hook's positive control: pointed at a temp folder, it must see an open there (so "no hits" means something)."""
    watched = root / "watched"
    _bootstrap(root, "hookcheck", watch=watched)
    with open(watched / "f.txt", "w", encoding="utf-8") as f:
        f.write("x")
    (watched / "listing").mkdir(exist_ok=True)
    os.listdir(watched)


def _child_main() -> int:
    a = sys.argv
    root = Path(a[a.index("--root") + 1])
    role = a[a.index("--child") + 1]
    tag = a[a.index("--tag") + 1] if "--tag" in a else role
    ids = json.loads(a[a.index("--ids") + 1]) if "--ids" in a else []
    if role == "hookcheck":
        child_hookcheck(root)
    elif role == "write":
        child_write(root, a[a.index("write") + 1])
    elif role == "read":
        child_read(root, tag, False, ids)
    elif role == "boot":
        child_read(root, tag, True, ids)
    return 0


if __name__ == "__main__":
    sys.exit(_child_main() if "--child" in sys.argv else main())
