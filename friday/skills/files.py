"""File skills: search, read, and (Phase 26) mutate.

P0 walks your user folders directly. P1 swaps the backend for the Everything SDK,
which makes full-disk search instant; the skill signatures stay the same.

The mutation skills below (`write`, `copy`, `move`, `delete`, `mkdir`) reuse the
same Executor/tier/confirmation/audit/undo architecture as every other skill —
see friday.permissions and friday.undo — rather than a filesystem-specific
mechanism. They deliberately stay out of `shell.run`'s reach: routing a file
write through an arbitrary shell command would bypass the per-call risk
classifiers below (e.g. "overwriting an existing file" escalating to a
confirm), collapsing a specific, previewable operation into an opaque one.
"""

from __future__ import annotations

import os
import shutil
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


def _overwrites(path: str, content: str = "") -> bool:
    """Risk classifier for `write`: escalates only when it would destroy
    existing content, matching browser.click's is_consequential pattern —
    the tier stays L1 (reversible) for the common create-a-new-file case."""
    return Path(path).expanduser().exists()


def _dst_exists(src: str, dst: str) -> bool:
    return Path(dst).expanduser().exists()


@skill(
    name="files.write",
    tier="L1",
    action="modify",
    description="Create a new text file, or overwrite an existing one, with the given content",
    examples=[
        "create a text file called notes.txt",
        "write this to a new file",
        "save this text as a file on my desktop",
        "make a file with this content",
        "create a new file named todo.md with this",
    ],
    dry_run=lambda path, content="": (
        f"{'Overwrite' if Path(path).expanduser().exists() else 'Create'} "
        f"{path} with {len(content)} characters"
    ),
    risk=_overwrites,
    undo=lambda path, content="": {"skill": "files.delete", "args": {"path": path}},
)
def write(
    path: Annotated[str, "full path of the file to write"],
    content: Annotated[str, "text content to write"] = "",
) -> SkillResult:
    p = Path(path).expanduser()
    if not p.parent.exists():
        return SkillResult(speech=f"There's no folder at {p.parent}.", ok=False)
    if p.exists() and p.is_dir():
        return SkillResult(speech=f"{p.name} is already a folder, not a file.", ok=False)

    existed = p.exists()
    try:
        p.write_text(content, encoding="utf-8")
    except Exception as exc:
        return SkillResult(speech=f"I couldn't write that file: {exc}", ok=False)

    verb = "Overwrote" if existed else "Created"
    return SkillResult(speech=f"{verb} {p.name}.", data={"path": str(p), "overwritten": existed})


@skill(
    name="files.copy",
    tier="L1",
    action="modify",
    description="Copy a file to a new location",
    examples=[
        "copy this file to my documents",
        "make a copy of this file",
        "duplicate this file to the desktop",
        "copy report.docx into the backups folder",
    ],
    dry_run=lambda src, dst: f"Copy {src} to {dst}",
    risk=_dst_exists,
    undo=lambda src, dst: {"skill": "files.delete", "args": {"path": dst}},
)
def copy(
    src: Annotated[str, "full path of the file to copy"],
    dst: Annotated[str, "full path (or destination folder) to copy it to"],
) -> SkillResult:
    source = Path(src).expanduser()
    if not source.exists():
        return SkillResult(speech=f"There's no file at {src}.", ok=False)
    if not source.is_file():
        return SkillResult(speech=f"{source.name} is a folder — copy each file, or use a script for whole folders.", ok=False)

    dest = Path(dst).expanduser()
    if dest.is_dir():
        dest = dest / source.name
    if not dest.parent.exists():
        return SkillResult(speech=f"There's no folder at {dest.parent}.", ok=False)

    try:
        shutil.copy2(source, dest)
    except Exception as exc:
        return SkillResult(speech=f"I couldn't copy that file: {exc}", ok=False)

    return SkillResult(speech=f"Copied {source.name} to {dest}.", data={"src": str(source), "dst": str(dest)})


@skill(
    name="files.move",
    tier="L1",
    action="modify",
    description="Move or rename a file",
    examples=[
        "move this file to my downloads",
        "rename this file to final_report.docx",
        "move invoice.pdf into the tax folder",
        "rename notes.txt to meeting_notes.txt",
    ],
    dry_run=lambda src, dst: f"Move {src} to {dst}",
    risk=_dst_exists,
    undo=lambda src, dst: {"skill": "files.move", "args": {"src": dst, "dst": src}},
)
def move(
    src: Annotated[str, "full path of the file to move"],
    dst: Annotated[str, "full destination path (or folder) to move it to"],
) -> SkillResult:
    source = Path(src).expanduser()
    if not source.exists():
        return SkillResult(speech=f"There's no file at {src}.", ok=False)
    if not source.is_file():
        return SkillResult(speech=f"{source.name} is a folder — I can only move individual files.", ok=False)

    dest = Path(dst).expanduser()
    if dest.is_dir():
        dest = dest / source.name
    if not dest.parent.exists():
        return SkillResult(speech=f"There's no folder at {dest.parent}.", ok=False)

    try:
        shutil.move(str(source), str(dest))
    except Exception as exc:
        return SkillResult(speech=f"I couldn't move that file: {exc}", ok=False)

    return SkillResult(speech=f"Moved {source.name} to {dest}.", data={"src": str(source), "dst": str(dest)})


@skill(
    name="files.delete",
    tier="L2",
    action="delete",
    description="Delete a file, or an empty folder",
    examples=[
        "delete this file",
        "remove that pdf from my downloads",
        "get rid of this file",
        "delete the empty folder called old_drafts",
        "erase draft.docx",
    ],
    dry_run=lambda path: f"Permanently delete {path}",
)
def delete(
    path: Annotated[str, "full path of the file or empty folder to delete"],
) -> SkillResult:
    p = Path(path).expanduser()
    if not p.exists():
        return SkillResult(speech=f"There's nothing at {path}.", ok=False)

    try:
        if p.is_dir():
            p.rmdir()  # refuses non-empty directories (OSError) — no recursive delete here
        else:
            p.unlink()
    except OSError as exc:
        if p.is_dir():
            return SkillResult(speech=f"{p.name} isn't empty — I won't delete a folder full of files.", ok=False)
        return SkillResult(speech=f"I couldn't delete that: {exc}", ok=False)

    return SkillResult(speech=f"Deleted {p.name}.", data={"path": str(p)})


@skill(
    name="files.mkdir",
    tier="L1",
    action="modify",
    description="Create a new folder",
    examples=[
        "create a folder called projects",
        "make a new directory named archive",
        "create a folder on my desktop called photos",
        "make a subfolder called drafts",
    ],
    dry_run=lambda path: f"Create folder {path}",
    undo=lambda path: {"skill": "files.delete", "args": {"path": path}},
)
def mkdir(
    path: Annotated[str, "full path of the folder to create"],
) -> SkillResult:
    p = Path(path).expanduser()
    if p.exists():
        return SkillResult(speech=f"{p.name} already exists.", ok=False)
    if not p.parent.exists():
        return SkillResult(speech=f"There's no folder at {p.parent}.", ok=False)

    try:
        p.mkdir()
    except Exception as exc:
        return SkillResult(speech=f"I couldn't create that folder: {exc}", ok=False)

    return SkillResult(speech=f"Created folder {p.name}.", data={"path": str(p)})
