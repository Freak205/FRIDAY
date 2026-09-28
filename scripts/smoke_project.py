"""Project awareness: resolution, git-state detection (including the "not a
git repo" case — this very project isn't one), and the skill layer end to end.
"""

import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import project  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

MATCH_CASES = [
    ("what is my project status", "project.inspect"),
    ("look at the current state of my project", "project.inspect"),
    ("open this project in visual studio code", "project.open"),
    ("open the friday project in vs code", "project.open"),
    ("launch vs code on this project", "project.open"),
    ("open my friday project", "project.inspect"),
    # a bare app-launch phrase must NOT be misread as a project lookup just
    # because it names the same app project.open's examples mention
    ("open vs code", "apps.open"),
    ("open vscode", "apps.open"),
    ("launch vs code", "apps.open"),
    ("start vs code", "apps.open"),
    ("open visual studio code", "apps.open"),
]


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    print("\n--- registration ---\n")
    for name in ("project.inspect", "project.open"):
        registered = REGISTRY.get(name) is not None
        print(f"  {'OK  ' if registered else 'MISS'} {name} registered")
        overall &= registered

    print("\n--- intent matching ---\n")
    match_ok = 0
    for utterance, expected in MATCH_CASES:
        u = BRAIN.understand(utterance)
        good = u.skill == expected
        match_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance:42} -> {u.skill} ({u.score:.2f})")
    print(f"\n  {match_ok}/{len(MATCH_CASES)} correct")
    overall &= match_ok == len(MATCH_CASES)

    print("\n--- resolve(): blank/'this' means the current directory ---\n")
    resolved = project.resolve("")
    resolve_ok = resolved == Path.cwd().resolve()
    print(f"  {'OK  ' if resolve_ok else 'MISS'} resolve('') -> {resolved}")
    overall &= resolve_ok

    print("\n--- resolve(): unknown name fails cleanly ---\n")
    try:
        project.resolve("a-project-that-definitely-does-not-exist-anywhere-xyz")
        print("  FAIL expected ProjectNotFound, none raised")
        overall = False
    except project.ProjectNotFound:
        print("  OK   unknown project name raises ProjectNotFound")

    print("\n--- inspect(): this repo (not a git repository) ---\n")
    root = Path(__file__).resolve().parent.parent
    report = project.inspect(str(root))
    checks = {
        "found PLAN.md": bool(report.plan_excerpt),
        "found README-ish top-level entries": len(report.top_level) > 0,
        "detected Python stack": "Python" in report.stacks,
        "correctly reports not-a-git-repo": not report.git.is_repo,
        "found a next-step hint in PLAN.md's own 'recommended next' sections":
            bool(report.next_step_hint),
    }
    for label, ok in checks.items():
        print(f"  {'OK  ' if ok else 'MISS'} {label}")
        overall &= ok
    print(f"  next_step_hint[:100] = {report.next_step_hint[:100]!r}")

    print("\n--- next_step_hint(): unchecked checkboxes when there's no 'recommended next' heading ---\n")
    with tempfile.TemporaryDirectory(prefix="friday-proj-todo-test-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "TODO.md").write_text(
            "# Todo\n\n- [x] done thing\n- [ ] wire up the export button\n- [ ] write tests\n",
            encoding="utf-8",
        )
        todo_report = project.inspect(str(tmp_path))
        hint_ok = "wire up the export button" in todo_report.next_step_hint
        print(f"  {'OK  ' if hint_ok else 'MISS'} unchecked box surfaced -> {todo_report.next_step_hint!r}")
        overall &= hint_ok

    print("\n--- next_step_hint(): no PLAN/TODO file -> empty, never invented ---\n")
    with tempfile.TemporaryDirectory(prefix="friday-proj-noplan-test-") as tmp:
        blank_report = project.inspect(tmp)
        blank_ok = blank_report.next_step_hint == ""
        print(f"  {'OK  ' if blank_ok else 'MISS'} no plan/todo file -> hint={blank_report.next_step_hint!r}")
        overall &= blank_ok

    print("\n--- inspect(): a real git repo (a fresh temp repo) ---\n")
    with tempfile.TemporaryDirectory(prefix="friday-proj-test-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "README.md").write_text("# Test Repo\n\nHello.\n", encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")

        def git(*args: str) -> None:
            subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True)

        git("init", "-q")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        git("add", "-A")
        git("commit", "-q", "-m", "initial commit")
        (tmp_path / "scratch.txt").write_text("uncommitted\n", encoding="utf-8")

        git_report = project.inspect(str(tmp_path))
        git_checks = {
            "detected as a git repo": git_report.git.is_repo,
            "found a branch name": bool(git_report.git.branch),
            "found 1 uncommitted change": git_report.git.dirty == 1,
            "found the initial commit": bool(git_report.git.recent_commits),
            "found README excerpt": "Test Repo" in git_report.readme_excerpt,
            "detected Python stack": "Python" in git_report.stacks,
        }
        for label, ok in git_checks.items():
            print(f"  {'OK  ' if ok else 'MISS'} {label}")
            overall &= ok

    print("\n--- end to end via SESSION.handle (fuzzy project name -> this repo) ---\n")
    result = await SESSION.handle("check on my friday project", actor="text")
    e2e_ok = result.ok and "python" in result.speech.lower()
    print(f"  {'OK  ' if e2e_ok else 'MISS'} -> {result.speech[:120]}")
    overall &= e2e_ok

    print("\n--- Phase 15.0: project.* name slot extraction (real-machine finding) ---\n")
    # Found by actually running the brief's own literal example D against the
    # live daemon: `_LEAD_VERBS` (friday/brain/extract.py) never learned
    # "inspect"/"check on"/"look at" -- verbs project.inspect's OWN taught
    # examples use -- so those verbs, or a trailing "and tell me ..." clause,
    # became PART of the extracted "name" argument. `project.resolve()`'s
    # fuzzy match then only succeeded by accident when enough of the real
    # folder name survived; the brief's longer example did not (the whole
    # sentence became the name -> ProjectNotFound). See PLAN.md Phase 15.0.
    # Through BRAIN.understand (the real pipeline: normalize -> match ->
    # extract), not friday.brain.extract.extract() directly -- normalize()
    # already strips trailing punctuation before extraction ever runs, so a
    # direct extract() call on a raw, period-terminated string would see a
    # trailing "." this real pipeline never actually presents it with.
    EXTRACT_CASES = [
        # (utterance, expected extracted "name")
        ("Inspect my FRIDAY project and tell me what to work on next.", "friday project"),
        ("Check on my FRIDAY project.", "friday project"),
        ("Inspect my FRIDAY project.", "friday project"),
    ]
    extract_ok = 0
    for utterance, expected in EXTRACT_CASES:
        got = (BRAIN.understand(utterance).args.get("name") or "").lower()
        good = got == expected
        extract_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance!r:60} -> name={got!r} (expected {expected!r})")
    overall &= extract_ok == len(EXTRACT_CASES)

    # The literal brief example, end to end: must actually resolve and
    # inspect the real project, not fail with "I can't find a project
    # called <the whole sentence>."
    result = await SESSION.handle(
        "Inspect my FRIDAY project and tell me what to work on next.", actor="text",
    )
    example_d_ok = result.ok and "next" in result.speech.lower()
    print(f"  {'OK  ' if example_d_ok else 'MISS'} brief's literal example D end to end -> {result.speech[:120]}")
    overall &= example_d_ok

    # A bare "open the project in VS Code" (no project named at all) must
    # resolve to the current project, not pass "project in vscode" through
    # as a literal (garbage) folder query.
    result = await SESSION.handle("open the project in vs code", actor="text")
    bare_ok = result.ok
    print(f"  {'OK  ' if bare_ok else 'MISS'} bare 'the project' resolves to the current project -> {result.speech[:120]}")
    overall &= bare_ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
