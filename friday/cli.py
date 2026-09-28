"""Command line interface.

    python run.py              interactive console (everything in-process)
    python run.py serve        run the daemon for other clients to attach to
    python run.py say "..."    one-shot command
    python run.py skills       list registered skills
    python run.py audit        show the recent action log
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from friday import __version__, audit, store
from friday.brain import BRAIN
from friday.bus import BUS, Event
from friday.config import CFG
from friday.log import setup
from friday.registry import REGISTRY
from friday.session import SESSION

cli = typer.Typer(add_completion=False, help="FRIDAY — local personal agent")
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


def _skills_table() -> None:
    table = Table(box=None, pad_edge=False)
    table.add_column("tier", width=4)
    table.add_column("skill", style="bold")
    table.add_column("does", style="dim")

    for s in sorted(REGISTRY.all(), key=lambda x: x.name):
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
