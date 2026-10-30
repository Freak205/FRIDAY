"""Operation-owned shell processes (Phase 90): start one command, own its whole process tree, and stop exactly that tree.

Before this phase `shell.run` was `subprocess.run` in a worker thread. A STOP only armed the shared `INTEL` flag; the
orchestrator cannot interrupt a worker thread, so it announced "worker-thread tool, waiting for it to finish" and let the
command run on (up to the 60 s step timeout). A timeout killed only the direct child, never what it had started, and left the
pipe reader waiting on any descendant that still held the pipe. The process was not tied to a goal, and nothing recorded which
processes an operation owned.

What an operation owns here:

  * its own process group (`CREATE_NEW_PROCESS_GROUP`), so a console Ctrl+Break can be aimed at it and nothing else; and
  * a Windows job object the child is assigned to right after it starts. Every descendant it starts joins the job, so "the
    operation's tree" is a kernel-tracked set, not a guess from names or parent ids. Nothing else is ever in that job.

Stopping, bounded and in this order (nothing is ever killed by image name, and `taskkill` is never used):

  1. graceful: Ctrl+Break to the operation's process group; wait up to `grace_s` for the job to empty;
  2. forced:   `TerminateJobObject` on the operation's own job -- only if step 1 did not empty it (or could not be sent);
  3. confirm:  wait up to `confirm_s` for the job to report zero active processes.

If step 3 cannot show the tree is gone (the job could not be created or queried, or a process outlived the terminate) the outcome
is "unknown", never "cancelled". A process that finished on its own before the stop is simply "completed": a stop that arrives
after the fact changes nothing and harms nothing, and a repeated stop is the same flag read again -- the worker thread that owns
the process handles it once, then leaves.

The job is deliberately NOT created with kill-on-close: a command that intentionally leaves something running (`start notepad`)
keeps doing so after it completes normally, exactly as before.

Only the thread that started the process ever stops it; the caller supplies `stop_requested()`, scoped by the caller to the
goal that owns the operation (see `friday.skills.shell`). Output is returned as text and never logged. Pipes and the job handle
are released on every path.
"""

from __future__ import annotations

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
    detail: str = ""       # a fixed reason code for UNKNOWN (never argument text)
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

def _new_job():
    """A job object for one operation, or None. Not kill-on-close (see the module docstring)."""
    if not _WIN:
        return None
    try:
        return win32job.CreateJobObject(None, "")
    except Exception:  # noqa: BLE001
        log.warning("shell operation: job object unavailable; the process tree cannot be tracked")
        return None


def _assign(job, proc: subprocess.Popen) -> bool:
    """Put the just-started child in the job. False when that is not possible (then the tree is not tracked)."""
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
        log.warning("shell operation: could not assign the process to its job; the tree cannot be tracked")
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


# -- stopping ---------------------------------------------------------------------------------------------------------------

def _tree_gone(proc: subprocess.Popen, job, tracked: bool) -> bool | None:
    """True / False when the tree is known gone / still there; None when that cannot be known."""
    if not tracked:
        return None
    n = _active_count(job)
    if n is None:
        return None
    return n == 0 and proc.poll() is not None


def _stop_tree(proc: subprocess.Popen, job, tracked: bool, *, grace_s: float, confirm_s: float) -> tuple[bool | None, str]:
    """Graceful, then forced, then confirm. Returns (gone, how)."""
    how = ""
    if _send_graceful(proc):
        how = "graceful"
        end = time.monotonic() + grace_s
        while time.monotonic() < end:
            if _tree_gone(proc, job, tracked) or (not tracked and proc.poll() is not None):
                break
            time.sleep(POLL_S / 2)
    gone = _tree_gone(proc, job, tracked)
    if gone is not True:
        if tracked and _terminate_job(job):
            how = "forced"
        else:
            try:
                proc.kill()           # the direct child only: its descendants are not ours to name without the job
            except Exception:  # noqa: BLE001
                pass
            how = "forced"
        end = time.monotonic() + confirm_s
        while time.monotonic() < end:
            gone = _tree_gone(proc, job, tracked)
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
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if _WIN else 0
    proc = subprocess.Popen(
        list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd, creationflags=flags,
    )
    job = _new_job()
    tracked = _assign(job, proc)
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
            if not (tracked and _terminate_job(job)):
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
        gone, how = _stop_tree(proc, job, tracked, grace_s=grace_s, confirm_s=confirm_s)
        out, err, drained = _drain(proc, confirm_s)
        if gone is True:
            return Outcome(reason, proc.poll(), out, err, how)
        detail = "tree_still_running" if gone is False else ("not_tracked" if not tracked else "not_confirmed")
        log.warning("shell operation: stop acted on but not confirmed (%s)", detail)
        return Outcome(UNKNOWN, proc.poll(), out, err, how, detail, reason)
    finally:
        _release(proc, inline=drained)
        _close(job)
        with _LOCK:
            _ACTIVE.pop(op.op_id, None)
