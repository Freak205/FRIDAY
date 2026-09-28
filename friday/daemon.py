"""The FRIDAY daemon.

One long-lived process. Clients (CLI, tray, overlay, voice, phone bridge) connect
over HTTP + WebSocket on localhost, so every interface takes an identical path
through the brain and the permission guard.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from friday import __version__, audit, store
from friday.brain import BRAIN
from friday.bus import BUS
from friday.config import CFG
from friday.log import get, setup
from friday.registry import REGISTRY
from friday.session import SESSION

log = get(__name__)


class Say(BaseModel):
    text: str
    actor: str = "text"


class Teach(BaseModel):
    skill: str
    utterance: str | None = None


class Invoke(BaseModel):
    skill: str
    args: dict[str, Any] = {}
    actor: str = "text"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    setup()
    log.info("FRIDAY %s starting", __version__)
    store.init()
    REGISTRY.discover()
    await asyncio.to_thread(BRAIN.warm)

    from friday.jobs import SCHEDULER
    from friday.triggers import WATCHER

    SCHEDULER.start()
    WATCHER.start()

    log.info("ready — %d skills, listening on %s:%s",
             len(REGISTRY), CFG.daemon.host, CFG.daemon.port)
    await BUS.publish("daemon.ready", skills=len(REGISTRY))
    yield

    WATCHER.stop()
    SCHEDULER.stop()

    from friday import browser

    if browser.is_open():
        await browser.close()

    log.info("FRIDAY shutting down")


app = FastAPI(title="FRIDAY", version=__version__, lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "version": __version__,
        "skills": len(REGISTRY),
        "voice": CFG.voice.enabled,
        "pending": SESSION.pending.kind if SESSION.pending else None,
    }


@app.get("/skills")
async def skills() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "tier": s.tier,
            "description": s.description,
            "params": [
                {"name": p.name, "required": p.required, "description": p.description}
                for p in s.params
            ],
            "examples": s.examples,
        }
        for s in sorted(REGISTRY.all(), key=lambda x: x.name)
    ]


@app.post("/say")
async def say(body: Say) -> dict[str, Any]:
    """The main entry point: natural language in, result out."""
    result = await SESSION.handle(body.text, actor=body.actor)
    return {"speech": result.speech, "ok": result.ok, "data": result.data}


@app.post("/invoke")
async def invoke(body: Invoke) -> dict[str, Any]:
    """Call a skill directly, bypassing the brain but not the permission guard.

    Phase 15.0: found on the real machine — a denied call (e.g. an
    unattended actor over its tier ceiling) raised `PermissionError_`
    uncaught, turning a routine policy decision into a 500 Internal Server
    Error instead of the same clean `{"ok": false, ...}` shape `/say`
    already returns for the identical denial via `Session._run`'s own
    `except PermissionError_`. Mirrors that handling here rather than
    inventing a new error contract.
    """
    from friday.permissions import EXECUTOR, PermissionError_

    try:
        result = await EXECUTOR.run(body.skill, body.args, actor=body.actor)
    except PermissionError_ as exc:
        return {"speech": f"I'm not allowed to do that. {exc}", "ok": False, "data": {}}
    except KeyError:
        return {"speech": "That skill isn't available.", "ok": False, "data": {}}
    return {"speech": result.speech, "ok": result.ok, "data": result.data}


@app.post("/cancel")
async def cancel() -> dict[str, Any]:
    """Ask whatever `plan.run` goal is currently running to stop at its next
    checkpoint (Phase 16.0). Cooperative, not forced: the running `/say` (or
    equivalent) request keeps its own task and returns normally, reporting
    `stopped="cancelled"` with whatever evidence it already gathered — see
    `friday.orchestrator.Orchestrator.run_goal`'s `cancel_check` parameter.
    Safe to call with nothing running; it just arms a flag the next
    `start_goal` clears."""
    from friday.intelligence.state import INTEL

    INTEL.request_cancel()
    return {"ok": True, "speech": "Cancellation requested."}


@app.post("/teach")
async def teach(body: Teach) -> dict[str, Any]:
    if body.utterance:
        BRAIN.teach(body.utterance, body.skill)
        return {"ok": True, "speech": f"\"{body.utterance}\" now means {body.skill}."}
    result = await SESSION.teach_last(body.skill)
    return {"ok": result.ok, "speech": result.speech}


@app.get("/audit")
async def recent_audit(limit: int = 20) -> list[dict[str, Any]]:
    return audit.recent(limit)


@app.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    """Bidirectional channel: send utterances, receive every bus event."""
    await ws.accept()
    queue = BUS.stream()

    async def pump() -> None:
        while True:
            event = await queue.get()
            await ws.send_json(
                {"topic": event.topic, "data": event.data, "at": event.at.isoformat()}
            )

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            msg = await ws.receive_json()
            if text := msg.get("text"):
                result = await SESSION.handle(text, actor=msg.get("actor", "text"))
                await ws.send_json(
                    {"topic": "result", "data": {"speech": result.speech, "ok": result.ok}}
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("websocket error")
    finally:
        pump_task.cancel()
        BUS.drop(queue)


def serve() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=CFG.daemon.host,
        port=CFG.daemon.port,
        log_config=None,
        access_log=False,
    )
