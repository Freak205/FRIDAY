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

Phase 32.0: no mutation destroys a user's file without first keeping a verified copy of it under
the recovery store (friday.recovery), and the undo journal restores THAT copy. Writes go through a
temporary file + rename (a crashed process does not leave a half-written file); a failed call
journals no undo and leaves the file untouched, except in the narrow cases named under "Known
limitation" below.
"""

from __future__ import annotations

import errno
import os
import secrets
import shutil
from pathlib import Path
from typing import Annotated

from rapidfuzz import fuzz

from friday import recovery
from friday.recovery import RecoveryError, RecoveryStranded
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


def _restore_replaces(snapshot: str = "", path: str = "") -> bool:
    """A restore onto a path that exists replaces what is there (after keeping it), so it asks."""
    return Path(path).expanduser().exists()


# -- Phase 32.0: safe, recoverable mutation ------------------------------------------------------
#
# Semantics (see PLAN.md Phase 32.0 for the before/after table):
#   write   new path        publish with a no-overwrite rename; undo = delete it (itself recoverable)
#           existing file   keep a verified copy first; re-check it, then os.replace; undo = restore that copy
#   copy    same rules as write for the destination; copying a file onto itself is refused
#   move    destination file exists  -> keep a verified copy of it first (the call is already
#           confirmed by the risk classifier); a folder in the way, a link, or the same file under
#           another name -> refused. Same volume = one rename; across volumes = copy +
#           verify + remove, rolled back if the original cannot be removed. The publish to a NEW
#           destination never overwrites: a file that appears there after the check makes the move
#           refuse (os.rename / link). An approved replacement is swapped in only if the
#           destination still matched the copy that was kept when it was re-checked just before the
#           swap (the re-check and the swap are two steps; see Known limitation).
#           undo = move back, and if a destination file was replaced, restore it too
#   delete  the file is moved into FRIDAY's recovery store, not unlinked;
#           undo = restore it. A file too large to keep, or one that cannot be kept, is NOT deleted.
# A failed call journals NO undo and normally leaves the user's files untouched. The exceptions: a
# cross-volume move whose rollback itself fails (the speech says what state things are in) and a
# delete reported as stranded (the file is in the store, marked unverified).
#
# Known limitation (overwrite / replace): Windows has no atomic compare-and-replace. An approved
# overwrite keeps a verified copy of the destination (size + sha256), re-checks that the destination
# still matches it immediately before os.replace, and then replaces. That window is bounded, not
# closed: a process that modifies the destination between the re-check and os.replace has its newer
# bytes replaced with no snapshot of them, and a writer that holds the file open with share-delete
# can keep writing. A stranded delete (recovery.RecoveryStranded) and a cross-volume rollback that
# fails need a person to resolve them; the speech names the recovery id. New destinations are not
# affected: they are published with a no-overwrite rename.


def _in_store(*targets: Path) -> SkillResult | None:
    if any(recovery.in_store(t) for t in targets):
        return SkillResult(
            speech="That location is FRIDAY's recovery storage; I don't change it with file commands.", ok=False,
        )
    return None


def _destination(source: Path, dst: str) -> tuple[Path | None, str]:
    """The path `dst` means for `source`: an existing folder means 'inside it'. A destination that
    names a folder that does not exist (trailing separator) is an error, not a file called that."""
    dest = Path(dst).expanduser()
    if dest.is_dir():
        dest = dest / source.name
    elif str(dst).rstrip(" ").endswith(("/", "\\")):
        return None, f"There's no folder at {dest}."
    if not dest.parent.exists():
        return None, f"There's no folder at {dest.parent}."
    return dest, ""


def _temp_beside(target: Path) -> Path:
    return target.with_name(f".{target.name}.friday-tmp-{secrets.token_hex(4)}")


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _replace_checked(tmp: Path, target: Path, snapshot: str | None) -> None:
    """Swap `tmp` over `target` -- but only if `target` is still exactly the file whose copy was kept
    as `snapshot`. If it changed since (the user edited it, another program replaced it), the swap
    would destroy a version that is in neither the copy nor the new file, so it does not happen.
    The check and `os.replace` are two steps, not one atomic operation: the window is small, not
    zero (see Known limitation above)."""
    if snapshot is not None and target.exists() and not recovery.still_matches(snapshot, target):
        raise OSError(f"{target.name} changed after I kept its copy, so I left it alone")
    os.replace(tmp, target)


def _keep_original(target: Path, why: str) -> tuple[str | None, SkillResult | None]:
    """Keep a verified copy of the file that is about to be replaced. Failing to is a refusal."""
    try:
        return recovery.preserve(target, reason=why), None
    except RecoveryError as exc:
        return None, SkillResult(
            speech=f"I didn't change {target.name}: I couldn't keep a recoverable copy of it first ({exc}).",
            ok=False,
        )


def _undo_create(path: Path) -> dict:
    return {"skill": "files.delete", "args": {"path": str(path)}}


def _undo_restore(snapshot: str, path: Path) -> dict:
    return {"skill": "files.restore", "args": {"snapshot": snapshot, "path": str(path)}}


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
)
def write(
    path: Annotated[str, "full path of the file to write"],
    content: Annotated[str, "text content to write"] = "",
) -> SkillResult:
    p = Path(path).expanduser()
    if (refused := _in_store(p)) is not None:
        return refused
    if not p.parent.exists():
        return SkillResult(speech=f"There's no folder at {p.parent}.", ok=False)
    if p.exists() and p.is_dir():
        return SkillResult(speech=f"{p.name} is already a folder, not a file.", ok=False)
    if recovery.is_link(p):
        return SkillResult(speech=f"{p.name} is a link; I won't write through it.", ok=False)

    existed = p.exists()
    snapshot = None
    if existed:
        snapshot, refused = _keep_original(p, "overwritten by files.write")
        if refused is not None:
            return refused

    tmp = _temp_beside(p)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if existed:
            shutil.copymode(p, tmp)
            _replace_checked(tmp, p, snapshot)
        else:
            recovery.publish_new(tmp, p)  # refuses to overwrite (a file appeared meanwhile)
    except FileExistsError:
        _discard(tmp)
        return SkillResult(speech=f"A file appeared at {p} while I was writing, so I left it alone and wrote nothing.", ok=False)
    except Exception as exc:
        _discard(tmp)
        return SkillResult(speech=f"I couldn't write that file: {exc}", ok=False)

    data = {"path": str(p), "overwritten": existed}
    if existed:
        data["snapshot"] = snapshot
        return SkillResult(
            speech=f"Overwrote {p.name}. The original is kept, so I can undo this.",
            data=data, undo=_undo_restore(snapshot, p),
        )
    return SkillResult(speech=f"Created {p.name}.", data=data, undo=_undo_create(p))


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

    dest, problem = _destination(source, dst)
    if dest is None:
        return SkillResult(speech=problem, ok=False)
    if (refused := _in_store(dest)) is not None:
        return refused
    if recovery.is_link(dest):
        return SkillResult(speech=f"{dest.name} is a link; I won't copy over it.", ok=False)
    existed = dest.exists()
    if existed and dest.is_dir():
        return SkillResult(speech=f"{dest.name} is a folder; I won't copy a file over it.", ok=False)
    if existed and os.path.samefile(source, dest):
        return SkillResult(speech=f"{source.name} and {dest.name} are the same file; nothing to copy.", ok=False)

    snapshot = None
    if existed:
        snapshot, refused = _keep_original(dest, "overwritten by files.copy")
        if refused is not None:
            return refused

    tmp = _temp_beside(dest)
    try:
        shutil.copy2(source, tmp)
        if not recovery.same_content(source, tmp):
            raise OSError("the copy did not match the original")
        if existed:
            _replace_checked(tmp, dest, snapshot)
        else:
            recovery.publish_new(tmp, dest)  # refuses to overwrite (a file appeared meanwhile)
    except FileExistsError:
        _discard(tmp)
        return SkillResult(speech=f"A file appeared at {dest} while I was copying, so I left it alone and copied nothing.", ok=False)
    except Exception as exc:
        _discard(tmp)
        return SkillResult(speech=f"I couldn't copy that file: {exc}", ok=False)

    data = {"src": str(source), "dst": str(dest)}
    if existed:
        data["snapshot"] = snapshot
    return SkillResult(
        speech=f"Copied {source.name} to {dest}.", data=data,
        undo=_undo_restore(snapshot, dest) if existed else _undo_create(dest),
    )


def _is_cross_volume(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _same_as(path: Path, size: int, digest: str) -> bool:
    try:
        return path.stat().st_size == size and recovery.sha256_of(path) == digest
    except OSError:
        return False


def _move_across_volumes(
    source: Path, dest: Path, replace: bool, snapshot: str | None = None, published: dict | None = None,
) -> None:
    """Copy, verify, publish without clobbering, then remove the original -- but only if the original
    is still exactly what was copied. `published` is filled with the size/sha256 of the copy the
    moment it lands at `dest`, so the caller can tell its own copy from a file somebody else put
    there. Raises OSError, leaving `dest` as it was (the caller restores a replaced file) if the
    original could not be removed."""
    published = {} if published is None else published
    tmp = _temp_beside(dest)
    try:
        shutil.copy2(source, tmp)
        if not recovery.same_content(source, tmp):
            raise OSError("the copy did not match the original")
        size, digest = tmp.stat().st_size, recovery.sha256_of(tmp)
        if replace:
            _replace_checked(tmp, dest, snapshot)
        else:
            recovery.publish_new(tmp, dest)  # a file that appeared meanwhile is never overwritten
        published.update(size=size, sha256=digest)
    except Exception:
        _discard(tmp)
        raise
    if not _same_as(source, size, digest):
        raise OSError(f"{source.name} changed while I was moving it, so I did not remove it")
    # If this raises, the caller puts `dest` back (restores a replaced file from its snapshot, or
    # removes the stray copy): the move must not leave a duplicate of a file that never moved.
    source.unlink()


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

    dest, problem = _destination(source, dst)
    if dest is None:
        return SkillResult(speech=problem, ok=False)
    if (refused := _in_store(source, dest)) is not None:
        return refused
    if recovery.is_link(source) or recovery.is_link(dest):
        return SkillResult(speech="One of those is a link; I won't move a link.", ok=False)

    existed = dest.exists()
    if existed and os.path.samefile(source, dest):
        if os.path.abspath(source) == os.path.abspath(dest):
            return SkillResult(
                speech=f"{source.name} is already at {dest}.", data={"src": str(source), "dst": str(dest), "noop": True},
            )
        # Windows is case-insensitive: Notes.txt -> notes.txt is one file under two spellings, and a
        # case-only rename is a real rename. Any other second name for the same file is not.
        if not (source.parent.samefile(dest.parent) and source.name.lower() == dest.name.lower()):
            return SkillResult(speech=f"{source.name} and {dest.name} are the same file.", ok=False)
        existed = False
    if existed and dest.is_dir():
        return SkillResult(speech=f"{dest.name} is a folder; I won't move a file over it.", ok=False)

    snapshot = None
    if existed:
        snapshot, refused = _keep_original(dest, "replaced by files.move")
        if refused is not None:
            return refused

    published: dict = {}  # filled once OUR copy is at `dest` (cross-volume only; a rename is all-or-nothing)

    def put_back() -> str:
        """Return the destination to what it was; the words describe how that went. It only
        touches a destination it has just checked holds this move's own copy, so a file somebody
        else put there is left alone -- but the check and the restore/unlink are separate steps,
        and a change in between is not caught."""
        if not published:
            return "Nothing at the destination was changed."
        try:
            if not dest.exists():
                if snapshot is not None:
                    recovery.restore_to(snapshot, dest, replace=False)
            elif not _same_as(dest, published["size"], published["sha256"]):
                kept = f" (your original is kept as recovery id {snapshot})" if snapshot else ""
                return f"A different file is now at the destination, so I left it alone{kept}."
            elif snapshot is not None:
                recovery.restore_to(snapshot, dest, replace=True)
            elif recovery.same_content(source, dest):
                dest.unlink()
            else:
                return "A copy of the earlier version is left at the destination; the original was not removed."
            return "I put everything back as it was."
        except (RecoveryError, OSError) as exc:
            return f"I could not fully put things back ({exc})."

    try:
        if existed:
            # re-check, then os.replace: two steps, so a residual window (see Known limitation above)
            if dest.exists() and not recovery.still_matches(snapshot, dest):
                raise OSError(f"{dest.name} changed after I kept its copy, so I left both files alone")
            os.replace(source, dest)
        else:
            recovery.publish_new(source, dest)  # refuses to overwrite a file that appeared since the check
    except FileExistsError:
        return SkillResult(
            speech=f"A file appeared at {dest} while I was moving, so I left both files alone and moved nothing.", ok=False,
        )
    except OSError as exc:
        if not _is_cross_volume(exc):
            return SkillResult(speech=f"I couldn't move that file: {exc}", ok=False)
        try:
            _move_across_volumes(source, dest, replace=existed, snapshot=snapshot, published=published)
        except FileExistsError:
            return SkillResult(
                speech=f"A file appeared at {dest} while I was moving, so I left both files alone and moved nothing.", ok=False,
            )
        except OSError as inner:
            return SkillResult(speech=f"I couldn't move that file: {inner}. {put_back()}", ok=False)
    except Exception as exc:
        return SkillResult(speech=f"I couldn't move that file: {exc}", ok=False)

    back = {"skill": "files.move", "args": {"src": str(dest), "dst": str(source)}}
    data = {"src": str(source), "dst": str(dest)}
    if existed:
        data["snapshot"] = snapshot
        data["replaced"] = True
        undo = {"steps": [back, _undo_restore(snapshot, dest)]}
        return SkillResult(
            speech=f"Moved {source.name} to {dest}, replacing the file that was there. The original is kept, so I can undo this.",
            data=data, undo=undo,
        )
    return SkillResult(speech=f"Moved {source.name} to {dest}.", data=data, undo=back)


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
    dry_run=lambda path: f"Delete {path} (a recoverable copy is kept, so it can be undone)",
)
def delete(
    path: Annotated[str, "full path of the file or empty folder to delete"],
) -> SkillResult:
    p = Path(path).expanduser()
    if (refused := _in_store(p)) is not None:
        return refused
    if not p.exists() and not recovery.is_link(p):
        return SkillResult(speech=f"There's nothing at {path}.", ok=False)
    if recovery.is_link(p):
        return SkillResult(speech=f"{p.name} is a link; I won't delete a link.", ok=False)
    if p.is_dir() and any(p.iterdir()):
        return SkillResult(speech=f"{p.name} isn't empty — I won't delete a folder full of files.", ok=False)

    try:
        token = recovery.preserve(p, reason="deleted by files.delete", move=True)
    except RecoveryStranded as exc:
        # The one case where the file is no longer at its path: say exactly where it is, claim nothing.
        return SkillResult(
            speech=f"I could not safely delete {p.name}: {exc}.", ok=False,
            data={"path": str(p), "stranded_snapshot": exc.token, "recoverable": False},
        )
    except RecoveryError as exc:
        return SkillResult(
            speech=f"I didn't delete {p.name}: I couldn't keep a recoverable copy of it first ({exc}).", ok=False,
        )
    except OSError as exc:
        return SkillResult(speech=f"I couldn't delete that: {exc}", ok=False)

    return SkillResult(
        speech=f"Deleted {p.name}. A recoverable copy is kept, so I can undo this.",
        data={"path": str(p), "snapshot": token, "recoverable": True},
        undo=_undo_restore(token, p),
    )


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
    if (refused := _in_store(p)) is not None:
        return refused
    if p.exists():
        return SkillResult(speech=f"{p.name} already exists.", ok=False)
    if not p.parent.exists():
        return SkillResult(speech=f"There's no folder at {p.parent}.", ok=False)

    try:
        p.mkdir()
    except Exception as exc:
        return SkillResult(speech=f"I couldn't create that folder: {exc}", ok=False)

    return SkillResult(speech=f"Created folder {p.name}.", data={"path": str(p)})


@skill(
    name="files.restore",
    tier="L1",
    action="modify",
    internal=True,
    description="Put back a file or folder from FRIDAY's recovery copy (what undo uses)",
    dry_run=lambda snapshot, path: f"Restore {path} from recovery copy {snapshot}",
    risk=_restore_replaces,
)
def restore(
    snapshot: Annotated[str, "recovery id of the saved copy"],
    path: Annotated[str, "where to put it back"],
) -> SkillResult:
    return restore_snapshot(snapshot, path)


def restore_snapshot(snapshot: str, path: str) -> SkillResult:
    """Put recovery copy `snapshot` back at `path`, keeping whatever is there first. Shared by the
    internal `files.restore` (the undo journal) and the user-facing `files.recover`; always called
    from inside an Executor call, so permission, confirmation and audit have already applied."""
    if not recovery.is_token(snapshot):
        return SkillResult(speech="That isn't a valid recovery id.", ok=False)
    p = Path(path).expanduser()
    if (refused := _in_store(p)) is not None:
        return refused
    if not p.parent.exists():
        return SkillResult(
            speech=f"There's no folder at {p.parent}, so I haven't restored {p.name}. The recovery copy is kept.",
            ok=False,
        )
    try:
        meta = recovery.info(snapshot)
        if meta["kind"] == "dir":
            if p.exists():
                return SkillResult(speech=f"{p.name} already exists; nothing restored.", ok=False)
            recovery.restore_to(snapshot, p, replace=False)
            return SkillResult(
                speech=f"Restored folder {p.name}.", data={"path": str(p), "snapshot": snapshot, "kind": "dir"},
                undo=_undo_create(p),
            )
        if p.is_dir():
            return SkillResult(speech=f"{p.name} is a folder; nothing restored.", ok=False)
        current = None
        if p.exists():
            if recovery.sha256_of(p) == meta["sha256"]:
                return SkillResult(
                    speech=f"{p.name} already has the saved content.",
                    data={"path": str(p), "snapshot": snapshot, "kind": "file", "noop": True},
                )
            current = recovery.preserve(p, reason="replaced by files.restore")
        recovery.restore_to(snapshot, p, replace=current is not None, expect_token=current)
    except RecoveryError as exc:
        return SkillResult(speech=f"I couldn't restore {p.name}: {exc}", ok=False)
    except OSError as exc:
        return SkillResult(speech=f"I couldn't restore {p.name}: {exc}", ok=False)

    return SkillResult(
        speech=f"Restored {p.name}.", data={"path": str(p), "snapshot": snapshot, "kind": "file"},
        undo=_undo_restore(current, p) if current else _undo_create(p),
    )
