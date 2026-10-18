"""Command line interface.

    python run.py              interactive console (everything in-process)
    python run.py serve        run the daemon for other clients to attach to
    python run.py say "..."    one-shot command
    python run.py skills       list registered skills
    python run.py audit        show the recent action log
    python run.py recovery     list / recover / cleanup / migrate the recovery copies
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from friday import __version__, audit, paths, store
from friday.brain import BRAIN
from friday.bus import BUS, Event
from friday.config import CFG
from friday.log import setup
from friday.registry import REGISTRY
from friday.session import SESSION

cli = typer.Typer(add_completion=False, pretty_exceptions_show_locals=False, help="FRIDAY — local personal agent")
con = Console()

_TIER_STYLE = {"L0": "dim", "L1": "cyan", "L2": "yellow", "L3": "red"}


def _install_terminal_confirm(auto_approve: bool = False) -> None:
    """Answer L2/L3 confirmations at the terminal.

    Session's default handler parks the request until the next utterance, which
    is right for voice but leaves a one-shot command hanging until it times out.
    A text client can just ask.
    """
    from friday.permissions import EXECUTOR

    async def confirm(skill, args, preview: str) -> bool:
        tier_style = _TIER_STYLE.get(skill.tier, "yellow")
        con.print(f"\n  [{tier_style}]{skill.tier}[/{tier_style}] [yellow]{preview}[/yellow]")

        if auto_approve:
            con.print("  [dim]auto-approved (--yes)[/dim]")
            return True

        try:
            answer = await asyncio.to_thread(
                lambda: con.input("  [yellow]approve? [y/N] ›[/yellow] ").strip().lower()
            )
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in ("y", "yes", "yeah", "yep", "ok", "sure", "do it")

    EXECUTOR.set_confirm_handler(confirm)


async def _boot(
    quiet: bool = False, automation: bool = False, auto_approve: bool = False
) -> None:
    """Bring the brain up in-process. `automation` also starts jobs + triggers."""
    setup()
    store.init()
    REGISTRY.discover()
    if not quiet:
        with con.status("[dim]waking up...", spinner="dots"):
            await asyncio.to_thread(BRAIN.warm)
    else:
        await asyncio.to_thread(BRAIN.warm)

    _install_terminal_confirm(auto_approve)

    if automation:
        from friday.jobs import SCHEDULER
        from friday.triggers import WATCHER

        SCHEDULER.start()
        WATCHER.start()


async def _print_events(event: Event) -> None:
    """Surface the interesting parts of the pipeline while you type."""
    if event.topic == "brain.understood":
        d = event.data
        if d.get("skill"):
            con.print(
                f"  [dim]→ {d['skill']}  ({d.get('score', 0):.2f})"
                + (f"  {d['args']}" if d.get("args") else "")
                + "[/dim]"
            )
    elif event.topic == "permission.confirm_requested":
        con.print(f"  [yellow]⚠ {event.data.get('preview')}[/yellow]")
    elif event.topic == "skill.error":
        con.print(f"  [red]✗ {event.data.get('error')}[/red]")


@cli.command()
def console() -> None:
    """Interactive console. This is the fastest way to talk to FRIDAY."""

    async def main() -> None:
        await _boot(automation=True)
        BUS.subscribe("*", _print_events)

        from friday.jobs import all_jobs

        name = CFG.identity.name
        active = len([j for j in all_jobs(enabled_only=True)])
        con.print()
        con.rule(
            f"[bold]{name}[/bold] v{__version__} — "
            f"{len(REGISTRY)} skills, {active} active jobs"
        )
        con.print("[dim]Type a command. 'skills' to list, 'jobs' for automations, "
                  "'teach <skill>' to correct the last one, 'quit' to exit.[/dim]\n")

        while True:
            try:
                text = con.input("[bold cyan]you ›[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                con.print("\n[dim]bye[/dim]")
                return

            if not text:
                continue
            low = text.lower()

            if low in ("quit", "exit", "q"):
                con.print("[dim]bye[/dim]")
                return

            if low == "skills":
                _skills_table()
                continue

            if low.startswith("teach "):
                result = await SESSION.teach_last(text.split(maxsplit=1)[1].strip())
                con.print(f"[bold green]{name} ›[/bold green] {result.speech}\n")
                continue

            if low == "audit":
                _audit_table(10)
                continue

            if low == "recovery" or low.startswith("recovery "):
                parts = text.split()
                reply = await _recovery(parts[1].lower() if len(parts) > 1 else "list", parts[2] if len(parts) > 2 else "", 10)
                con.print(f"[bold green]{name} ›[/bold green] {reply.speech}\n")
                continue

            if low in ("jobs", "schedule", "automations"):
                _jobs_table()
                continue

            result = await SESSION.handle(text, actor="text")
            style = "green" if result.ok else "yellow"
            con.print(f"[bold {style}]{name} ›[/bold {style}] {result.speech}\n")

    asyncio.run(main())


@cli.command()
def say(
    text: str,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="auto-approve confirmations (for scripting)"
    ),
) -> None:
    """Run a single command and print the result."""

    async def main() -> None:
        await _boot(quiet=True, auto_approve=yes)
        result = await SESSION.handle(text, actor="text")
        con.print(result.speech)
        raise typer.Exit(0 if result.ok else 1)

    asyncio.run(main())


@cli.command()
def desktop() -> None:
    """Run FRIDAY's cinematic desktop interface, tray icon, and global hotkeys."""
    from friday.gui.app import HOTKEY, run

    con.print(f"[dim]starting FRIDAY — press {HOTKEY} anywhere[/dim]")
    run()


@cli.command()
def serve() -> None:
    """Run the daemon so other clients (tray, voice, phone) can attach."""
    from friday.daemon import serve as _serve

    con.print(f"[dim]FRIDAY daemon on {CFG.daemon.host}:{CFG.daemon.port}[/dim]")
    _serve()


@cli.command()
def skills() -> None:
    """List every registered skill."""
    setup()
    REGISTRY.discover()
    _skills_table()


@cli.command(name="audit")
def show_audit(limit: int = 20) -> None:
    """Show recent actions — what FRIDAY did, when, and whether you approved it."""
    setup()
    store.init()
    _audit_table(limit)


_RECOVERY_SKILLS = {
    "list": "files.recoveries", "recover": "files.recover",
    "cleanup": "files.recovery_cleanup", "migrate": "files.recovery_migrate",
}


async def _recovery(action: str, target: str, limit: int):
    """Run one recovery command through the Executor (permission, confirmation and audit apply).
    These skills are internal -- the planner never sees them -- so this is the way in."""
    from friday.permissions import EXECUTOR, PermissionError_
    from friday.registry import SkillResult

    skill = _RECOVERY_SKILLS.get(action)
    if skill is None:
        return SkillResult(speech="Use: recovery list | recover <id> | cleanup | migrate", ok=False)
    args: dict = {"limit": limit} if action == "list" else {}
    if action == "recover":
        if not target:
            return SkillResult(speech="Use: recovery recover <id>  (ids come from: recovery list)", ok=False)
        args = {"snapshot": target}
    try:
        return await EXECUTOR.run(skill, args, actor="text")
    except PermissionError_ as exc:
        return SkillResult(speech=f"Not allowed: {exc}", ok=False)


@cli.command(name="recovery")
def recovery_cmd(
    action: str = typer.Argument("list", help="list | recover | cleanup | migrate"),
    target: str = typer.Argument("", help="the recovery id, for `recover`"),
    limit: int = typer.Option(10, "--limit", "-n", help="how many recovery copies to list"),
    yes: bool = typer.Option(False, "--yes", "-y", help="auto-approve the confirmation (for scripting)"),
) -> None:
    """See, restore, clean up and migrate the recovery copies FRIDAY keeps before overwriting or deleting a file.

    list           read-only: ids, what happened, where from, size, status (never any file content)
    recover <id>   put one copy back where it came from (asks first; keeps whatever is there now)
    cleanup        permanently delete old copies no undo entry needs (asks first)
    migrate        copy the old in-repository data/snapshots into the current store (asks first; the
                   old folder is never changed or deleted)
    """
    async def main() -> None:
        await _boot(quiet=True, auto_approve=yes)
        result = await _recovery(action, target, limit)
        con.print(result.speech)
        raise typer.Exit(0 if result.ok else 1)

    asyncio.run(main())


@cli.command(name="db")
def db_cmd(
    action: str = typer.Argument("status", help="status | migrate | scan | scrub | compact | verify-backup | restore"),
    target: str = typer.Option("", "--target", help="scrub: which database -- active | legacy (never both at once)"),
    apply: bool = typer.Option(False, "--apply", help="scrub: really modify the target (default is a preview)"),
    confirm: str = typer.Option("", "--confirm", help="scrub --apply: the code the preview printed for this target"),
    backup_dir: str = typer.Option("", "--backup-dir", help="scrub: where the new backup folder goes (outside the repo and OneDrive)"),
    allow_synced: bool = typer.Option(False, "--allow-synced", help="scrub: allow a database inside a cloud-synced folder"),
    backup: str = typer.Option("", "--backup", help="verify-backup / restore: the friday.db.bak file"),
    to: str = typer.Option("", "--to", help="restore: a NEW file to restore into (never overwrites)"),
) -> None:
    """Where FRIDAY's database lives, move it out of a synced folder, and see/scrub the personal data it holds.

    status         read-only: the active database and any old in-repository one (sizes and row counts, no content)
    migrate        copy the old in-repository database to the active location (never overwrites, never changes or
                   deletes the old file; FRIDAY must not be running)
    scan           read-only: COUNTS of stored values that look like content (file bodies, typed text, messages, code)
                   in the active and the old database -- never the values themselves
    scrub          PREVIEW (default, read-only) of what would be removed from historical audit/episode rows, per
                   database. With --target active|legacy --apply --confirm CODE it takes a verified backup outside
                   the repository, proves it restores, then rewrites only those rows in one transaction. FRIDAY
                   must not be running. The backup keeps the sensitive history.
    compact        --target X: checkpoint + VACUUM so replaced content leaves the file (no logical change; use
                   after an interrupted scrub)
    verify-backup  --backup FILE: integrity, schema, counts and a restore rehearsal on a disposable copy
    restore        --backup FILE --to NEWFILE: write a verified copy of a backup to a new file (never overwrites)
    """
    from friday import dbplace

    action = action.lower()
    if action == "status":
        st = dbplace.status()
        for label, d in (("active", st["active"]), ("old   ", st["legacy"])):
            if not d:
                continue
            con.print(f"{label}  {d['path']}")
            con.print(f"        exists={d['exists']}  bytes={d.get('bytes', '-')}  synced-folder={d['synced_folder']}  sidecars={d.get('sidecars', [])}")
            if d.get("tables"):
                con.print("        rows: " + ", ".join(f"{k}={v}" for k, v in d["tables"].items() if v))
        if st["legacy"] and st["legacy"]["exists"] and st["legacy"]["synced_folder"]:
            con.print("[yellow]The old database is still in a synced folder. Once you have checked the active one, delete the old file yourself (FRIDAY never deletes it).[/yellow]")
    elif action == "migrate":
        try:
            m = dbplace.migrate_legacy(dbplace.resolve())
        except dbplace.DbPlacementError as exc:
            con.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        con.print(f"{m.reason}: {m.source} -> {m.dest}")
        if m.tables:
            con.print("rows: " + ", ".join(f"{k}={v}" for k, v in m.tables.items() if v))
    elif action == "scan":
        targets = [dbplace.resolve()]
        if Path(paths.LEGACY_DB).exists() and dbplace._norm(paths.LEGACY_DB) != dbplace._norm(targets[0]):
            targets.append(Path(paths.LEGACY_DB))
        for t in targets:
            if not Path(t).exists():
                continue
            con.print(f"{t}")
            for k, v in dbplace.scan_sensitive(t).items():
                if k != "database":
                    con.print(f"   {k}: {v}")
    elif action == "scrub":
        try:
            _db_scrub(target, apply, confirm, backup_dir, allow_synced)
        except (typer.Exit, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001 -- never let a traceback quote stored values
            con.print(f"db scrub failed ({type(exc).__name__}); details withheld because they may quote stored data. "
                      "Nothing was modified unless a line above says it was.", markup=False)
            raise typer.Exit(1)
    elif action == "compact":
        from friday import dbscrub

        try:
            info = dbscrub.compact(dbscrub.resolve_target(target), allow_synced=allow_synced)
            con.print(f"compacted {info['path']}: integrity={info['integrity']}, contents unchanged", markup=False, soft_wrap=True)
        except dbscrub.ScrubError as exc:
            con.print(str(exc), markup=False, soft_wrap=True)
            raise typer.Exit(1)
    elif action in ("verify-backup", "restore"):
        from friday import dbscrub

        if not backup:
            con.print("[red]--backup FILE is required[/red]")
            raise typer.Exit(2)
        try:
            if action == "verify-backup":
                snap = dbscrub.verify_backup_file(Path(backup))
                con.print(f"backup OK: integrity={snap['integrity']}, restore rehearsal passed; rows: "
                          + ", ".join(f"{k}={v}" for k, v in snap["counts"].items() if v))
            else:
                if not to:
                    con.print("[red]--to NEWFILE is required[/red]")
                    raise typer.Exit(2)
                info = dbscrub.restore(Path(backup), Path(to))
                con.print(f"restored to {info['restored']} (verified). Stop FRIDAY, set the scrubbed database AND its "
                          "-wal / -shm files aside (rename, do not delete), then move this file into its place.",
                          markup=False, soft_wrap=True)
        except dbscrub.ScrubError as exc:
            con.print(str(exc), markup=False, soft_wrap=True)
            raise typer.Exit(1)
    else:
        con.print("Use: db status | migrate | scan | scrub | verify-backup | restore")
        raise typer.Exit(2)


def _db_scrub(target: str, apply: bool, confirm: str, backup_dir: str, allow_synced: bool) -> None:
    from friday import dbscrub

    names = [target.lower()] if target else list(dbscrub.TARGETS)
    try:
        targets = [dbscrub.resolve_target(n) for n in names]
    except dbscrub.ScrubError as exc:
        con.print(str(exc), markup=False, soft_wrap=True)
        raise typer.Exit(2)
    if apply and not target:
        con.print("--apply needs an explicit --target active|legacy (the two databases are never scrubbed together)", markup=False)
        raise typer.Exit(2)
    for t in targets:
        if not t.path.is_file():
            con.print(f"[{t.name}] {t.path}: does not exist -- nothing to do", markup=False)
            continue
        pv = dbscrub.preview(t)
        for line in dbscrub.format_preview(pv):
            con.print(line, markup=False, soft_wrap=True)
        if not apply:
            continue
        if not confirm:
            con.print(f"[yellow]Preview only. To modify the {t.name} database: stop FRIDAY, then repeat with "
                      f"--apply --confirm {pv.code}[/yellow]")
            raise typer.Exit(2)
        try:
            res = dbscrub.apply(t, confirm=confirm, backup_root=Path(backup_dir) if backup_dir else None,
                                allow_synced=allow_synced)
        except dbscrub.ScrubError as exc:
            con.print(str(exc), markup=False, soft_wrap=True)
            raise typer.Exit(1)
        con.print(f"scrubbed {t.path}: " + ", ".join(f"{k}={v} rows" for k, v in res.rows_changed.items())
                  + f"; integrity={res.after['integrity']}; vacuumed={res.vacuumed}", markup=False)
        con.print(f"backup (read-only, keeps the sensitive history -- retire it deliberately): {res.backup.file}", markup=False, soft_wrap=True)
        con.print(f"    sha256={res.backup.sha256}  restore rehearsal: passed", markup=False)
    if not apply:
        con.print("Preview only: nothing was modified.")


def _skills_table() -> None:
    table = Table(box=None, pad_edge=False)
    table.add_column("tier", width=4)
    table.add_column("skill", style="bold")
    table.add_column("does", style="dim")

    for s in sorted((x for x in REGISTRY.all() if not x.internal), key=lambda x: x.name):
        table.add_row(
            f"[{_TIER_STYLE[s.tier]}]{s.tier}[/{_TIER_STYLE[s.tier]}]",
            s.name,
            s.description,
        )
    con.print()
    con.print(table)
    con.print()


@cli.command(name="jobs")
def show_jobs() -> None:
    """List scheduled and event-driven jobs."""
    setup()
    store.init()
    REGISTRY.discover()
    _jobs_table()


def _jobs_table() -> None:
    from friday.jobs import SCHEDULER, all_jobs

    rows = all_jobs()
    if not rows:
        con.print("[dim]no jobs scheduled — try: "
                  "\"every day at 8am tell me my battery\"[/dim]")
        return

    upcoming = dict(SCHEDULER.next_runs())

    table = Table(box=None, pad_edge=False)
    table.add_column("on", width=3)
    table.add_column("job", style="bold")
    table.add_column("trigger", style="dim")
    table.add_column("does", style="dim")
    table.add_column("next", style="cyan")
    table.add_column("runs", width=6)

    for j in rows:
        state = "[green]●[/green]" if j.enabled else "[dim]○[/dim]"
        when = j.trigger_spec.get("human") or j.trigger_type
        does = ", ".join(a["skill"] for a in j.actions)
        fails = f" [red]({j.fail_count} failed)[/red]" if j.fail_count else ""
        table.add_row(
            state, j.name[:28], when[:22], does[:26],
            upcoming.get(j.name, "—"), f"{j.run_count}{fails}",
        )
    con.print()
    con.print(table)
    con.print()


def _audit_table(limit: int) -> None:
    rows = audit.recent(limit)
    if not rows:
        con.print("[dim]nothing logged yet[/dim]")
        return

    table = Table(box=None, pad_edge=False)
    table.add_column("when", style="dim", width=19)
    table.add_column("skill", style="bold")
    table.add_column("by", width=9)
    table.add_column("policy", width=9)
    table.add_column("ok", width=3)
    table.add_column("result", style="dim")

    for r in reversed(rows):
        ok = "" if r["ok"] is None else ("[green]✓[/green]" if r["ok"] else "[red]✗[/red]")
        table.add_row(
            r["at"][:19].replace("T", " "),
            r["skill"],
            r["actor"],
            r["decision"],
            ok,
            (r["result"] or r["error"] or "")[:50],
        )
    con.print()
    con.print(table)
    con.print()


def main() -> None:
    import sys

    # Bare `python run.py` drops you into the console.
    if len(sys.argv) == 1:
        sys.argv.append("console")
    cli()
