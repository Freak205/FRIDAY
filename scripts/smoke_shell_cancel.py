"""Phase 90 -- a running `shell.run` can be ended by FRIDAY's one STOP signal, and only its own process tree is touched.

Real processes, no mocks of the thing under test: every child is a throwaway `sys.executable` script in a temp dir, so nothing
here runs a destructive command, touches `data/` or the real key, or kills anything by name. A stop is delivered the way
production delivers it (`INTEL.start_goal` / `INTEL.request_cancel` -> `shell._stop_scope`) or, for the unit-level cases, through
the `stop_requested` callable of `shellproc.run_owned`. The few kernel-level seams (`_active_count`, `_new_job`, `_assign`,
`_send_graceful`, `_terminate_job`) are replaced only to force a path the machine will not produce on demand.

  A. cancel a running process: cancelled, quickly, partial output kept; graceful when the child honours Ctrl+Break
  B. cancel a process that started a child: the whole tree is gone and confirmed
  C. a stop after completion is harmless; completion keeps its real exit code
  D. repeated stop signals: one outcome, one termination attempt, no exception
  E. cleanup: registry empty, pipes closed, job handle released, no thread growth -- on every path
  F. a cancelled / timed-out / unconfirmed command is never `ok`, even when the leftover exit code is 0
  G. unrelated processes (a bystander, a concurrent operation) and unrelated goals are not affected
  H. an unconfirmed termination is reported as unknown
  I. escalation: graceful first, forced only when the child ignores Ctrl+Break; the time limit also ends the whole tree
  J. privacy: no command text in logs
  K. regressions: no hang on an untracked / unterminable tree, a failing stop callback leaves nothing running, an empty goal id
     is still stoppable (and only by its own goal), a process that finished just before the stop acted is "completed"
"""

import logging
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
from phase21_common import check, finish, scenario  # noqa: E402

from friday import shellproc  # noqa: E402
from friday.intelligence.state import INTEL  # noqa: E402
from friday.skills import shell  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="p90-"))
SENTINEL = "SENTINEL-p90-do-not-log"
SPAWNED: dict[int, float] = {}   # pid -> create_time, so a leftover is killed only if it is still THAT process

CHILD = TMP / "child.py"
CHILD.write_text(
    r'''
import os, signal, subprocess, sys, time
mode, ready = sys.argv[1], sys.argv[2]

def alive(seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(0.05)

def mark(*pids):
    with open(ready, "w") as f:
        f.write(" ".join(str(p) for p in pids))

if mode == "quick":
    print("hi", flush=True); sys.exit(3)
if mode == "finite":
    print("started", flush=True); mark(os.getpid()); alive(float(sys.argv[3])); print("finished", flush=True); sys.exit(0)
if mode == "delay3":
    print("working", flush=True); mark(os.getpid()); alive(float(sys.argv[3])); sys.exit(3)
if mode == "trap0":
    def h(*a):
        print("bye", flush=True); sys.exit(0)
    signal.signal(signal.SIGBREAK, h)
if mode == "ignore":
    signal.signal(signal.SIGBREAK, signal.SIG_IGN)
gc = None
if mode == "tree":
    gc = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"])
print("up", flush=True)
mark(os.getpid(), *( [gc.pid] if gc else [] ))
alive(120)
''',
    encoding="utf-8",
)


# A base-python (not the venv launcher) child AND grandchild that both ignore Ctrl+Break and both hold the inherited pipes.
BASE_PY = getattr(sys, "_base_executable", None) or getattr(sys, "base_executable", None) or sys.executable
HOLD = TMP / "hold.py"
HOLD.write_text(
    r'''
import signal, subprocess, sys, os
signal.signal(signal.SIGBREAK, signal.SIG_IGN)
gc = subprocess.Popen([sys.executable, "-c",
    "import signal, time\nsignal.signal(signal.SIGBREAK, signal.SIG_IGN)\nwhile True: time.sleep(0.2)"])
print("hold", flush=True)
with open(sys.argv[1], "w") as f:
    f.write(f"{os.getpid()} {gc.pid}")
import time
while True: time.sleep(0.2)
''',
    encoding="utf-8",
)


def argv(mode: str, tag: str, *extra: str) -> tuple[list[str], Path]:
    ready = TMP / f"{tag}.ready"
    ready.unlink(missing_ok=True)
    return [sys.executable, str(CHILD), mode, str(ready), *extra], ready


def cmdline(mode: str, tag: str, *extra: str) -> tuple[str, Path]:
    a, ready = argv(mode, tag, *extra)
    return " ".join(a), ready


def note(pid: int) -> None:
    try:
        SPAWNED[pid] = psutil.Process(pid).create_time()
    except psutil.Error:
        pass


def tree_of(pid: int) -> list[tuple[int, float]]:
    """The process and all its descendants right now, as (pid, create_time) -- so 'gone' cannot be fooled by pid reuse."""
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


class Op:
    """run_owned on a worker thread, stopped by a flag the test sets once the child says it is ready."""

    def __init__(self, a: list[str], ready: Path, *, timeout_s: float = 30.0, owner: str = "t", auto_stop: bool = True,
                 grace_s: float = 1.0, confirm_s: float = 1.0) -> None:
        self.ready, self.auto_stop, self.stop_calls = ready, auto_stop, 0
        self.stop = threading.Event()
        self.snap: list[tuple[int, float]] = []
        self.outcome: shellproc.Outcome | None = None
        self.error: BaseException | None = None
        self.elapsed = 0.0

        def stop_requested() -> bool:
            self.stop_calls += 1
            if self.auto_stop and not self.stop.is_set() and ready.exists() and ready.read_text().strip():
                ops = shellproc.active()
                if ops:
                    self.snap = tree_of(ops[0]["pid"])
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


def run_in_goal(command: str, ready: Path | None, *, gid: str = "goal-p90", shell_name: str = "cmd", stops: int = 1,
                delay: float = 0.0, between=None, wait_s: float = 40.0):
    """shell.run on a worker thread inside a real INTEL goal; STOP is `INTEL.request_cancel()` `stops` times once the child is ready."""
    INTEL.start_goal("p90 test", gid)
    box: dict = {}

    def work() -> None:
        t = time.monotonic()
        try:
            box["result"] = shell.run(command, shell=shell_name, cwd=str(TMP))
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        box["elapsed"] = time.monotonic() - t

    th = threading.Thread(target=work, daemon=True)
    th.start()
    snap: list[tuple[int, float]] = []
    if ready is not None:
        wait_for(ready)
    else:
        end = time.monotonic() + 15
        while not shellproc.active() and time.monotonic() < end:
            time.sleep(0.05)
    time.sleep(delay)
    ops = shellproc.active()
    if ops:
        snap = tree_of(ops[0]["pid"])
    if between is not None:
        between()
    armed = [INTEL.request_cancel() for _ in range(stops)]
    th.join(wait_s)
    INTEL.end_goal(ok=False, status="cancelled")
    box["snap"], box["armed"] = snap, armed
    return box


def fast_escalation():
    """shell.run does not expose grace/confirm; shorten them for the duration of a test."""
    orig = shellproc.run_owned

    def quick(*a, **k):
        k.setdefault("grace_s", 1.0)
        k.setdefault("confirm_s", 1.0)
        return orig(*a, **k)

    shellproc.run_owned = quick
    return lambda: setattr(shellproc, "run_owned", orig)


def main() -> int:
    started = time.perf_counter()
    check("0  test child path has no spaces (cmd /c is run unquoted)", " " not in sys.executable and " " not in str(CHILD))
    restore_fast = fast_escalation()
    try:
        scenarios()
    finally:
        restore_fast()
        INTEL.end_goal(ok=False, status="cancelled")
        cleanup()
    return finish("Phase 90 shell cancellation", time.perf_counter() - started, min_assertions=130, min_scenarios=11)


def scenarios() -> None:
    # ------------------------------------------------------------------ A
    scenario("A  cancel a running process")
    a, ready = argv("sleep", "a1")
    op = Op(a, ready, timeout_s=60)
    out = op.join()
    check("A1 the operation returned", out is not None and op.error is None, repr(op.error))
    check("A2 outcome is cancelled", out is not None and out.state == shellproc.CANCELLED, getattr(out, "state", ""))
    check("A3 finished long before the 60 s limit", op.elapsed < 15, f"{op.elapsed:.1f}s")
    check("A4 output written before the stop is preserved", out is not None and "up" in out.stdout, getattr(out, "stdout", ""))
    check("A5 graceful Ctrl+Break was enough for a child that honours it", out is not None and out.how == "graceful", getattr(out, "how", ""))
    check("A6 the child is gone", all_gone(op.snap), str(op.snap))
    check("A7 a nonzero/abnormal exit code is recorded, not hidden", out is not None and out.returncode not in (None, 0), str(getattr(out, "returncode", "")))
    check("A8 registry is empty afterwards", shellproc.active() == [], str(shellproc.active()))

    # ------------------------------------------------------------------ B
    scenario("B  cancel a process that has a child process")
    a, ready = argv("tree", "b1")
    op = Op(a, ready, timeout_s=60)
    out = op.join()
    check("B1 outcome is cancelled", out is not None and out.state == shellproc.CANCELLED, getattr(out, "state", ""))
    pids = [int(x) for x in ready.read_text().split()] if ready.exists() else []
    check("B2 the child reported a grandchild", len(pids) == 2, str(pids))
    check("B3 the snapshot saw more than one process in the tree", len(op.snap) >= 2, str(op.snap))
    check("B4 every process of the tree is gone (pid + create_time)", all_gone(op.snap), str(op.snap))
    check("B5 the grandchild pid is gone", not any(alive(p, c) for p, c in op.snap if p in pids[1:]))
    check("B6 the tree was confirmed (cancelled, not unknown)", out is not None and out.state != shellproc.UNKNOWN)
    check("B7 registry is empty afterwards", shellproc.active() == [])

    # ------------------------------------------------------------------ C
    scenario("C  a stop after completion is harmless")
    a, ready = argv("quick", "c1")
    flag = threading.Event()
    out = shellproc.run_owned(a, cwd=TMP, timeout_s=30, owner="c", stop_requested=flag.is_set)
    flag.set()   # the stop arrives only after the process finished
    check("C1 completed", out.state == shellproc.COMPLETED, out.state)
    check("C2 real exit code kept", out.returncode == 3, str(out.returncode))
    check("C3 output kept", out.stdout.strip() == "hi", repr(out.stdout))
    check("C4 nothing was terminated", out.how == "" and out.detail == "")
    check("C5 registry is empty", shellproc.active() == [])
    # a stop acted on after the process has already exited: nothing to terminate, nothing sent, nothing harmed
    proc = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    job = shellproc._new_job()
    shellproc._assign(job, proc)
    proc.communicate()
    calls = {"term": 0, "send": 0}
    o_term, o_send = shellproc._terminate_job, shellproc._send_graceful
    shellproc._terminate_job = lambda j: calls.__setitem__("term", calls["term"] + 1) or True
    shellproc._send_graceful = lambda p: calls.__setitem__("send", calls["send"] + 1) or False   # never aim a signal at a dead pid
    try:
        gone, how = shellproc._stop_tree(proc, job, grace_s=0.3, confirm_s=0.3)
    finally:
        shellproc._terminate_job, shellproc._send_graceful = o_term, o_send
        shellproc._close(job)
    check("C6 stop on an exited, tracked process confirms 'gone' without terminating", gone is True and calls["term"] == 0, f"{gone} {calls}")
    # through the skill: a goal whose command finishes, then STOP
    cmd, ready = cmdline("quick", "c2")
    box = run_in_goal(cmd, None, stops=0)
    check("C7 shell.run reports the real exit code for a completed command", box["result"].data["returncode"] == 3, str(box.get("result")))
    check("C8 a failed exit code is not ok but not a cancellation", not box["result"].ok and "status" not in box["result"].data)
    check("C9 a STOP after the goal ended arms nothing", INTEL.request_cancel() is False and not INTEL.is_cancel_requested())

    # ------------------------------------------------------------------ D
    scenario("D  repeated STOP signals")
    counts = {"send": 0, "term": 0}
    o_term, o_send = shellproc._terminate_job, shellproc._send_graceful
    shellproc._terminate_job = lambda j: counts.__setitem__("term", counts["term"] + 1) or o_term(j)
    shellproc._send_graceful = lambda p: counts.__setitem__("send", counts["send"] + 1) or o_send(p)
    try:
        a, ready = argv("sleep", "d1")
        op = Op(a, ready, timeout_s=60)
        out = op.join()
    finally:
        shellproc._terminate_job, shellproc._send_graceful = o_term, o_send
    check("D1 a flag that stays true gives one clean outcome", out is not None and out.state == shellproc.CANCELLED and op.error is None)
    check("D2 exactly one graceful attempt", counts["send"] == 1, str(counts))
    check("D3 no forced termination was needed", counts["term"] == 0, str(counts))
    cmd, ready = cmdline("sleep", "d2")
    counts.update(send=0, term=0)
    shellproc._terminate_job = lambda j: counts.__setitem__("term", counts["term"] + 1) or o_term(j)
    shellproc._send_graceful = lambda p: counts.__setitem__("send", counts["send"] + 1) or o_send(p)
    try:
        box = run_in_goal(cmd, ready, stops=25)
    finally:
        shellproc._terminate_job, shellproc._send_graceful = o_term, o_send
    r = box.get("result")
    check("D4 25 INTEL stops during one command: no exception", "error" not in box, repr(box.get("error")))
    check("D5 the first stop armed the goal, the rest are harmless repeats", all(box["armed"]), str(box["armed"][:3]))
    check("D6 shell.run reports cancelled", r is not None and r.data.get("status") == "cancelled", str(getattr(r, "data", "")))
    check("D7 one graceful attempt for 25 stops", counts["send"] == 1, str(counts))
    check("D8 STOP after the outcome (goal ended) is a no-op that raises nothing", INTEL.request_cancel() is False)
    check("D9 tree gone", all_gone(box["snap"]))

    # ------------------------------------------------------------------ E
    scenario("E  cleanup and handle release on every path")
    popens: list[subprocess.Popen] = []
    jobs: list = []

    class RecPopen(subprocess.Popen):
        def __init__(self, *aa, **kk):
            super().__init__(*aa, **kk)
            popens.append(self)

    o_new = shellproc._new_job

    def rec_job():
        j = o_new()
        jobs.append(j)
        return j

    orig_popen = subprocess.Popen
    subprocess.Popen = RecPopen
    shellproc._new_job = rec_job
    threads_before = threading.active_count()
    o_ac = shellproc._active_count
    paths: dict[str, shellproc.Outcome] = {}
    try:
        a, r = argv("quick", "e1");   paths["completed"] = shellproc.run_owned(a, cwd=TMP, timeout_s=30)
        a, r = argv("sleep", "e2");   paths["cancelled"] = Op(a, r).join()
        a, r = argv("tree", "e3");    paths["timeout"] = shellproc.run_owned(a, cwd=TMP, timeout_s=2.5, grace_s=1, confirm_s=1)
        a, r = argv("sleep", "e4")
        shellproc._active_count = lambda j: 1
        try:
            paths["unknown"] = Op(a, r).join()
        finally:
            shellproc._active_count = o_ac
    finally:
        subprocess.Popen = orig_popen
        shellproc._new_job = o_new
    check("E1 all four paths produced the expected state",
          [paths[k].state for k in ("completed", "cancelled", "timeout", "unknown")] == ["completed", "cancelled", "timeout", "unknown"],
          str({k: v.state for k, v in paths.items()}))
    check("E2 four operations recorded", len(popens) == 4 and len(jobs) == 4, f"{len(popens)} {len(jobs)}")
    check("E3 stdout pipe closed on every path", all(p.stdout.closed for p in popens))
    check("E4 stderr pipe closed on every path", all(p.stderr.closed for p in popens))
    check("E5 every process handle was reaped (returncode known)", all(p.returncode is not None for p in popens), str([p.returncode for p in popens]))
    check("E6 every job handle was released", all(j is not None and int(j) == 0 for j in jobs), str([None if j is None else int(j) for j in jobs]))
    check("E7 registry empty", shellproc.active() == [], str(shellproc.active()))
    time.sleep(0.5)
    check("E8 no thread growth across the four paths", threading.active_count() <= threads_before, f"{threads_before} -> {threading.active_count()}")
    cleanup()

    # ------------------------------------------------------------------ F
    scenario("F  a stopped, timed-out or unconfirmed command is never ok")
    cmd, ready = cmdline("trap0", "f1")
    box = run_in_goal(cmd, ready)
    r = box["result"]
    check("F1 a child that exits 0 after the stop (returncode 0, 'bye' printed) ...", r.data.get("returncode") == 0 and "bye" in r.data.get("stdout", ""), str(r.data))
    check("F2 ... is still reported as cancelled", r.data.get("status") == "cancelled", str(r.data))
    check("F3 ... and is not ok", r.ok is False)
    check("F4 speech does not claim success or completion", "stopped" in r.speech.lower() and "done" not in r.speech.lower(), r.speech)
    check("F5 tree gone", all_gone(box["snap"]))

    old_timeout = shell._TIMEOUT
    shell._TIMEOUT = 3
    try:
        cmd, ready = cmdline("tree", "f2")
        box = run_in_goal(cmd, None, stops=0)
    finally:
        shell._TIMEOUT = old_timeout
    r = box["result"]
    check("F6 timeout: not ok", r.ok is False)
    check("F7 timeout: status timeout", r.data.get("status") == "timeout", str(r.data))
    check("F8 timeout: speech says it timed out", "timed out" in r.speech, r.speech)
    check("F9 timeout: the whole tree is gone (the old direct-child kill left the grandchild)", all_gone(box["snap"]), str(box["snap"]))

    o_ac = shellproc._active_count
    shellproc._active_count = lambda j: 1
    try:
        cmd, ready = cmdline("sleep", "f3")
        box = run_in_goal(cmd, ready)
    finally:
        shellproc._active_count = o_ac
    r = box["result"]
    check("F10 unconfirmed: not ok", r.ok is False)
    check("F11 unconfirmed: status unknown_outcome, not cancelled", r.data.get("status") == "unknown_outcome", str(r.data))
    cleanup()

    # ------------------------------------------------------------------ G
    scenario("G  unrelated processes and goals are not affected")
    bystander = subprocess.Popen([sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"])
    note(bystander.pid)
    a1, r1 = argv("sleep", "g1")
    a2, r2 = argv("finite", "g2", "3")
    op2 = Op(a2, r2, timeout_s=30, owner="other", auto_stop=False)
    op1 = Op(a1, r1, timeout_s=60, owner="mine")
    out1, out2 = op1.join(), op2.join()
    check("G1 operation 1 was cancelled", out1 is not None and out1.state == shellproc.CANCELLED)
    check("G2 the concurrent operation (stop never set) completed on its own", out2 is not None and out2.state == shellproc.COMPLETED and out2.returncode == 0, str(out2))
    check("G3 ... with its full output", out2 is not None and "finished" in out2.stdout)
    check("G4 the bystander (not started by shellproc) is still running", bystander.poll() is None)
    check("G5 the bystander is not in operation 1's snapshot", bystander.pid not in [p for p, _ in op1.snap])
    check("G6 registry empty", shellproc.active() == [])

    # a flag armed for a DIFFERENT goal does not end a command owned by the first goal
    cmd, ready = cmdline("finite", "g3", "3")
    state: dict = {}

    def switch_goal() -> None:
        INTEL.end_goal(ok=True)
        INTEL.start_goal("another request", "goal-OTHER")
        state["armed"] = INTEL.request_cancel()

    box = run_in_goal(cmd, ready, gid="goal-first", stops=0, between=switch_goal)
    r = box["result"]
    check("G7 a different goal was running and was stopped", state.get("armed") is True)
    check("G8 the first goal's command was NOT cancelled by it", r.ok is True and "status" not in r.data, str(r.data))
    check("G9 its output is complete", "finished" in r.data.get("stdout", ""))
    # a stop requested while idle arms nothing, so it cannot cancel a later command
    INTEL.end_goal(ok=True)
    check("G10 idle: request_cancel returns False", INTEL.request_cancel() is False)
    check("G11 idle: nothing was armed", not INTEL.is_cancel_requested())
    cmd, ready = cmdline("finite", "g4", "1")
    box = run_in_goal(cmd, ready, gid="goal-later", stops=0)
    check("G12 a command started in a later goal runs to completion", box["result"].ok is True, str(box["result"].data))
    INTEL.end_goal(ok=True)
    check("G13 with no goal running, _stop_scope gives no stop signal", shell._stop_scope() == (None, None))
    check("G14 the bystander is still running after everything", bystander.poll() is None)
    bystander.kill()
    bystander.wait()

    # ------------------------------------------------------------------ H
    scenario("H  termination that cannot be confirmed is 'unknown'")
    o_ac = shellproc._active_count
    shellproc._active_count = lambda j: 1
    try:
        a, ready = argv("sleep", "h1")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join()
    finally:
        shellproc._active_count = o_ac
    check("H1 tree still reported running -> unknown", out is not None and out.state == shellproc.UNKNOWN, getattr(out, "state", ""))
    check("H2 detail is a fixed code", out is not None and out.detail == "tree_still_running", getattr(out, "detail", ""))
    check("H3 the cause (cancelled) is kept", out is not None and out.cause == shellproc.CANCELLED)
    check("H4 unknown is not reported as cancelled or completed", out is not None and out.state not in (shellproc.CANCELLED, shellproc.COMPLETED))
    check("H5 the registry is cleaned up even so", shellproc.active() == [])

    # Phase 91: ownership is established BEFORE the child can run, so "no job" / "assignment failed" are no longer an
    # "unknown" end of a running process: the command is simply never started (see smoke_shell_ownership for the full set).
    o_new = shellproc._new_job
    shellproc._new_job = lambda: None
    try:
        a, ready = argv("sleep", "h2")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join(10.0)
    finally:
        shellproc._new_job = o_new
    check("H6 no job object -> the command is not started (not 'unknown', not running)", out is not None and out.state == shellproc.NOT_STARTED, getattr(out, "state", ""))
    check("H7 detail no_job", out is not None and out.detail == "no_job", getattr(out, "detail", ""))
    check("H7b the child never ran (its ready file was never written)", not ready.exists())

    o_as = shellproc._assign
    shellproc._assign = lambda j, p: False
    try:
        a, ready = argv("sleep", "h3")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join(10.0)
    finally:
        shellproc._assign = o_as
    check("H8 assignment to the job fails -> not started", out is not None and out.state == shellproc.NOT_STARTED and out.detail == "assign_failed", str(out))
    check("H8b the child never ran", not ready.exists())

    o_ac = shellproc._active_count
    shellproc._active_count = lambda j: None
    try:
        a, ready = argv("sleep", "h4")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join()
    finally:
        shellproc._active_count = o_ac
    check("H9 the job cannot be queried -> unknown", out is not None and out.state == shellproc.UNKNOWN, getattr(out, "state", ""))

    # shell.run end to end for an unconfirmed termination (the job reports a process still active)
    restore = shellproc.run_owned
    shellproc.run_owned = lambda *aa, **kk: restore(*aa, **{**kk, "grace_s": 0.5, "confirm_s": 0.5})
    o_ac = shellproc._active_count
    shellproc._active_count = lambda j: 1
    try:
        cmd, ready = cmdline("sleep", "h5")
        box = run_in_goal(cmd, ready)
    finally:
        shellproc._active_count = o_ac
        shellproc.run_owned = restore
    r = box["result"]
    check("H10 shell.run: ok False", r.ok is False)
    check("H11 shell.run: status unknown_outcome", r.data.get("status") == "unknown_outcome", str(r.data))
    check("H12 shell.run: speech says it could not confirm", "couldn't confirm" in r.speech, r.speech)
    check("H13 shell.run: never says cancelled or stopped successfully", r.data.get("status") != "cancelled" and "I stopped that command" not in r.speech)
    check("H14 shell.run: reason is a fixed code", r.data.get("reason") == "tree_still_running", str(r.data))
    cleanup()

    # ------------------------------------------------------------------ I
    scenario("I  escalation and the time limit")
    a, ready = argv("ignore", "i1")
    op = Op(a, ready, timeout_s=60, grace_s=0.6, confirm_s=2.0)
    out = op.join()
    check("I1 a child that ignores Ctrl+Break is still ended", out is not None and out.state == shellproc.CANCELLED, str(out))
    check("I2 ... by the forced step", out is not None and out.how == "forced", getattr(out, "how", ""))
    check("I3 ... and confirmed gone", all_gone(op.snap))
    check("I4 the grace period was waited (>= 0.6 s) before forcing", op.elapsed >= 0.6)

    a, ready = argv("trap0", "i2")
    op = Op(a, ready, timeout_s=60)
    out = op.join()
    check("I5 a child that handles Ctrl+Break ends gracefully", out is not None and out.how == "graceful" and out.state == shellproc.CANCELLED, str(out))
    check("I6 ... and its own farewell output is captured", out is not None and "bye" in out.stdout)

    # graceful cannot be sent at all -> forced is the bounded fallback
    o_send = shellproc._send_graceful
    shellproc._send_graceful = lambda p: False
    try:
        a, ready = argv("sleep", "i3")
        op = Op(a, ready, timeout_s=60)
        out = op.join()
    finally:
        shellproc._send_graceful = o_send
    check("I7 graceful unavailable -> forced, still confirmed", out is not None and out.state == shellproc.CANCELLED and out.how == "forced", str(out))

    a, ready = argv("tree", "i4")
    out = shellproc.run_owned(a, cwd=TMP, timeout_s=3, grace_s=1, confirm_s=1)
    ops_seen = tree_of(int(ready.read_text().split()[0])) if ready.exists() else []
    gc_pids = [int(x) for x in ready.read_text().split()] if ready.exists() else []
    check("I8 time limit: state timeout (tree confirmed gone)", out.state == shellproc.TIMEOUT, str(out))
    check("I9 time limit: child and grandchild both gone", len(gc_pids) == 2 and not any(psutil.pid_exists(p) and p in SPAWNED and alive(p, SPAWNED[p]) for p in gc_pids), str(gc_pids))
    check("I10 time limit: never reported completed", out.state != shellproc.COMPLETED)

    # PowerShell: the default shell of shell.run
    box = run_in_goal("Start-Sleep -Seconds 60", None, shell_name="powershell", delay=2.0)
    r = box["result"]
    check("I11 powershell command cancelled", r.data.get("status") == "cancelled", str(r.data))
    check("I12 powershell command not ok", r.ok is False)
    check("I13 powershell tree gone", all_gone(box["snap"], 5.0), str(box["snap"]))
    check("I14 returned long before 60 s", box["elapsed"] < 20, f"{box['elapsed']:.1f}")

    # ------------------------------------------------------------------ J
    scenario("J  nothing sensitive in logs")
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    h = Grab(level=logging.DEBUG)
    targets = [logging.getLogger(), shellproc.log, shell.log]
    prior = [(t, t.level) for t in targets]
    for t in targets:
        t.addHandler(h)
        t.setLevel(logging.DEBUG)
    try:
        a, ready = argv("sleep", "j1", SENTINEL)
        Op(a, ready, grace_s=0.5, confirm_s=0.5).join()
        o_ac = shellproc._active_count
        shellproc._active_count = lambda j: 1
        try:
            a, ready = argv("sleep", "j2", SENTINEL)
            Op(a, ready, grace_s=0.5, confirm_s=0.5).join()       # the path that logs a warning
        finally:
            shellproc._active_count = o_ac
        cmd, ready = cmdline("sleep", "j3", SENTINEL)
        run_in_goal(cmd, ready)
    finally:
        for t, lvl in prior:
            t.removeHandler(h)
            t.setLevel(lvl)
    check("J1 log output was captured for the unconfirmed path", any("not confirmed" in m for m in records), str(records)[:200])
    check("J2 the command text (sentinel) is in no log record", not any(SENTINEL in m for m in records))
    check("J3 no executable path or script path in the records", not any(str(CHILD) in m or sys.executable in m for m in records))
    cleanup()
    check("J4 registry empty at the end", shellproc.active() == [])
    scenario_k()


def hold_argv(tag: str) -> tuple[list[str], Path]:
    ready = TMP / f"{tag}.ready"
    ready.unlink(missing_ok=True)
    return [BASE_PY, str(HOLD), str(ready)], ready


def note_ready(ready: Path) -> None:
    if ready.exists():
        for tok in ready.read_text().split():
            note(int(tok))


def scenario_k() -> None:
    scenario("K  regressions: hang on an untracked tree, a failing stop callback, an empty goal id, a finish just before the stop")

    # ---- K1: nothing may hang when the tree cannot be owned or terminated
    check("K0 the base interpreter exists and has no spaces (cmd /c is unquoted)", Path(BASE_PY).exists() and " " not in BASE_PY, BASE_PY)
    # Phase 91: with no job the child is never started, so there is nothing that could hold a pipe and nothing to hang on.
    o_new = shellproc._new_job
    shellproc._new_job = lambda: None
    try:
        a, ready = hold_argv("k1a")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join(10.0)
        check("K1 no job: run_owned RETURNS within 10 s", not op.thread.is_alive(), f"{op.elapsed:.1f}s")
        check("K2 ... state not_started", out is not None and out.state == shellproc.NOT_STARTED, str(out))
        check("K3 ... detail no_job", out is not None and out.detail == "no_job", getattr(out, "detail", ""))
        check("K4 ... no exception escaped", op.error is None, repr(op.error))
        check("K5 ... registry cleaned up and the child never ran", shellproc.active() == [] and not ready.exists())

        (TMP / "k1b.ready").unlink(missing_ok=True)
        cmd = f"{BASE_PY} {HOLD} {TMP / 'k1b.ready'}"
        INTEL.start_goal("p90 test", "goal-k1b")
        try:
            r = shell.run(cmd, shell="cmd", cwd=str(TMP))
        finally:
            INTEL.end_goal(ok=False, status="cancelled")
        note_ready(TMP / "k1b.ready")
        check("K6 shell.run with no job returns (not hung)", r is not None)
        check("K7 ... ok False", r is not None and r.ok is False)
        check("K8 ... status not_started with reason no_job", r is not None and r.data.get("status") == "not_started" and r.data.get("reason") == "no_job", str(getattr(r, "data", "")))
    finally:
        shellproc._new_job = o_new
        cleanup()

    o_term = shellproc._terminate_job
    shellproc._terminate_job = lambda j: False
    try:
        a, ready = hold_argv("k1c")
        op = Op(a, ready, timeout_s=60, grace_s=0.5, confirm_s=0.5)
        out = op.join(10.0)
        note_ready(ready)
        check("K9 tracked but TerminateJobObject fails: run_owned RETURNS within 10 s", not op.thread.is_alive(), f"{op.elapsed:.1f}s")
        check("K10 ... state unknown (the grandchild is still in the job)", out is not None and out.state == shellproc.UNKNOWN, str(out))
        check("K11 ... detail tree_still_running", out is not None and out.detail == "tree_still_running", getattr(out, "detail", ""))
        check("K12 ... registry cleaned up", shellproc.active() == [])
        cleanup()
        cmd = f"{BASE_PY} {HOLD} {TMP / 'k1d.ready'}"
        (TMP / "k1d.ready").unlink(missing_ok=True)
        box = run_in_goal(cmd, TMP / "k1d.ready", wait_s=15.0)
        note_ready(TMP / "k1d.ready")
        r = box.get("result")
        check("K13 shell.run with a failing terminate returns, ok False, unknown_outcome",
              r is not None and r.ok is False and r.data.get("status") == "unknown_outcome", str(getattr(r, "data", box.get("error"))))
    finally:
        shellproc._terminate_job = o_term
        cleanup()
    check("K14 every spawned hold process was cleaned up by recorded pid", not any(alive(p, c) for p, c in SPAWNED.items()))

    # ---- K2: an exception from the stop callback must not leave the process running
    for label, untracked in (("tracked", False),):   # Phase 91: an untracked run can no longer start
        a, ready = argv("tree", f"k2{label}")
        calls = {"n": 0}
        snap: list[tuple[int, float]] = []

        def boom() -> bool:
            calls["n"] += 1
            if calls["n"] >= 2 and ready.exists() and ready.read_text().strip():
                ops = shellproc.active()
                if ops:
                    snap.extend(tree_of(ops[0]["pid"]))
                raise RuntimeError("stop callback failed")
            return False

        box: dict = {}

        def work() -> None:
            try:
                box["out"] = shellproc.run_owned(a, cwd=TMP, timeout_s=60, stop_requested=boom, grace_s=0.5, confirm_s=0.5)
            except BaseException as exc:  # noqa: BLE001
                box["exc"] = exc

        o_new = shellproc._new_job
        if untracked:
            shellproc._new_job = lambda: None
        th = threading.Thread(target=work, daemon=True)
        t0 = time.monotonic()
        th.start()
        th.join(15.0)
        shellproc._new_job = o_new
        check(f"K15 {label}: run_owned returned (no hang)", not th.is_alive(), f"{time.monotonic() - t0:.1f}s")
        check(f"K16 {label}: the RuntimeError was re-raised", isinstance(box.get("exc"), RuntimeError), repr(box))
        direct = [(p, c) for p, c in snap[:1]]
        if untracked:
            check(f"K17 {label}: the direct child is gone", bool(direct) and all_gone(direct), str(snap))
        else:
            check(f"K17 {label}: child and grandchild are gone", len(snap) >= 2 and all_gone(snap), str(snap))
        check(f"K18 {label}: registry empty", shellproc.active() == [])
        cleanup()

    # ---- K3: a goal with an empty id is still THE running goal
    INTEL.start_goal("x", "")
    owner, fn = shell._stop_scope()
    check("K19 empty goal id: owner is '' (not None), stop callable present", owner == "" and fn is not None, repr((owner, fn)))
    check("K20 ... callable is false before STOP", fn() is False)
    check("K21 ... STOP arms the running goal", INTEL.request_cancel() is True)
    check("K22 ... callable is true after STOP", fn() is True)
    INTEL.end_goal(ok=False, status="cancelled")
    check("K23 ... false again once the goal is over", fn() is False)
    cmd, ready = cmdline("sleep", "k3")
    box = run_in_goal(cmd, ready, gid="")
    r = box["result"]
    check("K24 a long command in an empty-id goal is actually cancelled", r.data.get("status") == "cancelled" and r.ok is False, str(r.data))
    check("K25 ... quickly, and its tree is gone", box["elapsed"] < 20 and all_gone(box["snap"]), f"{box['elapsed']:.1f}")
    cmd, ready = cmdline("finite", "k3b", "3")
    state: dict = {}

    def other_goal() -> None:
        INTEL.end_goal(ok=True)
        INTEL.start_goal("another", "goal-OTHER")
        state["armed"] = INTEL.request_cancel()

    box = run_in_goal(cmd, ready, gid="", stops=0, between=other_goal)
    r = box["result"]
    check("K26 a flag armed for a DIFFERENT goal does not cancel the empty-id goal's command",
          state.get("armed") is True and r.ok is True and "status" not in r.data, str(r.data))
    INTEL.end_goal(ok=True)

    # ---- K4: the process finished just before the stop acted
    a, ready = argv("delay3", "k4", "1.0")
    counts = {"send": 0, "term": 0, "stop": 0}
    o_term, o_send = shellproc._terminate_job, shellproc._send_graceful
    shellproc._terminate_job = lambda j: counts.__setitem__("term", counts["term"] + 1) or o_term(j)
    shellproc._send_graceful = lambda p: counts.__setitem__("send", counts["send"] + 1) or o_send(p)

    def stop_after_exit() -> bool:
        counts["stop"] += 1
        ops = shellproc.active()
        if ops and counts["stop"] == 1:
            try:
                psutil.Process(ops[0]["pid"]).wait(timeout=10)    # the child has exited by the time the stop "acts"
            except psutil.Error:
                pass
            return True
        return counts["stop"] > 1

    try:
        out = shellproc.run_owned(a, cwd=TMP, timeout_s=30, stop_requested=stop_after_exit, grace_s=0.5, confirm_s=0.5)
    finally:
        shellproc._terminate_job, shellproc._send_graceful = o_term, o_send
    check("K27 the stop check really ran", counts["stop"] >= 1, str(counts))
    check("K28 a process that already finished is COMPLETED, not cancelled", out.state == shellproc.COMPLETED, str(out))
    check("K29 ... with its real exit code", out.returncode == 3, str(out.returncode))
    check("K30 ... and its output", "working" in out.stdout)
    check("K31 no graceful signal was sent and nothing terminated", counts["send"] == 0 and counts["term"] == 0, str(counts))
    check("K32 not marked as stopped/escalated", out.how == "" and out.detail == "")
    check("K33 registry empty", shellproc.active() == [])


if __name__ == "__main__":
    import iso_env  # noqa: E402  (throwaway database + recovery store + key: nothing touches the real ones)

    with iso_env.isolated_runtime():
        code = main()
    sys.stdout.flush()
    os._exit(code)
