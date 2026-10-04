"""Phase 25 (Workstream 5) — regression coverage for `shell.run`'s forbidden-command
blocklist (`friday.skills.shell._FORBIDDEN`/`_screen`).

The completion audit found that the single most powerful tool in the registry — arbitrary
shell execution — had a blocklist with NO test asserting it actually blocks anything: its
one safety backstop (confirmation is the real boundary; see the module docstring) rested
entirely on manual code review.

Writing this suite found a real defect (not a hypothetical one — brief-compliant: "do not
change the blocklist unless a real test exposes an actual defect"): the whole-drive `del`
pattern only matched EXACTLY ONE flag positioned before the drive letter, so the most
common real invocation of that exact command — `del /s /q c:\\`, `del /f /s /q c:\\`, or
the flags trailing the path — passed straight through despite matching the rule's own
stated intent ("wiping a whole drive"). Fixed at `friday/skills/shell.py`'s `_FORBIDDEN`
list (now one-or-more flags, either side of the drive path); pinned here as case B2/B3/B4.

This suite pins:

  A  every currently-forbidden pattern's representative command is rejected
  B  ...including realistic multi-flag / flag-order / flag-position variants of `del`
     and `Remove-Item` that a person (or a model) would actually type
  C  case and spacing variants of a forbidden command are still caught
  D  representative ALLOWED commands are not blocked (no over-blocking of ordinary use)
  E  fail-closed: a blocked command never reaches `subprocess.run` at all — `shell.run`
     returns `ok=False` and the process is never spawned, confirmation or not

Pure `_screen()` calls plus one real `shell.run()` invocation with `subprocess.run`
monkeypatched to detect any spawn attempt. No Ollama, no real process execution for a
blocked command; the one ALLOWED-command execution (D) runs a harmless real `dir`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday.skills import shell  # noqa: E402

FORBIDDEN_CASES = [
    # (command, why substring, label)
    ("format c:", "format", "disk formatting"),
    ("del /s c:\\", "wiping", "del /s <drive> (original single-flag form)"),
    ("del /q c:", "wiping", "del /q <drive>"),
    ("del /f c:", "wiping", "del /f <drive>"),
    ("Remove-Item -Recurse c:\\", "wiping", "Remove-Item -Recurse <drive>"),
    ("Remove-Item -Recurse -Force c:\\", "wiping", "Remove-Item -Recurse -Force <drive>"),
    ("vssadmin delete shadows", "shadow", "vssadmin delete shadows"),
    ("bcdedit /set testsigning on", "boot", "bcdedit"),
    ("diskpart", "partition", "diskpart"),
    ("cipher /w:c", "wip", "cipher /w"),
    ("reg delete HKLM", "registry", "reg delete HKLM"),
    ("reg delete HKEY_LOCAL_MACHINE", "registry", "reg delete HKEY_LOCAL_MACHINE"),
]

# The real defect this suite found: realistic multi-flag / reordered / trailing-flag
# invocations of the exact commands the blocklist already claims to cover.
MULTIFLAG_CASES = [
    ("del /s /q c:\\", "the most common real 'wipe C:' command line"),
    ("del /q /s c:\\", "flags in the other order"),
    ("del /f /s /q c:\\", "three flags"),
    ("del c:\\ /s /q", "flags trailing the path instead of leading it"),
    ("Remove-Item -Force -Recurse c:\\", "Remove-Item flags in the other order"),
]

CASE_SPACING_VARIANTS = [
    "FORMAT C:",
    "Format   C:",
    "  format c:  ".strip(),
    "VSSADMIN DELETE SHADOWS",
    "DiskPart",
    "REG DELETE HKLM",
]

ALLOWED_CASES = [
    ("dir", "an ordinary read-only command"),
    ("ipconfig /all", "network info, no destructive verb at all"),
    ("Get-ChildItem", "PowerShell's dir equivalent"),
    ("del myfile.txt", "deleting one named file, not a drive"),
    ("del /s myfile.txt", "a flag present, but the target is a file, not a bare drive"),
    ("del /s D:\\projects\\build", "a flag and a real path, not a bare drive letter"),
    ("Remove-Item C:\\temp\\file.txt", "Remove-Item on one file, no -Recurse"),
    ("git status", "an ordinary git command"),
    ("Format-Table -AutoSize", "a PowerShell cmdlet that happens to start with 'Format'"),
    ("echo hello world", "harmless output"),
]


def main() -> int:
    t0 = time.perf_counter()

    scenario("A: every currently-forbidden pattern's representative command is rejected")
    for command, why_substr, label in FORBIDDEN_CASES:
        why = shell._screen(command)
        check(f"blocked: {label}", why is not None and why_substr in why, f"{command!r} -> {why!r}")

    scenario("B: realistic multi-flag / reordered / trailing-flag variants (the defect this suite found)")
    for command, label in MULTIFLAG_CASES:
        why = shell._screen(command)
        check(f"blocked: {label}", why is not None, f"{command!r} -> {why!r}")

    scenario("C: case and spacing variants of a forbidden command are still caught")
    for command in CASE_SPACING_VARIANTS:
        why = shell._screen(command)
        check(f"blocked regardless of case/spacing: {command!r}", why is not None, f"{command!r} -> {why!r}")

    scenario("D: representative ALLOWED commands are not blocked")
    for command, label in ALLOWED_CASES:
        why = shell._screen(command)
        check(f"allowed: {label}", why is None, f"{command!r} -> {why!r}")

    scenario("E: fail-closed — a blocked command never reaches subprocess.run")
    import subprocess

    spawned = []
    original_run = subprocess.run

    def spy_run(*args, **kwargs):
        spawned.append(args)
        return original_run(*args, **kwargs)

    subprocess.run = spy_run
    try:
        result = shell.run(command="format c:")
        check("blocked command returns ok=False", not result.ok, result.speech)
        check("blocked command's speech explains why, without running it", "format" in result.speech.lower() and "won't" in result.speech.lower(), result.speech)
        check("subprocess.run was never invoked for a blocked command", spawned == [], spawned)

        spawned.clear()
        result2 = shell.run(command="del /s /q c:\\")
        check("the multi-flag defect case is also fail-closed end-to-end via shell.run()", not result2.ok and spawned == [], f"ok={result2.ok} spawned={spawned}")

        spawned.clear()
        result3 = shell.run(command="dir")
        check("an ALLOWED command still actually runs", result3.ok and spawned != [], f"ok={result3.ok} spawned={bool(spawned)}")
    finally:
        subprocess.run = original_run

    return finish("Phase 25 — shell.run blocklist", time.perf_counter() - t0, min_assertions=30, min_scenarios=5)


if __name__ == "__main__":
    sys.exit(main())
