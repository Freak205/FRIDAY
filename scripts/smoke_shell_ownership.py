"""Phase 91 -- the process tree of a shell command is owned BEFORE the child can run a single instruction.

Phase 90 assigned the child to its job object after `Popen` returned, so a child could start a descendant first and that descendant
would never be in the job. Now `run_owned` creates the job, creates the child SUSPENDED, assigns it, and only then resumes it; if
any step fails, the child never runs. Real processes, no mocks of the thing under test: every child is a throwaway script in a temp
dir (`sys.executable` / the base interpreter), nothing destructive runs, `data/` and the real key are untouched (this runs inside
`iso_env.isolated_runtime()`), and nothing is killed by name -- leftovers are killed only by recorded (pid, create_time). The few
kernel-level seams (`_new_job`, `_assign`, `_resume`, `_active_count`) are replaced only to force a path or to observe the order.

  A. a child cannot execute before it is assigned to the job (marker file written as its first statement), nor before the resume
  B. a child and ALL its descendants are members of the operation's job (30 rounds over the old race window, zero escapes)
  C. no job object -> nothing started (Popen never called, marker never created), a fixed reason code, never ok
  D. assignment failure -> the child never runs, is gone, and the outcome is not_started
  E. cleanup of the suspended process on every failure path (assign / resume / Popen error / BaseException): child reaped, pipes
     closed, job handle released, registry empty, no handle growth over repeated failures
  F. normal execution and completion are unchanged (differential against the old `subprocess.run`), detached processes survive
  G. STOP and the time limit still end the owned tree (graceful, forced, unknown)
  H. unrelated processes and other operations are not affected, including by a failed start next door
  I. `_resume` safety checks (a second suspend count, extra threads, an exited process)
  J. nested job objects (FRIDAY itself inside a job)
  K. kill-on-close while the child is suspended (a FRIDAY crash in that window kills it), disarmed before the child is released
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import psutil  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32job  # noqa: E402
from phase21_common import check, finish, scenario  # noqa: E402

from friday import shellproc  # noqa: E402
from friday.skills import shell  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="p91-"))
BASE_PY = getattr(sys, "_base_executable", None) or getattr(sys, "base_executable", None) or sys.executable
SPAWNED: dict[int, float] = {}   # pid -> create_time, so a leftover is killed only if it is still THAT process
NOTES: list[str] = []

CHILD = TMP / "own_child.py"
CHILD.write_text(
    r'''
import os, signal, subprocess, sys, time
marker, ready, mode = sys.argv[1], sys.argv[2], sys.argv[3]
extra = sys.argv[4] if len(sys.argv) > 4 else ""
open(marker, "w").write("x")          # the FIRST statement: proof the child ran

def hold(seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.05)

def mark(*pids):
    with open(ready, "w") as f:
        f.write(" ".join(str(p) for p in pids))

SLEEPER = "import time\nwhile True: time.sleep(0.2)"
if mode == "quick":
    print("ok", flush=True); sys.exit(0)
if mode == "exit3":
    print("out-text", flush=True); sys.stderr.write("err-text\n"); sys.stderr.flush(); sys.exit(3)
if mode == "unicode":
    print("café naïve", flush=True); sys.stderr.write("über\n"); sys.exit(0)
if mode == "cwd":
    print(os.getcwd(), flush=True); sys.exit(0)
if mode == "tree":
    gcs = [subprocess.Popen([extra, "-c", SLEEPER]) for _ in range(3)]    # the FIRST thing after the marker
    print("up", flush=True); mark(os.getpid(), *[g.pid for g in gcs]); hold(120)
if mode == "sleep":
    print("up", flush=True); mark(os.getpid()); hold(120)
if mode == "ignore":
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    print("up", flush=True); mark(os.getpid()); hold(120)
if mode == "finite":
    print("started", flush=True); mark(os.getpid()); hold(float(extra)); print("finished", flush=True); sys.exit(0)
if mode == "detach":
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    d = subprocess.Popen([sys.argv[5], "-c", "import time\ntime.sleep(" + extra + ")"], creationflags=flags)
    mark(d.pid); sys.exit(0)
''',
    encoding="utf-8",
)

SLEEPER = TMP / "sleeper.py"      # started by PowerShell's Start-Process: records its pid, then lives a few seconds
SLEEPER.write_text(
    "import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(float(sys.argv[2]))\n", encoding="utf-8",
)

NEST = TMP / "nest.py"            # runs run_owned from INSIDE an outer job object (nested jobs)
NEST.write_text(
    r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from friday import shellproc
child, marker, ready, mode, *rest = sys.argv[2:]
a = [sys.executable, child, marker, ready, mode, *rest]
def stop():
    return mode in ("tree", "sleep") and Path(ready).exists() and Path(ready).read_text().strip() != ""
o = shellproc.run_owned(a, cwd=Path(child).parent, timeout_s=40, stop_requested=stop, grace_s=1.0, confirm_s=1.0)
print(json.dumps({"state": o.state, "how": o.how, "detail": o.detail, "rc": o.returncode, "out": o.stdout}))
''',
    encoding="utf-8",
)

_n = [0]


def paths(tag: str) -> tuple[Path, Path]:
    _n[0] += 1
    marker, ready = TMP / f"{tag}{_n[0]}.marker", TMP / f"{tag}{_n[0]}.ready"
    marker.unlink(missing_ok=True)
    ready.unlink(missing_ok=True)
    return marker, ready


def argv(mode: str, tag: str, *extra: str) -> tuple[list[str], Path, Path]:
    marker, ready = paths(tag)
    return [sys.executable, str(CHILD), str(marker), str(ready), mode, *extra], marker, ready


def cmdline(mode: str, tag: str, *extra: str) -> tuple[str, Path, Path]:
    a, marker, ready = argv(mode, tag, *extra)
    return " ".join(a), marker, ready


def note(pid: int) -> None:
    try:
        SPAWNED[pid] = psutil.Process(pid).create_time()
    except psutil.Error:
        pass


def tree_of(pid: int) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    try:
        root = psutil.Process(pid)
        for p in [root, *root.children(recursive=True)]:
            out.append((p.pid, p.create_time()))
            SPAWNED[p.pid] = p.create_time()
    except psutil.Error:
        pass
    return out


def alive(pid: int, ctime: float) -> bool:
    try:
        return abs(psutil.Process(pid).create_time() - ctime) < 0.01 and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def all_gone(snap: list[tuple[int, float]], wait: float = 3.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not any(alive(p, c) for p, c in snap):
            return True
        time.sleep(0.05)
    return not any(alive(p, c) for p, c in snap)


def wait_for(path: Path, seconds: float = 15.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.exists() and path.read_text().strip():
            return True
        time.sleep(0.05)
    return False


def cleanup() -> None:
    for pid, ctime in list(SPAWNED.items()):
        if alive(pid, ctime):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass


def kill_on_close(job) -> bool:
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    return bool(info["BasicLimitInformation"]["LimitFlags"] & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)


def in_job(pid: int, job) -> bool | None:
    try:
        h = win32api.OpenProcess(win32con.PROCESS_QUERY_INFORMATION, False, pid)
    except Exception:  # noqa: BLE001
        return None            # the process is gone
    try:
        return bool(win32job.IsProcessInJob(h, job))
    finally:
        h.Close()


class Rec:
    """Records every Popen (with its create time and flags) and every job handle `_new_job` hands out, for the duration of a with-block."""

    def __init__(self) -> None:
        self.popens: list[subprocess.Popen] = []
        self.flags: list[int] = []
        self.ctimes: list[float] = []
        self.jobs: list = []

    def __enter__(self) -> "Rec":
        rec = self
        self.o_popen, self.o_new = subprocess.Popen, shellproc._new_job

        class RecPopen(subprocess.Popen):
            def __init__(self, *aa, **kk):
                super().__init__(*aa, **kk)
                rec.popens.append(self)
                rec.flags.append(int(kk.get("creationflags", 0)))
                try:
                    rec.ctimes.append(psutil.Process(self.pid).create_time())
                except psutil.Error:
                    rec.ctimes.append(0.0)

        def new_job():
            j = rec.o_new()
            rec.jobs.append(j)
            return j

        subprocess.Popen = RecPopen
        shellproc._new_job = new_job
        return self

    def __exit__(self, *exc) -> None:
        subprocess.Popen, shellproc._new_job = self.o_popen, self.o_new


class Op:
    """run_owned on a worker thread. `on_ready(op_pid)` runs once, in that thread, when the child has written its ready file; the
    operation is then stopped unless `auto_stop` is False."""

    def __init__(self, a: list[str], ready: Path, *, timeout_s: float = 40.0, owner: str = "t", auto_stop: bool = True,
                 grace_s: float = 1.0, confirm_s: float = 1.0, on_ready=None) -> None:
        self.stop = threading.Event()
        self.snap: list[tuple[int, float]] = []
        self.outcome: shellproc.Outcome | None = None
        self.error: BaseException | None = None
        self.elapsed = 0.0
        self.ready_seen = False

        def stop_requested() -> bool:
            if not self.ready_seen and ready.exists() and ready.read_text().strip():
                self.ready_seen = True
                ops = shellproc.active()
                if ops:
                    self.snap = tree_of(ops[0]["pid"])
                    if on_ready is not None:
                        on_ready(ops[0]["pid"])
                if auto_stop:
                    self.stop.set()
            return self.stop.is_set()

        def work() -> None:
            t = time.monotonic()
            try:
                self.outcome = shellproc.run_owned(a, cwd=TMP, timeout_s=timeout_s, owner=owner, stop_requested=stop_requested,
                                                   grace_s=grace_s, confirm_s=confirm_s)
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
            self.elapsed = time.monotonic() - t

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def join(self, seconds: float = 40.0) -> shellproc.Outcome | None:
        self.thread.join(seconds)
        return self.outcome


def run_quiet(a: list[str], **kw) -> shellproc.Outcome:
    kw.setdefault("timeout_s", 30)
    return shellproc.run_owned(a, cwd=TMP, **kw)


def main() -> int:
    started = time.perf_counter()
    check("0  test paths have no spaces (cmd /c is run unquoted)", " " not in sys.executable and " " not in str(CHILD) and " " not in BASE_PY)
    try:
        scenarios()
    finally:
        cleanup()
    for line in NOTES:
        print("NOTE:", line)
    return finish("Phase 91 shell process ownership", time.perf_counter() - started, min_assertions=100, min_scenarios=10)


def scenarios() -> None:
    # ------------------------------------------------------------------ A
    scenario("A  the child cannot execute before it is owned")
    marker, ready = paths("a1")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
    ev: list[tuple[str, bool]] = []
    o_assign, o_resume = shellproc._assign, shellproc._resume

    def slow_assign(job, proc):
        ev.append(("before_assign", marker.exists()))
        time.sleep(0.8)
        ev.append(("during_assign", marker.exists()))
        r = o_assign(job, proc)
        ev.append(("after_assign", marker.exists()))
        time.sleep(0.5)                 # assigned, not yet resumed
        ev.append(("assigned_not_resumed", marker.exists()))
        return r

    def spy_resume(proc, job=None):
        ev.append(("at_resume", marker.exists()))
        return o_resume(proc, job)

    shellproc._assign, shellproc._resume = slow_assign, spy_resume
    try:
        with Rec() as rec:
            out = run_quiet(a)
    finally:
        shellproc._assign, shellproc._resume = o_assign, o_resume
    seen = dict(ev)
    check("A1 the child was created suspended (CREATE_SUSPENDED passed to Popen)", len(rec.flags) == 1 and rec.flags[0] & 0x4, str(rec.flags))
    check("A2 the marker did not exist before the assignment began", seen.get("before_assign") is False, str(ev))
    check("A3 ... nor during a deliberately slow assignment", seen.get("during_assign") is False, str(ev))
    check("A4 ... nor right after the assignment, before the resume", seen.get("after_assign") is False and seen.get("assigned_not_resumed") is False, str(ev))
    check("A5 ... nor at the moment _resume is called", seen.get("at_resume") is False, str(ev))
    check("A6 the order was assign -> resume", [k for k, _ in ev][-1] == "at_resume" and "assigned_not_resumed" in seen, str(ev))
    check("A7 after the resume the child did run and complete", out.state == shellproc.COMPLETED and out.returncode == 0 and marker.exists() and out.stdout.strip() == "ok", str(out))

    # the same through shell.run with a cmd one-liner
    marker, ready = paths("a2")
    ev.clear()
    shellproc._assign, shellproc._resume = slow_assign, spy_resume
    try:
        r = shell.run(f'echo x > {marker}', shell="cmd", cwd=str(TMP))
    finally:
        shellproc._assign, shellproc._resume = o_assign, o_resume
    seen = dict(ev)
    check("A8 shell.run (cmd): nothing written before the resume", seen.get("before_assign") is False and seen.get("assigned_not_resumed") is False and seen.get("at_resume") is False, str(ev))
    check("A9 shell.run (cmd): the command ran after the resume and succeeded", r.ok and marker.exists(), str(getattr(r, "data", "")))

    # ------------------------------------------------------------------ B
    scenario("B  a child and all its descendants belong to the same operation's job")
    escapes = 0
    not_in_job: list[str] = []
    counts: list[int] = []
    gone_all = True
    rounds = 30
    t0 = time.monotonic()
    for i in range(rounds):
        a, marker, ready = argv("tree", "b", BASE_PY)
        jobs: list = []
        o_new = shellproc._new_job
        shellproc._new_job = lambda: jobs.append(o_new()) or jobs[-1]
        verdict: dict = {}

        def probe(op_pid: int, jobs=jobs, verdict=verdict) -> None:
            tree = tree_of(op_pid)
            verdict["tree"] = tree
            verdict["bad"] = [p for p, _ in tree if in_job(p, jobs[0]) is False]
            verdict["active"] = shellproc._active_count(jobs[0])

        try:
            op = Op(a, ready, on_ready=probe)
            out = op.join()
        finally:
            shellproc._new_job = o_new
        bad = verdict.get("bad")
        if bad is None or bad:
            escapes += 1
            not_in_job.append(f"round {i}: {bad}")
        counts.append(len(verdict.get("tree", [])))
        if out is None or out.state != shellproc.CANCELLED or not all_gone(op.snap):
            gone_all = False
    check(f"B1 {rounds} rounds: every process of the tree (child, launcher, 3 grandchildren) is in the operation's job", escapes == 0, str(not_in_job[:3]))
    check("B2 each round saw at least the child and its three grandchildren", min(counts) >= 4, str(counts))
    check("B3 the job's active-process count covered the whole tree", verdict.get("active") is not None and verdict["active"] >= 4, str(verdict.get("active")))
    check("B4 every round ended cancelled with the whole tree gone", gone_all)
    check("B5 registry empty", shellproc.active() == [])
    NOTES.append(f"B: {rounds} race-window rounds took {time.monotonic() - t0:.1f}s, tree sizes {sorted(set(counts))}")

    # a stop and a time limit still end exactly this tree, a bystander survives
    bystander = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"])
    note(bystander.pid)
    a, marker, ready = argv("tree", "b9", BASE_PY)
    op = Op(a, ready)
    out = op.join()
    check("B6 stop: cancelled, whole tree gone", out is not None and out.state == shellproc.CANCELLED and all_gone(op.snap), str(out))
    a, marker, ready = argv("tree", "b10", BASE_PY)
    holder: dict = {}
    th = threading.Thread(target=lambda: holder.update(out=run_quiet(a, timeout_s=3, grace_s=1, confirm_s=1)), daemon=True)
    th.start()
    wait_for(ready)
    snap = tree_of(int(ready.read_text().split()[0])) if ready.exists() else []
    th.join(30)
    check("B7 time limit: state timeout, whole tree gone", holder["out"].state == shellproc.TIMEOUT and len(snap) >= 4 and all_gone(snap), str(holder.get("out")))
    check("B8 the bystander was not touched", bystander.poll() is None)
    bystander.kill()
    bystander.wait()

    # ------------------------------------------------------------------ C
    scenario("C  no job object -> nothing is started")
    marker, ready = paths("c1")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
    o_new = shellproc._new_job
    shellproc._new_job = lambda: None
    try:
        with Rec() as rec:
            shellproc._new_job = lambda: None
            out = run_quiet(a)
    finally:
        shellproc._new_job = o_new
    time.sleep(1.0)
    check("C1 state not_started", out.state == shellproc.NOT_STARTED, str(out))
    check("C2 fixed reason code no_job", out.detail == "no_job", out.detail)
    check("C3 Popen was never called", rec.popens == [], str(len(rec.popens)))
    check("C4 the marker was never created (the command never ran)", not marker.exists())
    check("C5 no returncode, no output", out.returncode is None and out.stdout == "" and out.stderr == "")
    check("C6 registry empty", shellproc.active() == [])

    marker, ready = paths("c2")
    cmd = " ".join([sys.executable, str(CHILD), str(marker), str(ready), "quick"])
    shellproc._new_job = lambda: None
    try:
        r = shell.run(cmd, shell="cmd", cwd=str(TMP))
    finally:
        shellproc._new_job = o_new
    check("C7 shell.run: ok False", r.ok is False)
    check("C8 shell.run: status not_started, reason no_job", r.data.get("status") == "not_started" and r.data.get("reason") == "no_job", str(r.data))
    check("C9 shell.run: never reported as cancelled, timeout or unknown", r.data.get("status") not in ("cancelled", "timeout", "unknown_outcome"))
    check("C10 shell.run: speech says it was not run", "didn't run" in r.speech, r.speech)
    time.sleep(0.5)
    check("C11 shell.run: the command never ran", not marker.exists())

    # ------------------------------------------------------------------ D
    scenario("D  assignment failure -> the child never runs")
    marker, ready = paths("d1")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
    shellproc._assign = lambda j, p: False
    try:
        with Rec() as rec:
            out = run_quiet(a)
    finally:
        shellproc._assign = o_assign
    time.sleep(1.5)
    check("D1 state not_started", out.state == shellproc.NOT_STARTED, str(out))
    check("D2 reason code assign_failed", out.detail == "assign_failed", out.detail)
    check("D3 a suspended child had been created ...", len(rec.popens) == 1 and rec.flags[0] & 0x4, str(rec.flags))
    check("D4 ... and it never ran, even after waiting 1.5 s (no marker)", not marker.exists())
    check("D5 ... it is gone and reaped", rec.popens[0].returncode is not None and not alive(rec.popens[0].pid, rec.ctimes[0]), str(rec.popens[0].returncode))
    check("D6 registry empty", shellproc.active() == [])

    marker, ready = paths("d2")
    cmd = " ".join([sys.executable, str(CHILD), str(marker), str(ready), "quick"])
    shellproc._assign = lambda j, p: False
    try:
        r = shell.run(cmd, shell="cmd", cwd=str(TMP))
    finally:
        shellproc._assign = o_assign
    time.sleep(1.0)
    check("D7 shell.run: ok False with status not_started, reason assign_failed", r.ok is False and r.data.get("status") == "not_started" and r.data.get("reason") == "assign_failed", str(r.data))
    check("D8 shell.run: the command never ran", not marker.exists())

    # ------------------------------------------------------------------ E
    scenario("E  the suspended child, its pipes and the job are cleaned up on every failure path")
    cases: list[tuple[str, dict]] = []

    def run_case(label: str, patch_assign=None, patch_resume=None, argv_override=None, expect_exc=None):
        marker, ready = paths("e")
        a = argv_override or [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
        if patch_assign is not None:
            shellproc._assign = patch_assign
        if patch_resume is not None:
            shellproc._resume = patch_resume
        box: dict = {}
        try:
            with Rec() as rec:
                try:
                    box["out"] = run_quiet(a)
                except BaseException as exc:  # noqa: BLE001
                    box["exc"] = exc
        finally:
            shellproc._assign, shellproc._resume = o_assign, o_resume
        time.sleep(0.4)
        box.update(rec=rec, marker=marker, label=label)
        cases.append((label, box))
        return box

    def raise_kbd(job, proc):
        raise KeyboardInterrupt("simulated")

    c_assign = run_case("assign false", patch_assign=lambda j, p: False)
    c_resume = run_case("resume false", patch_resume=lambda p, job=None: False)
    c_nofile = run_case("missing executable", argv_override=[str(TMP / "does-not-exist.exe")])
    c_kbd = run_case("BaseException in assign", patch_assign=raise_kbd)

    check("E1 assign false: not_started, child never ran", c_assign["out"].state == shellproc.NOT_STARTED and not c_assign["marker"].exists())
    check("E2 resume false: not_started/resume_failed, child never ran", c_resume["out"].state == shellproc.NOT_STARTED and c_resume["out"].detail == "resume_failed" and not c_resume["marker"].exists(), str(c_resume.get("out")))
    check("E3 missing executable: the FileNotFoundError propagates (as with subprocess.run) and nothing was left", isinstance(c_nofile.get("exc"), FileNotFoundError) and shellproc.active() == [], repr(c_nofile.get("exc")))
    check("E4 BaseException while owning the suspended child is re-raised", isinstance(c_kbd.get("exc"), KeyboardInterrupt), repr(c_kbd.get("exc")))
    check("E5 ... and the child never ran", not c_kbd["marker"].exists())
    for label, box in cases:
        rec = box["rec"]
        for j in rec.jobs:
            check(f"E6 [{label}] the job handle was released", j is None or int(j) == 0, str(None if j is None else int(j)))
        for k, p in enumerate(rec.popens):
            check(f"E7 [{label}] the child is gone and reaped (returncode set, not alive)", p.returncode is not None and not alive(p.pid, rec.ctimes[k]), str(p.returncode))
            check(f"E8 [{label}] stdout/stderr pipes are closed", p.stdout.closed and p.stderr.closed)
    check("E9 missing executable: a job was created and closed, no process existed", len(c_nofile["rec"].popens) == 0 and all(j is None or int(j) == 0 for j in c_nofile["rec"].jobs), str(len(c_nofile["rec"].jobs)))
    check("E10 registry empty after every path", shellproc.active() == [], str(shellproc.active()))

    proc_self = psutil.Process()
    before = proc_self.num_handles()
    for _ in range(10):
        marker, ready = paths("e")
        a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
        shellproc._assign = lambda j, p: False
        try:
            run_quiet(a)
        finally:
            shellproc._assign = o_assign
        shellproc._resume = lambda p, job=None: False
        try:
            run_quiet(a)
        finally:
            shellproc._resume = o_resume
    time.sleep(0.5)
    after = proc_self.num_handles()
    check("E11 20 failed starts leak no handles", after - before <= 4, f"{before} -> {after}")

    # ------------------------------------------------------------------ F
    scenario("F  normal execution and completion are unchanged")
    for mode, extra in (("quick", ()), ("exit3", ()), ("unicode", ()), ("cwd", ())):
        a, marker, ready = argv(mode, "f", *extra)
        sub = TMP / "wd sub"
        sub.mkdir(exist_ok=True)
        new = shellproc.run_owned(a, cwd=sub, timeout_s=30)
        old = subprocess.run(a, capture_output=True, text=True, timeout=30, cwd=sub)
        check(f"F1 [{mode}] state completed", new.state == shellproc.COMPLETED, str(new))
        check(f"F2 [{mode}] returncode, stdout, stderr identical to the old subprocess.run",
              (new.returncode, new.stdout, new.stderr) == (old.returncode, old.stdout, old.stderr), f"{new} vs {(old.returncode, old.stdout, old.stderr)}")
    a, marker, ready = argv("exit3", "f")
    out = run_quiet(a)
    check("F3 exit code 3 is kept, with stdout and stderr", out.returncode == 3 and "out-text" in out.stdout and "err-text" in out.stderr, str(out))
    a, marker, ready = argv("unicode", "f")
    out = run_quiet(a)
    check("F4 non-ASCII text round-trips", "café" in out.stdout and "über" in out.stderr, repr(out.stdout))
    a, marker, ready = argv("cwd", "f")
    out = shellproc.run_owned(a, cwd=TMP / "wd sub", timeout_s=30)
    check("F5 the working directory is honoured", out.stdout.strip().lower().endswith("wd sub"), repr(out.stdout))
    a, marker, ready = argv("quick", "f")
    out = run_quiet(a)
    check("F6 exit 0 -> completed, no stop bookkeeping", out.returncode == 0 and out.how == "" and out.detail == "" and out.cause == "")

    r = shell.run("echo hello-cmd", shell="cmd", cwd=str(TMP))
    check("F7 shell.run via cmd: ok, output", r.ok and "hello-cmd" in r.data["stdout"] and r.data["returncode"] == 0, str(r.data))
    r = shell.run("exit 5", shell="cmd", cwd=str(TMP))
    check("F8 shell.run via cmd: exit 5 is a plain failure (no status key)", (not r.ok) and r.data["returncode"] == 5 and "status" not in r.data, str(r.data))
    r = shell.run("Write-Output hello-ps; exit 4", shell="powershell", cwd=str(TMP))
    check("F9 shell.run via powershell: exit 4 and output", (not r.ok) and r.data["returncode"] == 4 and "hello-ps" in r.data["stdout"], str(r.data))
    r = shell.run("Write-Output hello-ps", shell="powershell", cwd=str(TMP))
    check("F10 shell.run via powershell: ok", r.ok and r.data["stdout"] == "hello-ps", str(r.data))

    # an intentionally detached process keeps running after a normal completion (the job is not kill-on-close)
    a, marker, ready = argv("detach", "f", "8", BASE_PY)
    a.append(BASE_PY)                       # argv[5] of the child: the interpreter for the detached sleeper
    out = run_quiet(a)
    wait_for(ready)
    dpid = int(ready.read_text().split()[0]) if ready.exists() else 0
    note(dpid)
    time.sleep(0.5)
    check("F11 child with a DETACHED_PROCESS descendant completes normally", out.state == shellproc.COMPLETED and dpid > 0, str(out))
    check("F12 the detached process is still running after completion", dpid in SPAWNED and alive(dpid, SPAWNED[dpid]))
    if dpid in SPAWNED and alive(dpid, SPAWNED[dpid]):
        psutil.Process(dpid).kill()
    pidfile = TMP / "ps-detached.pid"
    pidfile.unlink(missing_ok=True)
    r = shell.run(f"Start-Process -FilePath '{BASE_PY}' -ArgumentList '{SLEEPER}','{pidfile}','8' -WindowStyle Hidden", shell="powershell", cwd=str(TMP))
    wait_for(pidfile, 15)
    ppid = int(pidfile.read_text()) if pidfile.exists() and pidfile.read_text().strip() else 0
    note(ppid)
    time.sleep(0.5)
    check("F13 PowerShell Start-Process: the command completes ok", r.ok, str(r.data))
    check("F14 ... and what it started is still running (not killed at completion)", ppid > 0 and ppid in SPAWNED and alive(ppid, SPAWNED[ppid]), str(ppid))
    if ppid in SPAWNED and alive(ppid, SPAWNED[ppid]):
        psutil.Process(ppid).kill()

    # ------------------------------------------------------------------ G
    scenario("G  STOP and the time limit still end the owned tree")
    a, marker, ready = argv("sleep", "g")
    op = Op(a, ready)
    out = op.join()
    check("G1 graceful stop: cancelled via Ctrl+Break, tree gone", out is not None and out.state == shellproc.CANCELLED and out.how == "graceful" and all_gone(op.snap), str(out))
    check("G2 ... and it was fast", op.elapsed < 15, f"{op.elapsed:.1f}")
    a, marker, ready = argv("ignore", "g")
    op = Op(a, ready, grace_s=0.6, confirm_s=2.0)
    out = op.join()
    check("G3 a child that ignores Ctrl+Break: forced, confirmed, tree gone", out is not None and out.state == shellproc.CANCELLED and out.how == "forced" and all_gone(op.snap), str(out))
    a, marker, ready = argv("tree", "g", BASE_PY)
    out = run_quiet(a, timeout_s=3, grace_s=1, confirm_s=1)
    check("G4 the time limit: timeout (not completed), confirmed", out.state == shellproc.TIMEOUT, str(out))
    o_ac = shellproc._active_count
    shellproc._active_count = lambda j: 1
    try:
        a, marker, ready = argv("sleep", "g")
        op = Op(a, ready, grace_s=0.5, confirm_s=0.5)
        out = op.join()
    finally:
        shellproc._active_count = o_ac
    check("G5 a tree that cannot be confirmed gone is 'unknown', never cancelled", out is not None and out.state == shellproc.UNKNOWN and out.detail == "tree_still_running" and out.cause == shellproc.CANCELLED, str(out))
    check("G6 registry empty", shellproc.active() == [])
    cleanup()

    # ------------------------------------------------------------------ H
    scenario("H  unrelated processes and other operations are not affected")
    bystander = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"])
    note(bystander.pid)
    a2, m2, r2 = argv("finite", "h", "5")
    op2 = Op(a2, r2, owner="other", auto_stop=False)
    check("H0 the concurrent operation is running", wait_for(r2) and shellproc.active() != [])
    # a failed start of one operation while another runs
    n_before = len(shellproc.active())
    marker, ready = paths("h")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
    shellproc._assign = lambda j, p: False
    try:
        failed = run_quiet(a)
    finally:
        shellproc._assign = o_assign
    shellproc._new_job = lambda: None
    try:
        failed2 = run_quiet(a)
    finally:
        shellproc._new_job = o_new
    check("H1 the failed starts were not_started", failed.state == shellproc.NOT_STARTED and failed2.state == shellproc.NOT_STARTED)
    check("H2 the running operation is still registered and running", len(shellproc.active()) == n_before and op2.thread.is_alive())
    a1, m1, r1 = argv("sleep", "h")
    op1 = Op(a1, r1)
    out1 = op1.join()
    check("H3 stopping operation 1 cancelled it", out1 is not None and out1.state == shellproc.CANCELLED)
    out2 = op2.join()
    check("H4 operation 2 (never stopped) completed by itself with its full output", out2 is not None and out2.state == shellproc.COMPLETED and "finished" in out2.stdout, str(out2))
    check("H5 the bystander (not started by shellproc) is still running", bystander.poll() is None)
    check("H6 the bystander is not in operation 1's tree", bystander.pid not in [p for p, _ in op1.snap])
    bystander.kill()
    bystander.wait()
    check("H7 registry empty", shellproc.active() == [])

    # ------------------------------------------------------------------ I
    scenario("I  _resume only reports success for exactly the situation it created")
    p = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"], creationflags=0x4)
    note(p.pid)
    real_psutil = sys.modules["psutil"]

    class FakeProc:
        def __init__(self, pid):
            pass

        def threads(self):
            return [type("T", (), {"id": 1})(), type("T", (), {"id": 2})()]

    class FakePsutil:
        Process = FakeProc

    sys.modules["psutil"] = FakePsutil
    try:
        two = shellproc._resume(p)
    finally:
        sys.modules["psutil"] = real_psutil
    check("I1 two threads in the new process -> not resumed", two is False)
    check("I2 ... and nothing ran (the child is still suspended)", psutil.Process(p.pid).status() == psutil.STATUS_STOPPED)

    k32 = __import__("ctypes").WinDLL("kernel32", use_last_error=True)
    k32.OpenThread.restype = __import__("ctypes").c_void_p
    k32.SuspendThread.argtypes = [__import__("ctypes").c_void_p]
    tid = psutil.Process(p.pid).threads()[0].id
    h = k32.OpenThread(0x2, 0, tid)
    k32.SuspendThread(h)                              # suspend count is now 2
    second = shellproc._resume(p)
    check("I3 a suspend count of 2 -> not resumed (it would still be held back)", second is False)
    third = shellproc._resume(p)                      # count is back to 1: now it is exactly the situation _resume expects
    check("I4 after the count is back to 1 the same call succeeds", third is True)
    k32.CloseHandle(h)
    p.kill()
    p.wait()
    gone = shellproc._resume(p)
    check("I5 an exited process -> False, no exception", gone is False)
    check("I6 an unknown pid -> False", shellproc._resume(type("P", (), {"pid": 0x7FFFFFF0})()) is False)

    # ------------------------------------------------------------------ J
    scenario("J  nested job objects: run_owned from inside an outer job")
    outer = win32job.CreateJobObject(None, "")
    sibling = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"], creationflags=0x4)
    nest_marker, nest_ready = paths("j")
    nest = subprocess.Popen(
        [sys.executable, str(NEST), str(REPO), str(CHILD), str(nest_marker), str(nest_ready), "tree", BASE_PY],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=0x4,
    )
    note(sibling.pid)
    note(nest.pid)
    assigned = []
    for pr in (sibling, nest):
        hproc = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pr.pid)
        try:
            win32job.AssignProcessToJobObject(outer, hproc)
            assigned.append(True)
        except Exception as exc:  # noqa: BLE001
            assigned.append(False)
            NOTES.append(f"J: could not build the outer job: {exc}")
        finally:
            hproc.Close()
    shellproc._resume(sibling)
    shellproc._resume(nest)
    check("J1 the test's outer job holds the sibling and the nested-run process", assigned == [True, True], str(assigned))
    try:
        stdout, stderr = nest.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        nest.kill()
        stdout, stderr = nest.communicate()
    try:
        result = json.loads(stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
    check("J2 the nested run produced a result", bool(result), (stdout + stderr)[-400:])
    ran = nest_marker.exists()
    if result.get("state") == shellproc.CANCELLED:
        NOTES.append("J: NESTED JOBS WORK -- run_owned inside an outer job started the child, owned its tree and stopped it (cancelled, graceful/forced: "
                     f"{result.get('how')})")
        check("J3 nested: the child ran (it was owned first) and the stop ended it as cancelled", ran and result.get("how") in ("graceful", "forced"), str(result))
        inner = [int(x) for x in nest_ready.read_text().split()] if nest_ready.exists() and nest_ready.read_text().strip() else []
        check("J4 nested: the inner tree (child + 3 grandchildren) is gone", len(inner) == 4 and not any(psutil.pid_exists(x) and x in SPAWNED and alive(x, SPAWNED[x]) for x in inner), str(inner))
    elif result.get("state") == shellproc.NOT_STARTED:
        NOTES.append(f"J: NESTED JOBS REFUSED by this Windows -- run_owned inside an outer job returned not_started/{result.get('detail')}; the child never ran")
        check("J3 nested refusal is safe: not_started and the child never ran", not ran and result.get("detail") == "assign_failed", str(result))
        check("J4 (refused) nothing to clean up", True)
    else:
        check("J3 nested: an expected outcome (cancelled or a safe not_started)", False, str(result))
        check("J4 nested: n/a", False)
    check("J5 the nested-run process itself was not killed by the inner stop (it exited normally)", nest.returncode == 0, str(nest.returncode))
    check("J6 the outer job's other member (sibling) is untouched", sibling.poll() is None)
    sibling.kill()
    sibling.wait()
    outer.Close()
    check("J7 registry empty", shellproc.active() == [])

    # ------------------------------------------------------------------ K
    scenario("K  the job is kill-on-close while the child is suspended, and disarmed before it is released")
    marker, ready = paths("k1")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "detach", "8", BASE_PY]
    seen_k: dict = {}
    o_assign, o_disarm = shellproc._assign, shellproc._disarm

    def spy_assign(job, proc):
        r = o_assign(job, proc)
        seen_k["armed_after_assign"] = kill_on_close(job)       # assigned, child still suspended
        seen_k["marker_after_assign"] = marker.exists()
        return r

    def spy_disarm(job):
        seen_k["armed_before_disarm"] = kill_on_close(job)
        seen_k["marker_at_disarm"] = marker.exists()
        r = o_disarm(job)
        seen_k["armed_after_disarm"] = kill_on_close(job)
        return r

    shellproc._assign, shellproc._disarm = spy_assign, spy_disarm
    try:
        out = run_quiet(a)
    finally:
        shellproc._assign, shellproc._disarm = o_assign, o_disarm
    wait_for(ready)
    kpid = int(ready.read_text().split()[0]) if ready.exists() and ready.read_text().strip() else 0
    note(kpid)
    time.sleep(0.5)
    check("K1 the job is kill-on-close once the suspended child is assigned", seen_k.get("armed_after_assign") is True, str(seen_k))
    check("K2 the child had not run at that point", seen_k.get("marker_after_assign") is False, str(seen_k))
    check("K3 it was still armed when _disarm was called, and the child still had not run", seen_k.get("armed_before_disarm") is True and seen_k.get("marker_at_disarm") is False, str(seen_k))
    check("K4 _disarm cleared kill-on-close", seen_k.get("armed_after_disarm") is False, str(seen_k))
    check("K5 normal completion", out.state == shellproc.COMPLETED and out.returncode == 0, str(out))
    check("K6 the DETACHED_PROCESS descendant is still alive after completion (disarmed -> closing the job did not kill it)", kpid in SPAWNED and alive(kpid, SPAWNED[kpid]), str(kpid))
    if kpid in SPAWNED and alive(kpid, SPAWNED[kpid]):
        psutil.Process(kpid).kill()

    # a patched _disarm that fails: the child is never released
    marker, ready = paths("k2")
    a = [sys.executable, str(CHILD), str(marker), str(ready), "quick"]
    shellproc._disarm = lambda job: False
    try:
        with Rec() as rec:
            out = run_quiet(a)
    finally:
        shellproc._disarm = o_disarm
    time.sleep(1.2)
    check("K7 _disarm fails -> not_started/resume_failed", out.state == shellproc.NOT_STARTED and out.detail == "resume_failed", str(out))
    check("K8 ... the marker was never created (the child was never released)", not marker.exists())
    check("K9 ... the child is gone and reaped, the job handle released", len(rec.popens) == 1 and rec.popens[0].returncode is not None and not alive(rec.popens[0].pid, rec.ctimes[0]) and all(int(j) == 0 for j in rec.jobs))

    # the crash window: a suspended, assigned child whose job handle is closed (FRIDAY died) is killed by the kernel
    marker, ready = paths("k3")
    job = shellproc._new_job()
    check("K10 a fresh job is armed", job is not None and kill_on_close(job))
    crash = subprocess.Popen([sys.executable, str(CHILD), str(marker), str(ready), "sleep"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | shellproc.CREATE_SUSPENDED)
    note(crash.pid)
    ctime = SPAWNED.get(crash.pid, 0.0)
    check("K11 the child is assigned and still suspended (no marker)", shellproc._assign(job, crash) and not marker.exists())
    time.sleep(0.3)
    job.Close()                                    # the crash: the last handle to the job goes away, nothing resumed it
    end = time.monotonic() + 5
    while crash.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    check("K12 the kernel killed the suspended child when the job handle closed", crash.poll() is not None and not alive(crash.pid, ctime), str(crash.poll()))
    check("K13 ... and it never ran", not marker.exists())
    for st in (crash.stdout, crash.stderr):
        st.close()
    check("K14 registry empty", shellproc.active() == [])


if __name__ == "__main__":
    import iso_env  # noqa: E402  (throwaway database + recovery store + key: nothing touches the real ones)

    with iso_env.isolated_runtime():
        code = main()
    sys.stdout.flush()
    os._exit(code)
