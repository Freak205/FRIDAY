"""Shell and developer skills.

Arbitrary command execution is the single most dangerous capability FRIDAY has,
so it sits at L2 (always confirmed) and refuses a small set of catastrophic
patterns outright. The blocklist is a backstop, not the security boundary — the
confirmation prompt is.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Annotated

from friday.log import get
from friday.registry import SkillResult, skill
from friday.intent import GIT_RULE

log = get(__name__)

_TIMEOUT = 60

# Patterns refused outright, regardless of confirmation. These are the commands
# with no plausible benign use from a voice assistant.
_FORBIDDEN = [
    (re.compile(r"\bformat\s+[a-z]:", re.I), "disk formatting"),
    (re.compile(r"\bdel\s+/[sqf]\s+[a-z]:\\?\s*$", re.I), "wiping a whole drive"),
    (re.compile(r"Remove-Item.*-Recurse.*[a-z]:\\?\s*$", re.I), "wiping a whole drive"),
    (re.compile(r"\bvssadmin\s+delete\s+shadows", re.I), "deleting shadow copies"),
    (re.compile(r"\bbcdedit\b", re.I), "editing the boot configuration"),
    (re.compile(r"\bdiskpart\b", re.I), "partition editing"),
    (re.compile(r"\bcipher\s+/w", re.I), "secure-wiping free space"),
    (re.compile(r"reg\s+delete\s+HK(LM|EY_LOCAL_MACHINE)\\?\s*$", re.I), "deleting a registry hive"),
]


def _screen(command: str) -> str | None:
    for pattern, why in _FORBIDDEN:
        if pattern.search(command):
            return why
    return None


@skill(
    name="shell.run",
    tier="L2",
    action="modify",
    description="Run a shell command and return its output",
    examples=[
        "run this command",
        "execute a command for me",
        "run ipconfig",
        "run a powershell command",
        "execute this in the terminal",
    ],
    dry_run=lambda command, shell="powershell", cwd="": (
        f"Run in {shell}: {command}" + (f" (in {cwd})" if cwd else "")
    ),
)
def run(
    command: Annotated[str, "the command to execute"],
    shell: Annotated[str, "'powershell' or 'cmd'"] = "powershell",
    cwd: Annotated[str, "working directory, blank for home"] = "",
) -> SkillResult:
    blocked = _screen(command)
    if blocked:
        return SkillResult(
            speech=f"I won't run that — it involves {blocked}.", ok=False
        )

    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.exists():
        return SkillResult(speech=f"There's no directory at {cwd}.", ok=False)

    argv = (
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
        if shell == "powershell"
        else ["cmd", "/c", command]
    )

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, cwd=workdir
        )
    except subprocess.TimeoutExpired:
        return SkillResult(speech=f"That command timed out after {_TIMEOUT} seconds.", ok=False)

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    ok = proc.returncode == 0

    if ok:
        speech = out[:400] if out else "Done, no output."
    else:
        speech = f"Exit code {proc.returncode}. {(err or out)[:300]}"

    return SkillResult(
        speech=speech,
        ok=ok,
        data={"stdout": out, "stderr": err, "returncode": proc.returncode},
    )


@skill(
    name="dev.git",
    tier="L1",
    action=GIT_RULE,
    description="Run a read-only git command in a repository",
    examples=[
        "git status",
        "what's the git status",
        "show me recent commits",
        "what branch am I on",
        "check for uncommitted changes",
    ],
)
def git(
    subcommand: Annotated[str, "git subcommand, e.g. 'status' or 'log --oneline -5'"] = "status",
    repo: Annotated[str, "path to the repository, blank for the current directory"] = "",
) -> SkillResult:
    # Only read-only subcommands here; anything that writes belongs at L2.
    allowed = {"status", "log", "diff", "branch", "show", "remote", "stash", "blame"}
    first = subcommand.strip().split()[0] if subcommand.strip() else "status"
    if first not in allowed:
        return SkillResult(
            speech=f"'{first}' changes the repository — ask me to run it via shell instead.",
            ok=False,
        )

    workdir = Path(repo).expanduser() if repo else Path.cwd()
    try:
        proc = subprocess.run(
            ["git", *subcommand.split()],
            capture_output=True, text=True, timeout=30, cwd=workdir,
        )
    except FileNotFoundError:
        return SkillResult(speech="Git isn't installed or isn't on the path.", ok=False)

    if proc.returncode != 0:
        return SkillResult(
            speech=(proc.stderr or "That git command failed.").strip()[:200], ok=False
        )

    out = proc.stdout.strip()
    return SkillResult(
        speech=out[:400] if out else "Nothing to report.",
        data={"output": out, "cwd": str(workdir)},
    )


@skill(
    name="dev.python",
    tier="L2",
    action="modify",
    description="Evaluate a short Python expression",
    examples=[
        "calculate something in python",
        "evaluate this python expression",
        "run some python for me",
        "what's 2 to the power of 40",
    ],
    dry_run=lambda code: f"Evaluate Python: {code}",
)
def python_eval(
    code: Annotated[str, "the Python expression or short script to run"],
) -> SkillResult:
    # A separate interpreter process, so a runaway expression can't take the
    # daemon down with it.
    import sys

    try:
        proc = subprocess.run(
            [sys.executable, "-c", f"print(eval({code!r}))"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return SkillResult(speech="That took too long to evaluate.", ok=False)

    if proc.returncode != 0:
        error = (proc.stderr or "").strip().splitlines()
        return SkillResult(
            speech=f"That didn't evaluate: {error[-1] if error else 'unknown error'}",
            ok=False,
        )

    result = proc.stdout.strip()
    return SkillResult(speech=result[:300], data={"result": result})
