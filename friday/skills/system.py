"""System skills: state, time, volume, session control."""

from __future__ import annotations

import ctypes
import platform
from datetime import datetime
from typing import Annotated

import psutil

from friday.registry import SkillResult, skill

# --------------------------------------------------------------------------
# audio endpoint helpers (pycaw / COM)
# --------------------------------------------------------------------------


def _endpoint():
    """Get the default speaker volume interface. COM must be init'd per thread."""
    from ctypes import POINTER, cast

    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    try:
        comtypes.CoInitialize()
    except Exception:
        pass  # already initialised on this thread

    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def _get_volume_pct() -> int:
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def _set_volume_pct(pct: int) -> int:
    pct = max(0, min(100, int(pct)))
    _endpoint().SetMasterVolumeLevelScalar(pct / 100.0, None)
    return pct


# --------------------------------------------------------------------------
# read-only (L0)
# --------------------------------------------------------------------------


@skill(
    name="system.time",
    tier="L0",
    description="Report the current date and time",
    examples=[
        "what time is it",
        "what's the time",
        "tell me the time",
        "what is today's date",
        "what day is it",
        "current date and time",
    ],
)
def current_time() -> SkillResult:
    now = datetime.now()
    return SkillResult(
        speech=now.strftime("It's %I:%M %p on %A, %B %d.").replace(" 0", " "),
        data={"iso": now.isoformat()},
    )


@skill(
    name="system.battery",
    tier="L0",
    description="Report battery percentage and charging state",
    examples=[
        "what's my battery",
        "battery level",
        "how much battery is left",
        "am I charging",
        "check the battery",
        "how much charge do I have",
        "is my laptop plugged in",
        "am I on power or battery",
        "how long until the battery dies",
        "is it charging right now",
    ],
)
def battery() -> SkillResult:
    b = psutil.sensors_battery()
    if b is None:
        return SkillResult(speech="This machine has no battery.", ok=False)

    pct = round(b.percent)
    if b.power_plugged:
        speech = f"Battery is at {pct} percent and charging."
    elif b.secsleft and b.secsleft > 0:
        hours, rem = divmod(b.secsleft, 3600)
        mins = rem // 60
        left = f"{hours} hours {mins} minutes" if hours else f"{mins} minutes"
        speech = f"Battery is at {pct} percent, about {left} remaining."
    else:
        speech = f"Battery is at {pct} percent."

    return SkillResult(
        speech=speech,
        data={"percent": pct, "plugged": b.power_plugged, "secsleft": b.secsleft},
    )


@skill(
    name="system.info",
    tier="L0",
    description="Report CPU, memory and disk usage",
    examples=[
        "system info",
        "how's my computer doing",
        "check system status",
        "how much memory am I using",
        "cpu usage",
        "how much disk space is left",
        "is my pc running slow",
    ],
)
def system_info() -> SkillResult:
    cpu = psutil.cpu_percent(interval=0.4)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")

    return SkillResult(
        speech=(
            f"CPU at {cpu:.0f} percent. "
            f"Memory {mem.percent:.0f} percent used, "
            f"{mem.available / 1024**3:.1f} gigabytes free. "
            f"Disk C has {disk.free / 1024**3:.0f} gigabytes free."
        ),
        data={
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "memory_available_gb": round(mem.available / 1024**3, 2),
            "disk_free_gb": round(disk.free / 1024**3, 1),
            "platform": platform.platform(),
        },
    )


@skill(
    name="system.volume.get",
    tier="L0",
    description="Report the current system volume",
    examples=["what's the volume", "how loud is it", "current volume level"],
)
def volume_get() -> SkillResult:
    pct = _get_volume_pct()
    return SkillResult(speech=f"Volume is at {pct} percent.", data={"percent": pct})


# --------------------------------------------------------------------------
# reversible writes (L1)
# --------------------------------------------------------------------------


@skill(
    name="system.volume.up",
    tier="L1",
    action="system_change",
    description="Increase the system volume",
    examples=[
        "turn up the volume",
        "volume up",
        "louder",
        "make it louder",
        "increase the volume",
        "i can't hear it",
        "turn it up",
    ],
    undo=lambda amount=10: {"skill": "system.volume.down", "args": {"amount": amount}},
)
def volume_up(
    amount: Annotated[int, "percentage points to raise, default 10"] = 10,
) -> SkillResult:
    pct = _set_volume_pct(_get_volume_pct() + amount)
    return SkillResult(speech=f"Volume {pct} percent.", data={"percent": pct})


@skill(
    name="system.volume.down",
    tier="L1",
    action="system_change",
    description="Decrease the system volume",
    examples=[
        "turn down the volume",
        "volume down",
        "quieter",
        "lower the volume",
        "make it quieter",
        "too loud",
        "turn it down",
    ],
    undo=lambda amount=10: {"skill": "system.volume.up", "args": {"amount": amount}},
)
def volume_down(
    amount: Annotated[int, "percentage points to lower, default 10"] = 10,
) -> SkillResult:
    pct = _set_volume_pct(_get_volume_pct() - amount)
    return SkillResult(speech=f"Volume {pct} percent.", data={"percent": pct})


@skill(
    name="system.volume.set",
    tier="L1",
    action="system_change",
    description="Set the system volume to a specific percentage",
    examples=[
        "set volume to 50",
        "set the volume to 30 percent",
        "put the volume at 20",
        "volume 70",
    ],
)
def volume_set(
    level: Annotated[int, "target volume, 0 to 100"],
) -> SkillResult:
    pct = _set_volume_pct(level)
    return SkillResult(speech=f"Volume set to {pct} percent.", data={"percent": pct})


@skill(
    name="system.volume.mute",
    tier="L1",
    action="system_change",
    description="Mute or unmute the system audio",
    examples=["mute", "mute the sound", "unmute", "silence", "turn the sound off"],
)
def volume_mute(
    state: Annotated[bool, "True to mute, False to unmute"] = True,
) -> SkillResult:
    _endpoint().SetMute(1 if state else 0, None)
    return SkillResult(
        speech="Muted." if state else "Unmuted.", data={"muted": state}
    )


@skill(
    name="system.lock",
    tier="L1",
    action="system_change",
    description="Lock the Windows session",
    examples=["lock the pc", "lock my computer", "lock the screen", "lock it"],
)
def lock() -> SkillResult:
    ok = bool(ctypes.windll.user32.LockWorkStation())
    return SkillResult(
        speech="Locking." if ok else "I couldn't lock the session.", ok=ok
    )


# --------------------------------------------------------------------------
# irreversible (L3) — always confirmed
# --------------------------------------------------------------------------


@skill(
    name="system.shutdown",
    tier="L3",
    action="system_change",
    description="Shut down, restart, or sign out of Windows",
    examples=["shut down the pc", "restart the computer", "reboot", "sign me out"],
    dry_run=lambda mode="shutdown", delay=10: (
        f"{mode.capitalize()} this machine in {delay} seconds"
    ),
)
def shutdown(
    mode: Annotated[str, "shutdown | restart | logoff"] = "shutdown",
    delay: Annotated[int, "seconds before it happens"] = 10,
) -> SkillResult:
    import subprocess

    flags = {"shutdown": "/s", "restart": "/r", "logoff": "/l"}
    flag = flags.get(mode)
    if flag is None:
        return SkillResult(speech=f"I don't know the mode '{mode}'.", ok=False)

    cmd = ["shutdown", flag] + ([] if flag == "/l" else ["/t", str(delay)])
    subprocess.run(cmd, check=True, capture_output=True)
    return SkillResult(speech=f"{mode.capitalize()} in {delay} seconds.")
