"""Known-correct solutions for every benchmark task -- the benchmark's own positive control.

`agent_benchmark.py --selftest` runs, for each task:
  * a NEGATIVE control: the task's check against an untouched environment must FAIL (except tasks whose
    correct behaviour is to change nothing -- `UNTOUCHED_OK`), and
  * a POSITIVE control: this oracle performs the task correctly through the real skills / Executor (no
    planner), then the same check must PASS.
If either is wrong the check is untrustworthy, so a baseline is only taken once the self-test is clean.
Each oracle returns (speech, result_ok): what a correct, honest FRIDAY answer would say.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bench_env import TARGET_TITLE
from bench_tasks import Env

# Correct behaviour is to leave the world unchanged (the check passes on an untouched environment).
UNTOUCHED_OK = {"fs_conflict_decline", "fs_missing_source", "br_missing_button", "br_consequential_click"}


async def X(tool: str, **args: Any) -> Any:
    from friday.permissions import EXECUTOR

    r = await EXECUTOR.run(tool, args, actor="selftest")
    if not r.ok:
        raise RuntimeError(f"oracle step {tool}({args}) failed: {r.speech}")
    return r


async def X_lenient(tool: str, **args: Any) -> Any:
    """Like X, but a reported failure is tolerated. Needed because `browser.click` on an element that NAVIGATES
    (a form's submit button) reports "no clickable element" even though the click happened -- a real FRIDAY
    defect the benchmark must not paper over, yet one the oracle has to step around to reach the real end state."""
    from friday.permissions import EXECUTOR

    return await EXECUTOR.run(tool, args, actor="selftest")


def P(env: Env, name: str) -> str:
    return str(env.work / name)


async def o_sys_time(env: Env) -> tuple[str, bool]:
    return (await X("system.time")).speech, True


async def o_sys_math(env: Env) -> tuple[str, bool]:
    return (await X("math.calculate", expression="1234 times 56")).speech, True


async def o_mkdir_write(env: Env) -> tuple[str, bool]:
    await X("files.mkdir", path=P(env, "Reports"))
    await X("files.write", path=P(env, "Reports\\summary.txt"), content="Q3 totals ready")
    return "Created the Reports folder and summary.txt.", True


async def o_copy(env: Env) -> tuple[str, bool]:
    await X("files.copy", src=P(env, "notes.txt"), dst=P(env, "notes_backup.txt"))
    return "Copied notes.txt to notes_backup.txt.", True


async def o_rename(env: Env) -> tuple[str, bool]:
    await X("files.move", src=P(env, "draft.txt"), dst=P(env, "final.txt"))
    return "Renamed draft.txt to final.txt.", True


async def o_photos(env: Env) -> tuple[str, bool]:
    await X("files.mkdir", path=P(env, "Pictures"))
    for n in ("photo1.jpg", "photo2.jpg"):
        await X("files.move", src=P(env, n), dst=P(env, "Pictures"))
    return "Moved both photos into Pictures.", True


async def o_sort(env: Env) -> tuple[str, bool]:
    await X("files.mkdir", path=P(env, "Text"))
    await X("files.mkdir", path=P(env, "Data"))
    for n in ("a.txt", "b.txt"):
        await X("files.move", src=P(env, n), dst=P(env, "Text"))
    await X("files.move", src=P(env, "data.csv"), dst=P(env, "Data"))
    return "Sorted the files into Text and Data.", True


async def o_read_value(env: Env) -> tuple[str, bool]:
    await X("files.read", path=P(env, "config.txt"))
    return "The port is 8443.", True


async def o_search_read(env: Env) -> tuple[str, bool]:
    r = await X("files.search", query="invoice")
    await X("files.read", path=r.data["results"][0]["path"])
    return "The total due is $1,284.50.", True


async def o_nothing(env: Env) -> tuple[str, bool]:
    return "", False


async def o_overwrite(env: Env) -> tuple[str, bool]:
    await X("files.copy", src=P(env, "a.txt"), dst=P(env, "b.txt"))
    return "Copied a.txt over b.txt.", True


async def o_delete(env: Env) -> tuple[str, bool]:
    await X("files.delete", path=P(env, "old_draft.txt"))
    return "Deleted old_draft.txt.", True


async def o_missing(env: Env) -> tuple[str, bool]:
    return "I couldn't find invoice_final.pdf, so I didn't copy anything.", False


async def o_br_read(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/")
    await X("browser.read")
    return "The support hotline is 555-0142.", True


async def o_br_nav(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/")
    await X("browser.click", target="Pricing")
    await X("browser.read")
    return "The Basic plan is $12 per month.", True


async def o_br_click_twice(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/counter")
    await X("browser.click", target="Add item")
    await asyncio.sleep(0.4)
    await X("browser.click", target="Add item")
    await asyncio.sleep(0.4)
    return "Clicked Add item twice.", True


async def o_br_form(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/form")
    await X("browser.type", field="Full name", text="Ada Lovelace")
    await X("browser.type", field="Email", text="ada@example.com")
    await X_lenient("browser.click", target="Register")
    await asyncio.sleep(0.5)
    return "Submitted the registration form.", True


async def o_br_search(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/search")
    await X("browser.type", field="Search products", text="router")
    await X("browser.click", target="Find")
    await X("browser.read")
    return "2 results are shown.", True


async def o_br_missing(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/")
    return "There is no Download Report button on this page.", False


async def o_br_delete_decline(env: Env) -> tuple[str, bool]:
    await X("browser.open", url=env.url + "/account")
    return "Cancelled.", False


async def o_ui_click(env: Env) -> tuple[str, bool]:
    await X("ui.click", label="Increment Counter", app=TARGET_TITLE)
    return "Clicked Increment Counter.", True


async def o_ui_fill(env: Env) -> tuple[str, bool]:
    await X("ui.fill", field="Harness Input", text="hello bench", app=TARGET_TITLE)
    return "Typed hello bench.", True


async def o_ui_right(env: Env) -> tuple[str, bool]:
    await X("ui.right_click", label="Context Target", app=TARGET_TITLE)
    return "Right-clicked Context Target.", True


async def o_ui_drag(env: Env) -> tuple[str, bool]:
    await X("ui.drag", source="Drag Source", target="Drop Target", app=TARGET_TITLE)
    return "Dragged Drag Source onto Drop Target.", True


async def o_ui_scroll(env: Env) -> tuple[str, bool]:
    await X("ui.scroll", direction="down", amount=3, app=TARGET_TITLE)
    return "Scrolled down.", True


async def o_ui_click_read(env: Env) -> tuple[str, bool]:
    await X("ui.click", label="Increment Counter", app=TARGET_TITLE)
    await X("ui.click", label="Increment Counter", app=TARGET_TITLE)
    return "The counter shows Count: 2.", True


async def o_ui_calc(env: Env) -> tuple[str, bool]:
    from bench_env import windows_titled

    await X("apps.open", app="calculator")
    deadline = time.time() + 15
    while not windows_titled("Calculator") and time.time() < deadline:
        await asyncio.sleep(0.3)
    await asyncio.sleep(1.5)
    for key in ("Clear", "Seven", "Plus", "Three", "Equals"):
        await X("ui.click", label=key, app="Calculator")
    return "7 plus 3 is 10.", True


async def o_ui_focus(env: Env) -> tuple[str, bool]:
    await X("apps.focus", app=TARGET_TITLE)
    return "Switched to Bench Test Window.", True


ORACLES = {
    "sys_time": o_sys_time, "sys_math": o_sys_math,
    "fs_mkdir_write": o_mkdir_write, "fs_copy": o_copy, "fs_rename": o_rename, "fs_group_photos": o_photos,
    "fs_sort_by_type": o_sort, "fs_read_value": o_read_value, "fs_search_read": o_search_read,
    "fs_conflict_decline": o_nothing, "fs_overwrite_approved": o_overwrite, "fs_delete_confirmed": o_delete,
    "fs_missing_source": o_missing,
    "br_read": o_br_read, "br_nav_click": o_br_nav, "br_click_twice": o_br_click_twice, "br_fill_form": o_br_form,
    "br_search_filter": o_br_search, "br_missing_button": o_br_missing, "br_consequential_click": o_br_delete_decline,
    "ui_click": o_ui_click, "ui_fill": o_ui_fill, "ui_right_click": o_ui_right, "ui_drag": o_ui_drag,
    "ui_scroll": o_ui_scroll, "ui_click_read": o_ui_click_read, "ui_calculator": o_ui_calc, "ui_focus": o_ui_focus,
}
