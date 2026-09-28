"""Phase 10.x: `request_cancel()`'s consume-once, TTL-guarded behavior — the
one change this phase makes inside friday/voice/ (see friday/voice/keys.py).
Physical Escape-key state (`GetAsyncKeyState`) is deliberately not exercised
here (no keyboard to simulate in CI) — only the GUI-settable flag layered
on top of it.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.voice import keys  # noqa: E402


def main() -> bool:
    overall = True

    def check(label: str, cond: bool) -> None:
        nonlocal overall
        print(f"  {'OK  ' if cond else 'MISS'} {label}")
        overall &= cond

    print("\n--- consume-once semantics ---\n")
    check("no cancel requested -> esc_pressed() is False", keys.esc_pressed() is False)
    keys.request_cancel()
    check("request_cancel() -> esc_pressed() True on first poll", keys.esc_pressed() is True)
    check("...and False on the very next poll (consumed exactly once)", keys.esc_pressed() is False)

    print("\n--- TTL expiry: a click that lands after its own cycle already ended ---\n")
    keys.request_cancel()
    time.sleep(keys._GUI_CANCEL_TTL_S + 0.3)
    check(
        "an unconsumed request expires after the TTL instead of cancelling a later cycle",
        keys.esc_pressed() is False,
    )

    print("\n--- a fresh request after expiry still works normally ---\n")
    keys.request_cancel()
    check("esc_pressed() True immediately after a fresh request", keys.esc_pressed() is True)

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    return overall


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
