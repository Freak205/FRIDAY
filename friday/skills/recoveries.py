"""The user-facing side of the recovery store (Phase 33): see what FRIDAY has kept, bring a file
back, and clean up old copies.

Four skills, all ordinary Executor calls (tier policy, confirmation, audit, undo apply). All are
`internal`: the planner is never offered them and the matcher never routes to them (a planner
choosing "clean up recovery copies" mid-goal is not something the user asked for, and three more tool
lines would cost every planner prompt context it does not have to spare). The user reaches them by
typing `recovery ...` in the console or running `python run.py recovery ...`:

  files.recoveries         L0  read-only list: id, what happened, where from, size, when, status.
                           Never any file content or hash; paths are shortened and redacted.
  files.recover            L2  put one copy back at the path it came from. The destination is fixed
                           by the copy itself (there is no path argument to steer), an occupied
                           destination is kept first and asks again, and the restore itself is
                           undoable. It shares its implementation with the internal `files.restore`
                           the undo journal uses -- which stays hidden from the planner.
  files.recovery_cleanup   L3  delete old recovery copies that nothing needs any more. It takes NO
                           arguments (a planner cannot loosen the retention policy), always asks and
                           shows what would go, and is refused for unattended actors.

  files.recovery_migrate   L2  copy the Phase 32 in-repository store into the current one, verified,
                           without ever touching the source.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from friday import recovery, retention
from friday.recovery import RecoveryError
from friday.registry import SkillResult, skill
from friday.skills.files import restore_snapshot


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _original_of(snapshot: str) -> str | None:
    try:
        return str(recovery.read_meta(snapshot).get("original_path") or "") or None
    except RecoveryError:
        return None


def _recover_occupied(snapshot: str = "") -> bool:
    """Bringing a copy back onto a path that exists replaces it (after keeping it): ask again."""
    original = _original_of(snapshot) if recovery.is_token(snapshot) else None
    try:
        return bool(original) and Path(original).expanduser().exists()
    except OSError:
        return True  # cannot tell -> ask


def _recover_preview(snapshot: str = "") -> str:
    original = _original_of(snapshot) if recovery.is_token(snapshot) else None
    where = retention.display_path(original) if original else "its original place"
    return f"Restore {where} from recovery copy {snapshot}"


@skill(
    name="files.recoveries",
    tier="L0",
    action="read",
    internal=True,
    description="List the recovery copies FRIDAY has kept of files it overwrote or deleted",
    examples=[
        "show my recovery copies",
        "what files can you restore",
        "list the files you saved before deleting",
        "what recoverable files do you have",
        "show recovery snapshots",
    ],
)
def recoveries(
    limit: Annotated[int, "how many of the newest copies to show (1-50)"] = 10,
) -> SkillResult:
    try:
        info = retention.listing(int(limit))
    except (retention.RetentionError, RecoveryError) as exc:
        return SkillResult(speech=f"I couldn't list the recovery copies: {exc}.", ok=False)
    except (TypeError, ValueError):
        return SkillResult(speech="Tell me how many to show, as a number from 1 to 50.", ok=False)

    rows = info["snapshots"]
    if not rows:
        return SkillResult(speech="There are no recovery copies.", data={**info, "snapshots": []})

    lines = [f"{info['total']} recovery cop{'y' if info['total'] == 1 else 'ies'} ({_size(info['total_bytes'])}), newest first:"]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {r['id']} — {r['operation']} {r['path']} ({_size(r['size'])}, {r['created_at'].replace('T', ' ')}) · {r['meaning']}"
        )
    if info["total"] > len(rows):
        lines.append(f"…and {info['total'] - len(rows)} older.")
    lines.append("To bring one back, type: recovery recover <id>")
    if info["eligible_for_cleanup"]:
        lines.append(
            f"{info['eligible_for_cleanup']} are older than {info['retention_days']} days and no undo needs them; "
            "I can clean those up if you ask."
        )
    if info["legacy"]:
        lines.append(f"{info['legacy']} older copies are still in the repository's data\\snapshots folder (read-only).")
    if info["synced_by_onedrive"]:
        lines.append("Note: the recovery folder is inside OneDrive, so these copies of your files may sync to the cloud.")
    return SkillResult(speech="\n".join(lines), data=info)


@skill(
    name="files.recover",
    tier="L2",
    action="modify",
    internal=True,
    description="Put a file or folder back from one of FRIDAY's recovery copies (ids come from files.recoveries)",
    examples=[
        "recover the file I deleted",
        "restore my deleted file from the recovery copy",
        "bring back the file you overwrote",
        "restore that recovery copy",
    ],
    dry_run=_recover_preview,
    risk=_recover_occupied,
)
def recover(
    snapshot: Annotated[str, "recovery id from files.recoveries"],
) -> SkillResult:
    snapshot = str(snapshot).strip()
    if not recovery.is_token(snapshot):
        return SkillResult(speech="That isn't a recovery id. Ask me to list the recovery copies to see them.", ok=False)
    original = _original_of(snapshot)
    if not original:
        return SkillResult(speech="I can't find that recovery copy, or it is damaged. Nothing was changed.", ok=False)
    return restore_snapshot(snapshot, original)


def _migrate_preview() -> str:
    try:
        out = retention.migrate_legacy(dry_run=True)
    except (OSError, RecoveryError) as exc:
        return f"Copy the old in-repository recovery copies into the current store (couldn't inspect them: {exc})"
    if not out["source"]:
        return "Copy the old in-repository recovery copies into the current store (there are none)"
    return (
        f"Copy {len(out['copied'])} recovery cop{'y' if len(out['copied']) == 1 else 'ies'} from {out['source']} "
        f"into {retention.display_path(recovery.root())} (verified; the old folder is left untouched)"
    )


@skill(
    name="files.recovery_migrate",
    tier="L2",
    action="modify",
    internal=True,
    description="Copy recovery copies from the old in-repository folder into the current store (never deletes the source)",
    dry_run=_migrate_preview,
)
def recovery_migrate() -> SkillResult:
    try:
        out = retention.migrate_legacy()
    except (OSError, RecoveryError) as exc:
        return SkillResult(speech=f"I couldn't migrate the old recovery copies: {exc}.", ok=False)
    if not out["source"]:
        return SkillResult(speech="There is no old in-repository recovery folder to migrate.", data=out)
    parts = [f"Copied {len(out['copied'])} recovery cop{'y' if len(out['copied']) == 1 else 'ies'} into the current store."]
    if out["already_there"]:
        parts.append(f"{len(out['already_there'])} were already there.")
    if out["failed"]:
        parts.append(f"{len(out['failed'])} could not be migrated and were left where they are.")
    parts.append(f"The old folder ({out['source']}) was not changed; delete it yourself once you have checked.")
    return SkillResult(speech=" ".join(parts), data=out, ok=not out["failed"])


def _cleanup_preview() -> str:
    try:
        plan = retention.make_plan(preview_window=True)
    except (retention.RetentionError, RecoveryError) as exc:
        return f"Clean up old recovery copies (I can't tell what is safe to delete: {exc}, so I would delete nothing)"
    extra = f" and first retire {plan.retire_undo} undo entries older than your undo window" if plan.retire_undo else ""
    if not plan.delete and not plan.claims:
        return f"Clean up old recovery copies (nothing qualifies right now{extra})"
    return (
        f"PERMANENTLY delete {len(plan.delete)} recovery cop{'y' if len(plan.delete) == 1 else 'ies'} "
        f"({_size(plan.bytes)}) older than {plan.days} days that no undo needs{extra}. "
        f"{plan.counts().get('pending-undo', 0)} copies kept for pending undo entries are not touched. "
        "Deleted copies cannot be brought back."
    )


@skill(
    name="files.recovery_cleanup",
    tier="L3",
    action="delete",
    internal=True,
    description="Permanently delete old recovery copies that no undo entry needs any more (asks first)",
    examples=[
        "clean up the recovery copies",
        "delete old recovery snapshots",
        "free up the space used by recovery copies",
    ],
    dry_run=_cleanup_preview,
)
def recovery_cleanup() -> SkillResult:
    report = retention.cleanup()
    ids = [d["token"] for d in report.deleted]
    data = {
        "deleted": ids, "freed_bytes": report.freed, "failed": list(report.failed),
        "retired_undo": report.retired_undo, "remaining": report.remaining,
        "finished_leftovers": report.finished_leftovers,
    }
    if report.stopped:
        return SkillResult(speech=f"I stopped the cleanup: {report.stopped}.", data=data, ok=bool(ids))
    if not ids and not report.finished_leftovers and not report.failed:
        extra = f" I retired {report.retired_undo} old undo entries." if report.retired_undo else ""
        return SkillResult(speech="There was nothing to clean up: no recovery copy is old enough and unneeded." + extra, data=data)
    parts = [f"Deleted {len(ids)} recovery cop{'y' if len(ids) == 1 else 'ies'} ({_size(report.freed)})."]
    if report.retired_undo:
        parts.append(f"Retired {report.retired_undo} undo entries older than your undo window.")
    if report.finished_leftovers:
        parts.append(f"Finished removing {report.finished_leftovers} left over from an interrupted cleanup.")
    if report.remaining:
        parts.append(f"{report.remaining} more qualify; ask again to continue.")
    if report.failed:
        parts.append(f"{len(report.failed)} could not be removed and stay for the next cleanup.")
    parts.append("Copies needed by pending undo entries were not touched.")
    return SkillResult(speech=" ".join(parts), data=data, ok=bool(ids) or not report.failed)
