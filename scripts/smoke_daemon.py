"""Phase 15.0 — the daemon's HTTP surface (`friday/daemon.py`), in-process.

New this phase: found on the real machine, not by inspection. `/invoke`
(direct skill call, bypassing the brain but not the permission guard) let
`friday.permissions.PermissionError_` propagate uncaught into a 500 Internal
Server Error instead of the same clean `{"ok": false, ...}` shape `/say`
already returns for an identical denial via `Session._run`'s own
`except PermissionError_`. No prior smoke test covered `/invoke` at all, so
this is a new, minimal file rather than an addition to an existing one —
covers just this endpoint's error handling plus enough of `/say`/`/health`
to prove the app boots and the fix didn't disturb the happy path.

Uses FastAPI's TestClient (httpx-based, already a dependency) against the
real `app` object — no real network socket, no separate daemon process.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from friday import store  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402


def main() -> None:
    store.init()
    REGISTRY.discover()

    from friday.daemon import app

    overall = True
    with TestClient(app) as client:
        print("\n--- A: /health boots cleanly ---\n")
        resp = client.get("/health")
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        print(f"  {'OK  ' if ok else 'MISS'} /health -> {resp.status_code} {resp.json()}")
        overall &= ok

        print("\n--- B: /say happy path (read-only skill) ---\n")
        resp = client.post("/say", json={"text": "what time is it", "actor": "text"})
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        print(f"  {'OK  ' if ok else 'MISS'} /say -> {resp.status_code} {resp.json().get('speech')}")
        overall &= ok

        print("\n--- C: /invoke happy path (direct call, L0 skill) ---\n")
        resp = client.post("/invoke", json={"skill": "system.time", "actor": "text"})
        ok = resp.status_code == 200 and resp.json().get("ok") is True
        print(f"  {'OK  ' if ok else 'MISS'} /invoke -> {resp.status_code} {resp.json().get('speech')}")
        overall &= ok

        print("\n--- D: /invoke on an unknown skill (KeyError) fails cleanly ---\n")
        resp = client.post("/invoke", json={"skill": "not.a.real.skill", "actor": "text"})
        ok = resp.status_code == 200 and resp.json().get("ok") is False
        print(f"  {'OK  ' if ok else 'MISS'} /invoke unknown skill -> {resp.status_code} {resp.json()}")
        overall &= ok

        print("\n--- E: /invoke permission denial returns clean JSON, not a 500 ---\n")
        # Regression for the real defect: an unattended actor (`scheduler`)
        # requesting an L2 (confirm-tier -> denied outright when unattended)
        # skill used to crash the whole HTTP response.
        resp = client.post(
            "/invoke", json={"skill": "apps.close", "args": {"app": "notepad"}, "actor": "scheduler"},
        )
        body = resp.json() if resp.status_code == 200 else None
        ok = (
            resp.status_code == 200 and body is not None
            and body.get("ok") is False and "not allowed" in body.get("speech", "").lower()
        )
        print(f"  {'OK  ' if ok else 'MISS'} /invoke permission denial -> {resp.status_code} {body}")
        overall &= ok

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    main()
