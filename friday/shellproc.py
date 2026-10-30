"""Operation-owned shell processes (Phase 90): start one command, own its whole process tree, and stop exactly that tree.

Before this phase `shell.run` was `subprocess.run` in a worker thread. A STOP only armed the shared `INTEL` flag; the
orchestrator cannot interrupt a worker thread, so it announced "worker-thread tool, waiting for it to finish" and let the
command run on (up to the 60 s step timeout). A timeout killed only the direct child, never what it had started, and left the
pipe reader waiting on any descendant that still held the pipe. The process was not tied to a goal, and nothing recorded which
processes an operation owned.

What an operation owns here:

  * its own process group (`CREATE_NEW_PROCESS_GROUP`), so a console Ctrl+Break can be aimed at it and nothing else; and
  * a Windows job object the child is assigned to BEFORE it executes a single instruction (Phase 91, below). Every descendant it
    starts joins the job, so "the operation's tree" is a kernel-tracked set, not a guess from names or parent ids. Nothing else is
    ever in that job.

Starting, in this order (Phase 91; Phase 90 assigned the job after the child was already running, so a child could start a
descendant first and that descendant would never be in the job):

  1. create the operation's job object, armed kill-on-close (so a crash of FRIDAY while the child is suspended takes the child
     with it) -- if that fails, nothing is started;
  2. create the child SUSPENDED (`CREATE_SUSPENDED`): its initial thread has not run any code, not even the loader;
  3. assign it to the job -- if that fails, the suspended child is killed without ever having run;
  4. switch the job's kill-on-close off, then resume the child's one initial thread (checked: it is the only thread, and it had been
     suspended exactly once) -- if either fails, the child is killed, still not having run;
  5. only now can it run, and everything it starts inherits the job.

A failure at any step ends in the "not_started" outcome with a fixed reason code, and the job, the process handle and the pipes are
released. Nested jobs (FRIDAY itself inside a job) are handled by Windows 8+ job nesting; where Windows refuses the assignment the
command is simply not run -- never run unowned.

Stopping, bounded and in this order (nothing is ever killed by image name, and `taskkill` is never used):

  1. graceful: Ctrl+Break to the operation's process group; wait up to `grace_s` for the job to empty;
  2. forced:   `TerminateJobObject` on the operation's own job -- only if step 1 did not empty it (or could not be sent);
  3. confirm:  wait up to `confirm_s` for the job to report zero active processes.

If step 3 cannot show the tree is gone (the job could not be queried, or a process outlived the terminate) the outcome
is "unknown", never "cancelled". A process that finished on its own before the stop is simply "completed": a stop that arrives
after the fact changes nothing and harms nothing, and a repeated stop is the same flag read again -- the worker thread that owns
the process handles it once, then leaves.

Once released, the job is NOT kill-on-close: a command that intentionally leaves something running (`start notepad`)
keeps doing so after it completes normally, exactly as before.

Only the thread that started the process ever stops it; the caller supplies `stop_requested()`, scoped by the caller to the
goal that owns the operation (see `friday.skills.shell`). Output is returned as text and never logged. Pipes and the job handle
are released on every path.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from friday.log import get

log = get(__name__)

POLL_S = 0.1
GRACE_S = 3.0
CONFIRM_S = 3.0

COMPLETED = "completed"   # the process ended by itself (any exit code)
TIMEOUT = "timeout"       # the time limit ran out and the tree was confirmed gone
CANCELLED = "cancelled"   # a stop request ended it and the tree was confirmed gone
UNKNOWN = "unknown"       # a stop/timeout was acted on but the tree could not be confirmed gone
NOT_STARTED = "not_started"  # ownership could not be established before the child could run, so it never ran

CREATE_SUSPENDED = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002

_WIN = sys.platform == "win32"
if _WIN:
    import win32api
    import win32con
    import win32job


@dataclass
class Outcome:
    state: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    how: str = ""          # "", "graceful" or "forced": how a stop/timeout ended the tree
    detail: str = ""       # a fixed reason code for UNKNOWN / NOT_STARTED (never argument text)
    cause: str = ""        # for UNKNOWN: what was being acted on, CANCELLED or TIMEOUT


@dataclass
class _Op:
    op_id: int
    pid: int
    owner: str | None
    started: float = field(default_factory=time.monotonic)


_LOCK = threading.Lock()
_ACTIVE: dict[int, _Op] = {}
_NEXT = [0]


def active() -> list[dict]:
    """The operations that have started and not yet been cleaned up (pid, owner, age) -- for shutdown and tests."""
    with _LOCK:
        return [{"op": o.op_id, "pid": o.pid, "owner": o.owner, "age_s": round(time.monotonic() - o.started, 3)} for o in _ACTIVE.values()]


# -- the Windows seam: every kernel call lives here so tests can replace one function -----------------------------------------

def _set_kill_on_close(job, on: bool) -> bool:
    try:
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        flags = info["BasicLimitInformation"]["LimitFlags"]
        flags = (flags | win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) if on else (flags & ~win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info["BasicLimitInformation"]["LimitFlags"] = flags
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return True
    except Exception:  # noqa: BLE001
        return False


def _new_job():
    """A job object for one operation, or None. It starts kill-on-close, so if FRIDAY dies while the child is still suspended the
    child dies with it; `_disarm` turns that off just before the child is released (see the module docstring)."""
    if not _WIN:
        return None
    try:
        job = win32job.CreateJobObject(None, "")
    except Exception:  # noqa: BLE001
        log.warning("shell operation: job object unavailable")
        return None
    if not _set_kill_on_close(job, True):
        _close(job)
        log.warning("shell operation: job object could not be armed")
        return None
    return job


def _disarm(job) -> bool:
    """Stop the job killing its processes when its handle closes: from here on a command that deliberately leaves something running
    keeps doing so after it completes, exactly as before."""
    return _set_kill_on_close(job, False)


def _assign(job, proc: subprocess.Popen) -> bool:
    """Put the suspended child in the job. False when that is not possible."""
    if job is None:
        return False
    try:
        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, proc.pid)
        try:
            win32job.AssignProcessToJobObject(job, h)
        finally:
            h.Close()
        return True
    except Exception:  # noqa: BLE001
        log.warning("shell operation: could not assign the process to its job")
        return False


def _active_count(job) -> int | None:
    """Processes currently in the job, or None when it cannot be read."""
    if job is None:
        return None
    try:
        return int(win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"])
    except Exception:  # noqa: BLE001
        return None


def _send_graceful(proc: subprocess.Popen) -> bool:
    """Ctrl+Break to this operation's process group (the pid of a group leader). False if it could not be sent."""
    try:
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT if _WIN else signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False


def _terminate_job(job) -> bool:
    if job is None:
        return False
    try:
        win32job.TerminateJobObject(job, 1)
        return True
    except Exception:  # noqa: BLE001
        return False


def _close(job) -> None:
    if job is not None:
        try:
            job.Close()
        except Exception:  # noqa: BLE001
            pass


def _resume(proc: subprocess.Popen, job=None) -> bool:
    """Let a child that was created suspended run: resume its one initial thread. True only if it really was the only thread and it had
    been suspended exactly once (ResumeThread returns the previous suspend count), i.e. the child is now running and nothing else is
    still holding it back. The job's kill-on-close is switched off immediately before the thread is released (if that fails, the child
    is not released). Anything else is False and the caller kills the child."""
    try:
        import psutil

        threads = psutil.Process(proc.pid).threads()
        if len(threads) != 1:
            return False
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenThread.restype = ctypes.c_void_p
        k32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.ResumeThread.restype = ctypes.c_uint32
        k32.ResumeThread.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenThread(_THREAD_SUSPEND_RESUME, 0, threads[0].id)
        if not handle:
            return False
        try:
            if job is not None and not _disarm(job):
                return False
            return k32.ResumeThread(handle) == 1
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def _discard(proc: subprocess.Popen, job) -> None:
    """Undo a start that could not be completed: the child never ran (or, if it somehow did, it is in the job), so ending the job and
    the process ends everything; then release the process handle, the pipes (no reader was ever started, so closing is immediate) and
    the job. Never raises."""
    _terminate_job(job)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=CONFIRM_S)
    except Exception:  # noqa: BLE001
        pass
    _release(proc, inline=True)
    _close(job)


# -- stopping ---------------------------------------------------------------------------------------------------------------

def _tree_gone(proc: subprocess.Popen, job) -> bool | None:
    """True / False when the tree is known gone / still there; None when that cannot be known."""
    n = _active_count(job)
    if n is None:
        return None
    return n == 0 and proc.poll() is not None


def _stop_tree(proc: subprocess.Popen, job, *, grace_s: float, confirm_s: float) -> tuple[bool | None, str]:
    """Graceful, then forced, then confirm. Returns (gone, how)."""
    how = ""
    if _send_graceful(proc):
        how = "graceful"
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            if _tree_gone(proc, job):
                break
            time.sleep(POLL_S / 2)
    gone = _tree_gone(proc, job)
    if gone is not True:
        if not _terminate_job(job):
            try:
                proc.kill()           # the job could not be ended: at least the direct child (the outcome stays unconfirmed)
            except Exception:  # noqa: BLE001
                pass
        how = "forced"
        end = time.monotonic() + confirm_s
        while time.monotonic() < end:
            gone = _tree_gone(proc, job)
            if gone is not False:
                break
            time.sleep(POLL_S / 2)
    return gone, how


def _drain(proc: subprocess.Popen, wait_s: float) -> tuple[str, str, bool]:
    """Whatever the process wrote, without waiting on a pipe a surviving descendant might still hold open.
    The last item says whether the pipes were read to the end (so closing them cannot block)."""
    try:
        out, err = proc.communicate(timeout=wait_s)
        done = True
    except subprocess.TimeoutExpired as exc:
        out, err, done = exc.stdout, exc.stderr, False
    except Exception:  # noqa: BLE001
        out, err, done = "", "", False
    return (out or ""), (err or ""), done


def _release(proc: subprocess.Popen, *, inline: bool) -> None:
    """Close the pipes. While a reader thread is still blocked on one, `close()` blocks too (until the process or a descendant that
    holds it exits), so unless the pipes were read to the end it is done on a daemon thread and this call never waits."""
    def close() -> None:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    if inline:
        close()
    else:
        threading.Thread(target=close, name="shellproc-close", daemon=True).start()


def run_owned(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    timeout_s: float,
    owner: str | None = None,
    stop_requested: Callable[[], bool] | None = None,
    grace_s: float = GRACE_S,
    confirm_s: float = CONFIRM_S,
) -> Outcome:
    """Run `argv`, capture its text output, and stop its whole tree on a stop request or at the time limit. See the module docstring.

    `owner` is only an identifier kept in the registry. `stop_requested` is polled from the calling thread every `POLL_S`; it is the
    caller's job to make it true only for a stop that is meant for this operation."""
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED) if _WIN else 0
    job = _new_job()
    if job is None:
        log.warning("shell operation: not started (no job object, so the process tree could not be owned)")
        return Outcome(NOT_STARTED, detail="no_job")
    try:
        proc = subprocess.Popen(
            list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, creationflags=flags,
        )
    except BaseException:
        _close(job)
        raise
    # The child exists but has executed nothing. It is assigned to the job, and only then allowed to run.
    try:
        owned = _assign(job, proc)
        resumed = owned and _resume(proc, job)
    except BaseException:
        _discard(proc, job)
        raise
    if not resumed:
        detail = "assign_failed" if not owned else "resume_failed"
        log.warning("shell operation: not started (%s)", detail)
        _discard(proc, job)
        return Outcome(NOT_STARTED, detail=detail)
    with _LOCK:
        _NEXT[0] += 1
        op = _Op(_NEXT[0], proc.pid, owner)
        _ACTIVE[op.op_id] = op
    drained = False
    try:
        deadline = time.monotonic() + timeout_s
        reason = ""
        try:
            while True:
                try:
                    out, err = proc.communicate(timeout=POLL_S)
                    drained = True
                    return Outcome(COMPLETED, proc.returncode, out or "", err or "")
                except subprocess.TimeoutExpired:
                    pass
                if stop_requested is not None and stop_requested():
                    reason = CANCELLED
                    break
                if time.monotonic() >= deadline:
                    reason = TIMEOUT
                    break
        except BaseException:
            # Anything unexpected while we own a running process (a failing stop callback, the worker being torn down): it must not
            # be left running behind us, unowned.
            if not _terminate_job(job):
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            raise
        if proc.poll() is not None:
            # It ended on its own between the last poll and now: a stop that arrives after the fact is harmless, and this is not
            # reported as a stop.
            out, err, drained = _drain(proc, confirm_s)
            return Outcome(COMPLETED, proc.returncode, out, err)
        gone, how = _stop_tree(proc, job, grace_s=grace_s, confirm_s=confirm_s)
        out, err, drained = _drain(proc, confirm_s)
        if gone is True:
            return Outcome(reason, proc.poll(), out, err, how)
        detail = "tree_still_running" if gone is False else "not_confirmed"
        log.warning("shell operation: stop acted on but not confirmed (%s)", detail)
        return Outcome(UNKNOWN, proc.poll(), out, err, how, detail, reason)
    finally:
        _release(proc, inline=drained)
        _close(job)
        with _LOCK:
            _ACTIVE.pop(op.op_id, None)
