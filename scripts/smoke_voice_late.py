"""Phase 42 -- a voice request that finishes after the wait is announced once, truthfully, and never over other speech.

Deterministic: the real `Backend` (with a fake `SESSION.handle`, as in smoke_backend_ask.py), the real BUS glue
(`friday.voice.late`), the real `VoiceSession.announce` and `ConversationLoop` -- with a fake TTS and a scripted wake
detector. No microphone, speaker, model, skill or database is involved, and nothing is re-run to fetch an answer.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from phase21_common import check, finish, scenario  # noqa: E402

from friday.bus import BUS  # noqa: E402
from friday.gui import backend as backend_mod  # noqa: E402
from friday.registry import SkillResult  # noqa: E402
from friday.voice.conversation import ConversationLoop  # noqa: E402
from friday.voice.fsm import State  # noqa: E402
from friday.voice.late import LATE_TOPIC, spoken_text, subscribe_late_announcements  # noqa: E402
from friday.voice.session import VoiceSession  # noqa: E402


class FakeTts:
    """Records what is spoken, and how many speak() calls ever overlapped."""

    def __init__(self, seconds: float = 0.05) -> None:
        self.spoken: list[str] = []
        self.seconds = seconds
        self.active = 0
        self.max_active = 0
        self.gate: threading.Event | None = None
        self._lock = threading.Lock()

    def __call__(self, text, **_):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.spoken.append(text)
        try:
            if self.gate is not None:
                self.gate.wait(3)
            time.sleep(self.seconds)
        finally:
            with self._lock:
                self.active -= 1
        return True


class ScriptedWake:
    def __init__(self) -> None:
        self.model_name = "fake_wake"
        self.feed_calls = 0
        self.reset_calls = 0

    def warm_up(self) -> None: ...

    def reset(self) -> None:
        self.reset_calls += 1

    def feed(self, block):
        self.feed_calls += 1
        return None


def make_session(tts: FakeTts) -> VoiceSession:
    return VoiceSession(capture=lambda d=None: None, transcribe=lambda a: None, handle_text=lambda t: SkillResult(speech="x"), speak=tts)


def wait_for(cond, seconds: float = 3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def main() -> int:
    t0 = time.perf_counter()
    b = backend_mod.Backend()
    threading.Thread(target=b.loop.run_forever, daemon=True, name="smoke-voice-late-loop").start()

    calls: list[str] = []

    class FakeSession:
        async def handle(self, text, *, actor="text"):
            calls.append(text)
            if text.startswith("boom"):
                await asyncio.sleep(0.4)
                raise RuntimeError("api_key=sk-abcdefghijklmnop1234 was rejected")
            if text.startswith("nope"):
                await asyncio.sleep(0.4)
                return SkillResult(speech="I could not do that.", ok=False)
            if text.startswith("secret"):
                await asyncio.sleep(0.4)
                return SkillResult(speech="Your token=ghp_abcdefghijklmnopqrstuvwxyz123456 is set.", ok=True)
            if text.startswith("quick"):
                await asyncio.sleep(0.4)
                return SkillResult(speech=f"result for {text}", ok=True)
            if text.startswith("slow"):
                await asyncio.sleep(0.9)
                return SkillResult(speech=f"result for {text}", ok=True)
            return SkillResult(speech=f"echo {text}", ok=True)

    real_session = backend_mod.SESSION
    backend_mod.SESSION = FakeSession()  # type: ignore[assignment]
    tts = FakeTts()
    session = make_session(tts)
    handler = subscribe_late_announcements(session.announce)
    b.ask_timeout_s = lambda: 0.2  # type: ignore[method-assign]
    try:
        scenario("A: a voice request inside the wait is never announced late")
        r = b.ask("hello there", actor="voice")
        time.sleep(0.6)
        check("A1 it answers directly and nothing is announced afterwards", r.ok and r.speech == "echo hello there" and tts.spoken == [], f"{r.speech!r} {tts.spoken}")

        scenario("B: timed out, then succeeds -> announced exactly once, as the final outcome")
        first = b.ask("quick one", actor="voice")
        check("B1 the 'still running' reply is not announced as an answer", first.data.get("still_running") is True and tts.spoken == [], f"{tts.spoken}")
        check("B2 when it finishes, its real result is spoken once", wait_for(lambda: len(tts.spoken) == 1), str(tts.spoken))
        time.sleep(0.4)
        check("B3 ...as the outcome (says it finished, carries its own words), not the pending text",
              len(tts.spoken) == 1 and tts.spoken[0] == "Your earlier request has finished. result for quick one" and "still running" not in tts.spoken[0].lower(), str(tts.spoken))
        check("B4 nothing was re-run to fetch it: the request ran exactly once", calls.count("quick one") == 1, str(calls))

        scenario("C: a late error / unsuccessful result is spoken as such, never as success")
        tts.spoken.clear()
        b.ask("boom a", actor="voice")
        check("C1 a late exception: spoken as an error that may have partly run; the secret in its text is redacted",
              wait_for(lambda: len(tts.spoken) == 1)
              and "error" in tts.spoken[0].lower() and "may already have run" in tts.spoken[0] and "sk-abcdefghijklmnop1234" not in tts.spoken[0]
              and "has finished" not in tts.spoken[0], str(tts.spoken))
        b.ask("nope b", actor="voice")
        check("C2 a late ok=False result: spoken as 'didn't succeed' with its own words, never 'has finished'",
              wait_for(lambda: len(tts.spoken) == 2) and tts.spoken[1] == "Your earlier request finished, but it didn't succeed. I could not do that." and "has finished" not in tts.spoken[1], str(tts.spoken))
        check("C3 a cancellation is spoken as cancelled and nothing else",
              spoken_text({"speech": "The earlier request was cancelled before it finished.", "ok": False, "cancelled": True}) == "The earlier request was cancelled before it finished.")

        scenario("D: two requests finishing in reverse order never cross")
        tts.spoken.clear()
        a = b.ask("slow A", actor="voice")   # finishes after ~0.9 s
        q = b.ask("quick B", actor="voice")  # finishes after ~0.4 s -> first
        check("D1 each has its own id", a.data["request_id"] != q.data["request_id"])
        check("D2 both are announced, in the order they finished, each with only its own words",
              wait_for(lambda: len(tts.spoken) == 2, 4.0) and tts.spoken[0].endswith("result for quick B") and tts.spoken[1].endswith("result for slow A")
              and "slow A" not in tts.spoken[0] and "quick B" not in tts.spoken[1], str(tts.spoken))
        check("D3 never two utterances at once", tts.max_active == 1, str(tts.max_active))

        scenario("E: a duplicate event does not repeat the speech")
        tts.spoken.clear()
        info = {"request_id": "dup-1", "utterance": "x", "speech": "Done it.", "ok": True, "error": False, "cancelled": False, "actor": "voice"}
        asyncio.run(BUS.publish(LATE_TOPIC, **info))
        asyncio.run(BUS.publish(LATE_TOPIC, **info))
        wait_for(lambda: len(tts.spoken) >= 1)
        time.sleep(0.4)
        check("E1 delivered twice, spoken once", tts.spoken == ["Your earlier request has finished. Done it."], str(tts.spoken))
        check("E2 and a direct second call for the same id is refused too", session.announce(info) is False and len(tts.spoken) == 1)

        scenario("F: never over other speech, never when the conversation is in progress")
        tts.spoken.clear()
        assert session._try_acquire()  # a voice cycle is running (listening / executing / speaking)
        busy = session.announce({**info, "request_id": "busy-1"})
        session._release()
        check("F1 while a cycle holds the session, the late result is NOT spoken (screen only)", busy is False and tts.spoken == [], str(tts.spoken))
        check("F2 ...and it is not queued to be read out of context later", session.announce({**info, "request_id": "busy-1"}) is False and tts.spoken == [])
        after_calls: list[int] = []
        refused = session.announce({**info, "request_id": "notidle"}, may_start=lambda: False, after=lambda: after_calls.append(1))
        check("F3 may_start()=False (conversation not idle): not spoken, post hook not run, session released", refused is False and tts.spoken == [] and not after_calls and session.busy is False)
        ok_one = session.announce({**info, "request_id": "idle-ok"}, may_start=lambda: True, after=lambda: after_calls.append(1))
        check("F4 idle: spoken, post hook run once", ok_one is True and len(tts.spoken) == 1 and after_calls == [1])
        quiet = make_session(FakeTts())
        quiet._tts_enabled = lambda: False
        check("F5 TTS disabled in config: nothing is spoken", quiet.announce({**info, "request_id": "off"}) is False)

        tts.spoken.clear()
        tts.gate = threading.Event()
        t1 = threading.Thread(target=session.announce, args=({**info, "request_id": "long-1", "speech": "First."},), daemon=True)
        t1.start()
        wait_for(lambda: tts.active == 1)
        t2 = threading.Thread(target=session.announce, args=({**info, "request_id": "long-2", "speech": "Second."},), daemon=True)
        t2.start()
        time.sleep(0.3)
        check("F6 a second announcement waits for the first to finish speaking; it never overlaps it", tts.active == 1 and len(tts.spoken) == 1 and tts.max_active == 1, f"{tts.spoken} active={tts.active}")
        tts.gate.set()
        t1.join(3); t2.join(3)
        check("F7 then it is spoken in turn", tts.spoken == ["Your earlier request has finished. First.", "Your earlier request has finished. Second."], str(tts.spoken))
        tts.gate = None

        scenario("G: privacy -- the existing redactor is applied; only voice requests are spoken; the request's own words are not read back")
        tts.spoken.clear()
        b.ask("secret thing", actor="voice")
        check("G1 a credential-shaped value in the result is not spoken", wait_for(lambda: len(tts.spoken) == 1) and "ghp_abcdefghijklmnopqrstuvwxyz123456" not in tts.spoken[0], str(tts.spoken))
        tts.spoken.clear()
        b.ask("quick typed", actor="text")
        time.sleep(1.0)
        check("G2 a TYPED request that finishes late is shown on screen only, never spoken", tts.spoken == [], str(tts.spoken))
        tts.spoken.clear()
        b.ask("quick sensitive words", actor="voice")
        check("G3 the request's own words are not read back, only the outcome", wait_for(lambda: len(tts.spoken) == 1) and "sensitive words" not in tts.spoken[0].replace("result for quick sensitive words", ""), str(tts.spoken))

        scenario("H: the conversation loop holds the wake scanner off during it, and only speaks when idle")
        wake = ScriptedWake()
        tts2 = FakeTts()
        s2 = make_session(tts2)
        loop = ConversationLoop(
            session=s2, wake_detector=wake, mic_device=None, wake_threshold=0.5, wake_cooldown_s=0.0, follow_up_enabled=False,
            follow_up_timeout_s=1.0, confirm_listen_timeout_s=1.0, post_tts_cooldown_s=0.0,
        )
        import queue as _q

        qq: _q.Queue = _q.Queue()
        block = np.zeros(480, dtype=np.float32)
        qq.put(block)
        assert s2._try_acquire()
        loop._tick(qq, State.IDLE)
        s2._release()
        check("H1 while the session is busy (an announcement or any cycle) the wake scanner scores nothing", wake.feed_calls == 0 and qq.empty())
        qq.put(block)
        loop._tick(qq, State.IDLE)
        check("H2 when it is free the wake scanner scores again", wake.feed_calls == 1)
        loop.fsm.wake_detected()
        check("H3 mid-conversation (not idle): the late result is not spoken", loop.announce_late({**info, "request_id": "h3"}) is False and tts2.spoken == [])
        loop.fsm.cancelled()
        resets = wake.reset_calls
        check("H4 idle: spoken once, and the detector is reset afterwards (its own voice cannot wake it)",
              loop.announce_late({**info, "request_id": "h4"}) is True and len(tts2.spoken) == 1 and wake.reset_calls == resets + 1, f"{tts2.spoken} {wake.reset_calls}")
    finally:
        BUS.unsubscribe(LATE_TOPIC, handler)
        backend_mod.SESSION = real_session
        b.loop.call_soon_threadsafe(b.loop.stop)

    return finish("Phase 42 -- late results announced to voice users", time.perf_counter() - t0, min_assertions=27, min_scenarios=8)


if __name__ == "__main__":
    sys.exit(main())
