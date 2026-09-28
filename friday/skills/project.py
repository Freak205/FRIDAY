"""Project skills: what state a development project is in, right now."""

from __future__ import annotations

import shutil
import subprocess
from typing import Annotated

from friday import project
from friday.registry import SkillResult, skill


@skill(
    name="project.inspect",
    tier="L0",
    description="Inspect a project directory: files, README/PLAN, git status, and known documents",
    examples=[
        "inspect my project",
        "what is my project status",
        "look at the current state of my project",
        "check on my friday project",
        "what's the state of this project",
        "give me a status report on this project",
        "look at this project and tell me where it's at",
    ],
)
def inspect(
    name: Annotated[str, "project name or path, blank for the current directory"] = "",
) -> SkillResult:
    try:
        report = project.inspect(name)
    except project.ProjectNotFound as exc:
        return SkillResult(speech=f"I can't find a project called {exc}.", ok=False)

    parts = [f"{report.name}."]
    if report.stacks:
        parts.append(f"Stack: {', '.join(report.stacks)}.")
    if report.git.is_repo:
        state = f"On branch {report.git.branch}"
        state += f", {report.git.dirty} uncommitted change(s)." if report.git.dirty else ", clean."
        parts.append(state)
    elif report.git.error:
        parts.append(report.git.error + ".")
    else:
        parts.append("Not a git repository.")
    if report.knowledge_hits:
        parts.append(f"{len(report.knowledge_hits)} related document(s) in the knowledge base.")
    if report.next_step_hint:
        parts.append(f"Next: {report.next_step_hint}")

    return SkillResult(
        speech=" ".join(parts),
        data={
            "path": str(report.path),
            "name": report.name,
            "top_level": report.top_level,
            "stacks": report.stacks,
            "readme_excerpt": report.readme_excerpt,
            "plan_excerpt": report.plan_excerpt,
            "git": {
                "is_repo": report.git.is_repo,
                "branch": report.git.branch,
                "dirty": report.git.dirty,
                "recent_commits": report.git.recent_commits,
                "error": report.git.error,
            },
            "knowledge_hits": report.knowledge_hits,
            "next_step_hint": report.next_step_hint,
        },
    )


@skill(
    name="project.open",
    tier="L1",
    action="open",
    description="Open a project folder in VS Code",
    examples=[
        "open my project in vs code",
        "open this project in visual studio code",
        "open my friday project in vs code",
        "launch vs code on this project",
        "open project x in vs code",
    ],
)
def open_in_vscode(
    name: Annotated[str, "project name or path, blank for the current directory"] = "",
) -> SkillResult:
    try:
        path = project.resolve(name)
    except project.ProjectNotFound as exc:
        return SkillResult(speech=f"I can't find a project called {exc}.", ok=False)

    code_bin = shutil.which("code") or shutil.which("code.cmd")
    if not code_bin:
        return SkillResult(
            speech="VS Code's 'code' command isn't on PATH. In VS Code, run "
            "'Shell Command: Install code command in PATH' from the command palette.",
            ok=False,
        )

    try:
        subprocess.Popen([code_bin, str(path)])
    except Exception as exc:
        return SkillResult(speech=f"I couldn't open VS Code: {exc}", ok=False)

    return SkillResult(speech=f"Opening {path.name} in VS Code.", data={"path": str(path)})
