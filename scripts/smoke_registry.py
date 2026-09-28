"""Smoke test: load every skill and print the registry."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.permissions import EXECUTOR  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402


def show() -> None:
    REGISTRY.discover()
    print(f"\n{len(REGISTRY)} skills registered\n")
    for s in sorted(REGISTRY.all(), key=lambda x: x.name):
        params = ", ".join(p.name + ("" if p.required else "?") for p in s.params)
        print(f"  [{s.tier}] {s.name:26} ({params:34}) ex={len(s.examples)}")


async def try_calls() -> None:
    print("\n--- executing a few L0/L1 skills ---\n")
    for name, args in [
        ("system.time", {}),
        ("system.battery", {}),
        ("system.info", {}),
        ("system.volume.get", {}),
        ("screen.active_window", {}),
        ("apps.list", {}),
    ]:
        try:
            result = await EXECUTOR.run(name, args, actor="test")
            print(f"  OK   {name:26} -> {result.speech}")
        except Exception as exc:
            print(f"  FAIL {name:26} -> {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    show()
    asyncio.run(try_calls())
