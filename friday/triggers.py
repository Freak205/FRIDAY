"""Event triggers — the non-time half of the automation engine.

A single polling loop watches system state and emits named events. Polling
rather than OS notification hooks is deliberate: it costs a few milliseconds
every few seconds, needs no elevated privileges, and holds no OS handles open —
which matters on a memory-constrained machine.

Emitted event names (usable as a job's `trigger_spec.event`):

    battery.low          battery.critical      battery.charging
    battery.discharging  battery.full
    network.connected    network.disconnected
    idle.started         idle.ended
    window.changed       process.started       process.stopped
    files.added
"""

from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from friday import jobs
from friday.bus import BUS
from friday.log import get

log = get(__name__)

POLL_SECONDS = 5
IDLE_THRESHOLD_SECONDS = 300
BATTERY_LOW = 25
BATTERY_CRITICAL = 10


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    millis = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return millis / 1000.0


@dataclass
class WatchState:
    battery_percent: int | None = None
    plugged: bool | None = None
    online: bool | None = None
    idle: bool = False
    window_title: str = ""
    processes: set[str] = field(default_factory=set)
    watched_dirs: dict[str, set[str]] = field(default_factory=dict)


class TriggerWatcher:
    def __init__(self) -> None:
        self.state = WatchState()
        self._task: asyncio.Task | None = None
        self._watch_paths: list[Path] = []
        self._first_pass = True

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            log.info("trigger watcher started (%ds poll)", POLL_SECONDS)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def watch_folder(self, path: str) -> bool:
        p = Path(path).expanduser()
        if not p.is_dir():
            return False
        if p not in self._watch_paths:
            self._watch_paths.append(p)
            self.state.watched_dirs[str(p)] = {f.name for f in p.iterdir()}
            log.info("watching folder %s", p)
        return True

    # -- the loop ------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("trigger poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def _emit(self, event: str, **data: Any) -> None:
        # The first pass establishes a baseline; firing then would mean every
        # job runs at startup just because state was previously unknown.
        if self._first_pass:
            return
        await BUS.publish(f"trigger.{event}", **data)
        await jobs.fire_event(event, data)

    async def _poll(self) -> None:
        await self._poll_battery()
        await self._poll_network()
        await self._poll_idle()
        await self._poll_window()
        await self._poll_processes()
        await self._poll_folders()
        self._first_pass = False

    async def _poll_battery(self) -> None:
        b = psutil.sensors_battery()
        if b is None:
            return
        percent, plugged = round(b.percent), b.power_plugged
        previous, was_plugged = self.state.battery_percent, self.state.plugged
        self.state.battery_percent, self.state.plugged = percent, plugged

        if was_plugged is not None and plugged != was_plugged:
            await self._emit(
                "battery.charging" if plugged else "battery.discharging",
                percent=percent,
            )

        if previous is None or plugged:
            return

        # Edge-triggered: fire only on the crossing, not every poll below it.
        if previous > BATTERY_CRITICAL >= percent:
            await self._emit("battery.critical", percent=percent)
        elif previous > BATTERY_LOW >= percent:
            await self._emit("battery.low", percent=percent)
        elif previous < 100 <= percent:
            await self._emit("battery.full", percent=percent)

    async def _poll_network(self) -> None:
        stats = psutil.net_if_stats()
        online = any(
            s.isup for name, s in stats.items()
            if name != "Loopback Pseudo-Interface 1"
        )
        if self.state.online is not None and online != self.state.online:
            await self._emit("network.connected" if online else "network.disconnected")
        self.state.online = online

    async def _poll_idle(self) -> None:
        idle_now = idle_seconds() >= IDLE_THRESHOLD_SECONDS
        if idle_now != self.state.idle:
            await self._emit(
                "idle.started" if idle_now else "idle.ended",
                seconds=round(idle_seconds()),
            )
        self.state.idle = idle_now

    async def _poll_window(self) -> None:
        try:
            import win32gui

            title = win32gui.GetWindowText(win32gui.GetForegroundWindow())
        except Exception:
            return
        if title and title != self.state.window_title:
            previous = self.state.window_title
            self.state.window_title = title
            await self._emit("window.changed", title=title, previous=previous)

    async def _poll_processes(self) -> None:
        current = set()
        for p in psutil.process_iter(["name"]):
            try:
                if name := p.info["name"]:
                    current.add(name.lower())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if self.state.processes:
            for started in current - self.state.processes:
                await self._emit("process.started", process=started)
            for stopped in self.state.processes - current:
                await self._emit("process.stopped", process=stopped)

        self.state.processes = current

    async def _poll_folders(self) -> None:
        for path in self._watch_paths:
            key = str(path)
            try:
                now = {f.name for f in path.iterdir()}
            except OSError:
                continue
            before = self.state.watched_dirs.get(key, set())
            if added := now - before:
                await self._emit("files.added", folder=key, files=sorted(added))
            self.state.watched_dirs[key] = now


WATCHER = TriggerWatcher()
