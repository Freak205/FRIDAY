"""Hardware and OS state: brightness, network, processes, clipboard, power."""

from __future__ import annotations

import subprocess
from typing import Annotated

import psutil

from friday.log import get
from friday.registry import SkillResult, skill

log = get(__name__)


def _ps(command: str) -> str:
    """Run a PowerShell one-liner and return stdout."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, timeout=20,
    )
    return out.stdout.strip()


# --------------------------------------------------------------------------
# brightness
# --------------------------------------------------------------------------


# Pinning the backend avoids probing every method on each call, which is what
# leaves COM objects to be torn down at interpreter exit ("Win32 exception
# occurred releasing IUnknown" on stderr). Resolved once, then reused.
_BRIGHTNESS_METHOD: str | None = None


def _brightness_method() -> str | None:
    global _BRIGHTNESS_METHOD
    if _BRIGHTNESS_METHOD is not None:
        return _BRIGHTNESS_METHOD or None

    import screen_brightness_control as sbc

    for candidate in ("wmi", "vcp"):
        try:
            if sbc.get_brightness(method=candidate):
                _BRIGHTNESS_METHOD = candidate
                return candidate
        except Exception:
            continue

    _BRIGHTNESS_METHOD = ""  # cached negative: no backend works here
    return None


def _get_brightness() -> int | None:
    try:
        import screen_brightness_control as sbc

        method = _brightness_method()
        values = sbc.get_brightness(method=method) if method else sbc.get_brightness()
        return int(values[0]) if values else None
    except Exception:
        log.debug("brightness read failed", exc_info=True)
        return None


def _set_brightness(level: int) -> int | None:
    level = max(0, min(100, int(level)))
    try:
        import screen_brightness_control as sbc

        method = _brightness_method()
        if method:
            sbc.set_brightness(level, method=method)
        else:
            sbc.set_brightness(level)
        return level
    except Exception:
        log.debug("brightness write failed", exc_info=True)
        return None


@skill(
    name="system.brightness.get",
    tier="L0",
    description="Report the current screen brightness",
    examples=["what's the brightness", "how bright is the screen", "brightness level"],
)
def brightness_get() -> SkillResult:
    level = _get_brightness()
    if level is None:
        return SkillResult(speech="I can't read the brightness on this display.", ok=False)
    return SkillResult(speech=f"Brightness is at {level} percent.", data={"percent": level})


@skill(
    name="system.brightness.set",
    tier="L1",
    action="system_change",
    description="Set the screen brightness to a specific level",
    examples=[
        "set brightness to 50",
        "brightness 70",
        "put the brightness at 30 percent",
    ],
)
def brightness_set(
    level: Annotated[int, "target brightness, 0 to 100"],
) -> SkillResult:
    result = _set_brightness(level)
    if result is None:
        return SkillResult(speech="I couldn't change the brightness.", ok=False)
    return SkillResult(speech=f"Brightness {result} percent.", data={"percent": result})


@skill(
    name="system.brightness.up",
    tier="L1",
    action="system_change",
    description="Increase the screen brightness",
    examples=[
        "brighter", "turn up the brightness", "increase brightness",
        "the screen is too dark", "brighten the screen",
    ],
    undo=lambda amount=20: {"skill": "system.brightness.down", "args": {"amount": amount}},
)
def brightness_up(
    amount: Annotated[int, "percentage points to raise"] = 20,
) -> SkillResult:
    current = _get_brightness()
    if current is None:
        return SkillResult(speech="I can't control this display's brightness.", ok=False)
    return brightness_set(current + amount)


@skill(
    name="system.brightness.down",
    tier="L1",
    action="system_change",
    description="Decrease the screen brightness",
    examples=[
        "dimmer", "turn down the brightness", "decrease brightness",
        "the screen is too bright", "dim the screen",
    ],
    undo=lambda amount=20: {"skill": "system.brightness.up", "args": {"amount": amount}},
)
def brightness_down(
    amount: Annotated[int, "percentage points to lower"] = 20,
) -> SkillResult:
    current = _get_brightness()
    if current is None:
        return SkillResult(speech="I can't control this display's brightness.", ok=False)
    return brightness_set(current - amount)


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------


@skill(
    name="network.status",
    tier="L0",
    description="Report network connectivity and the current Wi-Fi network",
    examples=[
        "am I online",
        "what's my network",
        "which wi-fi am I on",
        "is the internet working",
        "check my connection",
        "network status",
    ],
)
def network_status() -> SkillResult:
    stats = psutil.net_if_stats()
    active = [name for name, s in stats.items() if s.isup and name != "Loopback Pseudo-Interface 1"]

    ssid = ""
    signal = ""
    try:
        raw = _ps("netsh wlan show interfaces")
        for line in raw.splitlines():
            low = line.strip().lower()
            if low.startswith("ssid") and "bssid" not in low:
                ssid = line.split(":", 1)[1].strip()
            elif low.startswith("signal"):
                signal = line.split(":", 1)[1].strip()
    except Exception:
        log.debug("wlan query failed", exc_info=True)

    if ssid:
        speech = f"Connected to {ssid}" + (f" at {signal} signal." if signal else ".")
    elif active:
        speech = f"Connected via {active[0]}."
    else:
        speech = "You don't appear to be connected."

    io = psutil.net_io_counters()
    return SkillResult(
        speech=speech,
        data={
            "ssid": ssid,
            "signal": signal,
            "interfaces": active,
            "sent_mb": round(io.bytes_sent / 1024**2, 1),
            "recv_mb": round(io.bytes_recv / 1024**2, 1),
        },
    )


@skill(
    name="network.wifi.toggle",
    tier="L2",
    action="system_change",
    description="Turn the Wi-Fi adapter on or off",
    examples=["turn off wi-fi", "disable wifi", "turn wi-fi back on", "enable wifi"],
    dry_run=lambda enable=True: f"{'Enable' if enable else 'Disable'} the Wi-Fi adapter",
    undo=lambda enable=True: {"skill": "network.wifi.toggle", "args": {"enable": not enable}},
)
def wifi_toggle(
    enable: Annotated[bool, "True to enable, False to disable"] = True,
) -> SkillResult:
    action = "enable" if enable else "disable"
    out = _ps(
        "Get-NetAdapter | Where-Object {$_.Name -like '*Wi-Fi*' -or "
        f"$_.InterfaceDescription -like '*Wireless*'}} | {action.capitalize()}-NetAdapter -Confirm:$false"
    )
    return SkillResult(
        speech=f"Wi-Fi {action}d.", data={"enabled": enable, "output": out}
    )


# --------------------------------------------------------------------------
# processes
# --------------------------------------------------------------------------


@skill(
    name="process.top",
    tier="L0",
    description="List the processes using the most CPU or memory",
    examples=[
        "what's using my cpu",
        "what's eating my memory",
        "show me the heaviest processes",
        "why is my pc slow",
        "top processes",
        "what's hogging resources",
    ],
)
def top_processes(
    by: Annotated[str, "'memory' or 'cpu'"] = "memory",
    count: Annotated[int, "how many to list"] = 5,
) -> SkillResult:
    procs = []
    for p in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
        try:
            info = p.info
            procs.append({
                "pid": p.pid,
                "name": info["name"] or "?",
                "memory_mb": round((info["memory_info"].rss if info["memory_info"] else 0) / 1024**2, 1),
                "cpu": info["cpu_percent"] or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    key = "cpu" if by.startswith("cpu") else "memory_mb"
    procs.sort(key=lambda x: -x[key])
    top = procs[:count]

    if key == "memory_mb":
        listed = ", ".join(f"{p['name']} at {p['memory_mb']:.0f} megabytes" for p in top[:3])
        speech = f"Heaviest on memory: {listed}."
    else:
        listed = ", ".join(f"{p['name']} at {p['cpu']:.0f} percent" for p in top[:3])
        speech = f"Heaviest on CPU: {listed}."

    return SkillResult(speech=speech, data={"processes": top})


@skill(
    name="process.kill",
    tier="L2",
    action="delete",
    description="Terminate a running process by name",
    examples=[
        "kill chrome",
        "force quit spotify",
        "end that process",
        "terminate notepad",
        "stop the frozen app",
    ],
    dry_run=lambda name, force=False: f"Terminate every process named '{name}'",
)
def kill_process(
    name: Annotated[str, "process name, e.g. 'chrome.exe'"],
    force: Annotated[bool, "kill instead of asking it to close"] = False,
) -> SkillResult:
    target = name.lower().removesuffix(".exe")
    killed = []

    for p in psutil.process_iter(["name"]):
        try:
            pname = (p.info["name"] or "").lower().removesuffix(".exe")
            if pname == target:
                p.kill() if force else p.terminate()
                killed.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not killed:
        return SkillResult(speech=f"I don't see anything called {name} running.", ok=False)
    return SkillResult(
        speech=f"Stopped {len(killed)} {name} process{'es' if len(killed) > 1 else ''}.",
        data={"pids": killed},
    )


# --------------------------------------------------------------------------
# clipboard
# --------------------------------------------------------------------------


@skill(
    name="clipboard.read",
    tier="L0",
    description="Read the current clipboard contents",
    examples=[
        "what's in my clipboard",
        "read the clipboard",
        "what did I copy",
        "show me what's copied",
    ],
)
def clipboard_read() -> SkillResult:
    import win32clipboard

    try:
        win32clipboard.OpenClipboard()
        try:
            text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return SkillResult(speech="The clipboard is empty or holds something I can't read.", ok=False)

    preview = text[:200] + ("…" if len(text) > 200 else "")
    return SkillResult(
        speech=f"Clipboard has {len(text)} characters: {preview}",
        data={"text": text, "length": len(text)},
    )


@skill(
    name="clipboard.write",
    tier="L1",
    action="modify",
    description="Copy text to the clipboard",
    examples=["copy this to my clipboard", "put that on the clipboard", "copy that text"],
)
def clipboard_write(
    text: Annotated[str, "text to place on the clipboard"],
) -> SkillResult:
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()

    return SkillResult(speech="Copied.", data={"length": len(text)})


# --------------------------------------------------------------------------
# power
# --------------------------------------------------------------------------


@skill(
    name="system.power_plan",
    tier="L1",
    action="system_change",
    description="Report or change the Windows power plan",
    examples=[
        "what power plan am I on",
        "switch to high performance",
        "set power saver mode",
        "change to balanced power",
    ],
)
def power_plan(
    plan: Annotated[str, "'balanced', 'high performance', 'power saver', or blank to report"] = "",
) -> SkillResult:
    if not plan:
        out = _ps("powercfg /getactivescheme")
        name = out.split("(")[-1].rstrip(")") if "(" in out else out
        return SkillResult(speech=f"You're on the {name} power plan.", data={"plan": name})

    guids = {
        "balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
        "high performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
        "power saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
    }
    key = next((k for k in guids if k in plan.lower()), None)
    if key is None:
        return SkillResult(speech=f"I don't recognise the plan '{plan}'.", ok=False)

    subprocess.run(["powercfg", "/setactive", guids[key]], capture_output=True, timeout=15)
    return SkillResult(speech=f"Switched to {key}.", data={"plan": key})
