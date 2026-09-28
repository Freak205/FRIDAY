"""File skills: search and read.

P0 walks your user folders directly. P1 swaps the backend for the Everything SDK,
which makes full-disk search instant; the skill signatures stay the same.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from rapidfuzz import fuzz

from friday.registry import SkillResult, skill

# Directories worth searching, and ones that are never interesting.
_SEARCH_ROOTS = ["Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music"]
_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", ".venv", "venv", "AppData",
    "$RECYCLE.BIN", "System Volume Information", ".cache", "site-packages",
}
_MAX_RESULTS = 50
_MAX_SCANNED = 60_000  # keeps a runaway walk from hanging the daemon


def _roots() -> list[Path]:
    home = Path.home()
    return [home / d for d in _SEARCH_ROOTS if (home / d).exists()]


@skill(
    name="files.search",
    tier="L0",
    description="Search your user folders for files matching a name",
    examples=[
        "find my resume",
        "search for the budget spreadsheet",
        "where is my tax document",
        "look for files called invoice",
        "find that pdf about the project",
        "do I have a file named notes",
    ],
)
def search(
    query: Annotated[str, "part of the filename to look for"],
    extension: Annotated[str, "optional extension filter, e.g. 'pdf'"] = "",
) -> SkillResult:
    q = query.strip().lower()
    ext = extension.lower().lstrip(".")
    hits: list[tuple[int, Path]] = []
    scanned = 0

    for root in _roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                if scanned > _MAX_SCANNED:
                    break
                if ext and not fn.lower().endswith("." + ext):
                    continue
                score = fuzz.partial_ratio(q, fn.lower())
                if score >= 75:
                    hits.append((score, Path(dirpath) / fn))
            if scanned > _MAX_SCANNED:
                break

    hits.sort(key=lambda h: (-h[0], len(h[1].name)))
    top = hits[:_MAX_RESULTS]

    if not top:
        return SkillResult(speech=f"I couldn't find anything matching {query}.", ok=False)

    first = top[0][1]
    if len(top) == 1:
        speech = f"Found {first.name} in {first.parent.name}."
    else:
        speech = f"Found {len(top)} matches. The closest is {first.name} in {first.parent.name}."

    return SkillResult(
        speech=speech,
        data={"results": [{"path": str(p), "name": p.name, "score": s} for s, p in top]},
    )


@skill(
    name="files.read",
    tier="L0",
    description="Read the contents of a text file",
    examples=[
        "read that file",
        "show me the contents of notes.txt",
        "open and read my todo list",
        "what's in this file",
    ],
    presentational=("max_chars",),
)
def read(
    path: Annotated[str, "full path to the file"],
    max_chars: Annotated[int, "truncate beyond this many characters"] = 4000,
    offset: Annotated[int, "character position to start reading from (continue a long file)"] = 0,
) -> SkillResult:
    p = Path(path).expanduser()
    if not p.exists():
        return SkillResult(speech=f"There's no file at {path}.", ok=False)
    if not p.is_file():
        return SkillResult(speech=f"{p.name} is a folder, not a file.", ok=False)

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return SkillResult(speech=f"I couldn't read that file: {exc}", ok=False)

    offset = max(0, int(offset))
    if offset and offset >= len(text):
        return SkillResult(
            speech=f"{p.name} has only {len(text)} characters — nothing after position {offset}.",
            ok=False, data={"path": str(p), "total_chars": len(text), "offset": offset},
        )
    end = offset + max_chars
    truncated = len(text) > end
    body = text[offset:end]
    data = {"path": str(p), "content": body, "truncated": truncated, "offset": offset, "total_chars": len(text)}
    if truncated:
        # The cursor the NEXT call should use — what lets "read more" be a different
        # call rather than a repeat (friday.intent.next_page_args reads this key).
        data["next_offset"] = end

    if offset == 0:
        speech = f"{p.name} has {len(text)} characters." + (" Showing the first part." if truncated else "")
    else:
        speech = f"{p.name}: showing characters {offset}-{min(end, len(text))} of {len(text)}."
    if truncated:
        speech += f" More remains; continue with offset={end}."
    elif offset:
        speech += " That is the end of the file."  # measured live: without it the 3B model kept paging past the end
    return SkillResult(speech=speech, data=data)


@skill(
    name="files.reveal",
    tier="L1",
    action="open",
    description="Open a file or folder in File Explorer",
    examples=[
        "show that in explorer",
        "open the containing folder",
        "reveal this file",
        "open my downloads folder",
    ],
)
def reveal(
    path: Annotated[str, "file or folder to reveal"],
) -> SkillResult:
    import subprocess

    p = Path(path).expanduser()
    if not p.exists():
        return SkillResult(speech=f"There's nothing at {path}.", ok=False)

    if p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["explorer", str(p)])
    return SkillResult(speech=f"Opening {p.name} in Explorer.")
