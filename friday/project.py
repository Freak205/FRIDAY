"""Project awareness — what state a development project is in, right now.

Read-only: gathers a top-level directory listing, README/PLAN excerpts, git
status (when the folder is a repo), a language/stack guess, and any already-
indexed knowledge-base documents under the project path, into one structured
report. Reuses `friday.knowledge` for the last part rather than building a
second retrieval path. Never edits source code — that's a later capability.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday.log import get

log = get(__name__)

# Common places a personal project lives on this machine. Best-effort fuzzy
# lookup only — an explicit path always wins.
_SEARCH_ROOTS = [
    Path.home() / "Desktop",
    Path.home() / "OneDrive" / "Desktop",
    Path.home() / "Documents",
    Path.home() / "Projects",
    Path.home() / "source" / "repos",
]

_NOISE_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", ".idea", ".vscode",
    "dist", "build", "$RECYCLE.BIN", "System Volume Information",
}

_STACK_MARKERS = {
    "package.json": "Node.js",
    "pyproject.toml": "Python",
    "requirements.txt": "Python",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "pom.xml": "Java (Maven)",
    "build.gradle": "Java/Kotlin (Gradle)",
    "*.csproj": "C#/.NET",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
}


class ProjectNotFound(Exception):
    pass


@dataclass(slots=True)
class GitState:
    is_repo: bool
    branch: str = ""
    dirty: int = 0
    recent_commits: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(slots=True)
class ProjectReport:
    path: Path
    name: str
    top_level: list[str]
    stacks: list[str]
    readme_excerpt: str
    plan_excerpt: str
    git: GitState
    knowledge_hits: list[dict[str, Any]]
    next_step_hint: str = ""


def _candidate_dirs() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for root in _SEARCH_ROOTS:
        if not root.exists():
            continue
        try:
            for entry in root.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    found.setdefault(entry.name.lower(), entry)
        except OSError:
            continue
    return found


def resolve(name: str) -> Path:
    """Resolve a project name or path to a directory. Raises ProjectNotFound."""
    query = (name or "").strip()
    if not query or query.lower() in ("this", "current", "here", "it", "my project"):
        return Path.cwd().resolve()

    # Only treat the query as a literal filesystem path if it actually looks
    # like one. A bare name ("FRIDAY") must always go through name-based
    # matching below — otherwise, on Windows' case-insensitive filesystem, a
    # bare name can accidentally resolve *relative to cwd* to an unrelated
    # same-named subdirectory (e.g. running from inside the FRIDAY project
    # root, "FRIDAY" matches the inner friday/ package by coincidence).
    looks_like_path = (
        "/" in query or "\\" in query or query.startswith("~")
        or (len(query) > 1 and query[1] == ":")
    )
    if looks_like_path:
        direct = Path(query).expanduser()
        if direct.exists() and direct.is_dir():
            return direct.resolve()

    candidates = _candidate_dirs()
    if candidates:
        from rapidfuzz import fuzz, process, utils

        # rapidfuzz 3.x's WRatio no longer case-folds by default (processor=None
        # unless passed explicitly) — without it, "FRIDAY" scores 0 against
        # lowercase candidate keys. Spoken utterances arrive pre-lowered by the
        # brain's L1 normalizer, but a caller that bypasses the brain (an LLM
        # planner's arg, a direct API call) sends whatever casing it likes.
        # Cutoff raised from 75 to 88 after fixing the case-folding bug above
        # exposed WRatio's token-subset scoring: a nonsense query sharing one
        # common word with a real folder name (e.g. "...does not exist..."
        # vs. "old project", both containing "project") scored 85.5 — above
        # the old cutoff. 88 keeps real near-matches ("fridayy" 92.3, "the
        # friday project" 90.0, "frida" 90.9) while rejecting that case.
        match = process.extractOne(
            query, candidates.keys(), scorer=fuzz.WRatio,
            processor=utils.default_process, score_cutoff=88,
        )
        if match:
            return candidates[match[0]].resolve()

    raise ProjectNotFound(query)


# Headings this project's own PLAN.md convention uses to flag what's next —
# reused as a generic heuristic for *any* project that follows a similar
# pattern. Purely textual: never invents a next step that isn't already
# written down somewhere in the project's own docs.
_NEXT_HEADING = re.compile(r"(?im)^#{1,6}\s*.*\brecommended next\b.*$")
_CHECKBOX = re.compile(r"(?m)^\s*[-*]\s*\[ \]\s*(.+?)\s*$")
_NEXT_LABEL = re.compile(r"(?im)^#{1,6}\s*.*\b(next steps?|up next|todo)\b.*$")


def _next_step_hint(path: Path, limit: int = 500) -> str:
    """Best-effort 'what should I work on next' from the project's own docs.

    Looks for a "Recommended next ..." / "Next steps" / "TODO" heading first
    (this project's own PLAN.md convention, but a common one generally), then
    falls back to the first few unchecked markdown checkboxes. Returns "" —
    never a guess — when nothing textual points at a next step; the skill
    layer must not fabricate one.
    """
    doc = next(
        (path / n for n in ("PLAN.md", "plan.md", "TODO.md", "todo.md") if (path / n).exists()),
        None,
    )
    if doc is None:
        return ""
    try:
        text = doc.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    for pattern in (_NEXT_HEADING, _NEXT_LABEL):
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        heading = matches[-1]  # the most recent such section wins
        rest = text[heading.end():]
        end = re.search(r"\n#{1,6}\s", rest)
        chunk = rest[: end.start() if end else len(rest)].strip()
        if chunk:
            return chunk[:limit]

    boxes = _CHECKBOX.findall(text)
    if boxes:
        return "Open items: " + "; ".join(b[:100] for b in boxes[:5])

    return ""


def _read_excerpt(p: Path, limit: int = 1200) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").strip()[:limit]
    except OSError:
        return ""


def _top_level(path: Path, limit: int = 25) -> list[str]:
    try:
        entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError:
        return []
    names = [e.name + ("/" if e.is_dir() else "") for e in entries if e.name not in _NOISE_DIRS]
    return names[:limit]


def _detect_stacks(path: Path) -> list[str]:
    stacks = []
    for marker, label in _STACK_MARKERS.items():
        try:
            hit = any(path.glob(marker)) if marker.startswith("*") else (path / marker).exists()
        except OSError:
            hit = False
        if hit:
            stacks.append(label)
    seen: set[str] = set()
    return [s for s in stacks if not (s in seen or seen.add(s))]


def _git_state(path: Path) -> GitState:
    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, timeout=10
        )

    try:
        check = run("rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:
        return GitState(is_repo=False, error="git isn't installed")
    except subprocess.TimeoutExpired:
        return GitState(is_repo=False, error="git timed out")

    if check.returncode != 0 or check.stdout.strip() != "true":
        return GitState(is_repo=False)

    branch = run("branch", "--show-current").stdout.strip() or "(detached HEAD)"
    status = run("status", "--porcelain").stdout.strip()
    dirty = len(status.splitlines()) if status else 0
    log_out = run("log", "--oneline", "-5").stdout.strip()

    return GitState(
        is_repo=True, branch=branch, dirty=dirty,
        recent_commits=log_out.splitlines() if log_out else [],
    )


def _knowledge_hits(path: Path, name: str) -> list[dict[str, Any]]:
    try:
        from friday import knowledge
    except Exception:
        return []

    prefix = str(path).lower()
    try:
        docs = [d for d in knowledge.list_documents() if d["path"].lower().startswith(prefix)]
    except Exception:
        docs = []
    if docs:
        return [{"title": d["title"], "path": d["path"]} for d in docs[:5]]

    # Nothing indexed under this exact path — a meaning-based search for the
    # project name still surfaces anything relevant elsewhere in the KB.
    try:
        hits = knowledge.search(f"{name} project status plan", k=3)
    except Exception:
        return []
    return [
        {"title": h.title, "path": h.path, "score": round(h.score, 3)}
        for h in hits if h.score >= 0.35
    ]


def inspect(name: str = "") -> ProjectReport:
    path = resolve(name)
    if not path.exists():
        raise ProjectNotFound(str(path))

    readme = next(
        (path / n for n in ("README.md", "readme.md", "README.txt") if (path / n).exists()), None
    )
    plan = next(
        (path / n for n in ("PLAN.md", "plan.md", "TODO.md") if (path / n).exists()), None
    )

    return ProjectReport(
        path=path,
        name=path.name,
        top_level=_top_level(path),
        stacks=_detect_stacks(path),
        readme_excerpt=_read_excerpt(readme) if readme else "",
        plan_excerpt=_read_excerpt(plan) if plan else "",
        git=_git_state(path),
        knowledge_hits=_knowledge_hits(path, path.name),
        next_step_hint=_next_step_hint(path),
    )
