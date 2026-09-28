"""Manual real-microphone test for FRIDAY's conversational voice mode.

Unlike scripts/smoke_voice_conversation.py (fully deterministic, no
hardware, no model), this actually opens your microphone, loads the real
wake-word model, runs real commands through FRIDAY's real
SESSION/BRAIN/EXECUTOR, and speaks through your speakers. With the Phase
10.X.2 default (`voice.follow_up_enabled: false`, i.e. strict "Hey Jarvis"
gating), it guides you through:

    1. ignored speech (no wake word -> nothing happens)
    2. wake phrase ("hey jarvis")
    3. simple command right after waking it
    4. a second command with NO wake word -> must be ignored
    5. a second independent "hey jarvis" activation -> must work
    6. interruption (Esc while FRIDAY is speaking)
    7. a confirmation-required command

If `voice.follow_up_enabled: true` is set in config.yaml, this instead walks
through the legacy Phase 8 flow (a follow-up command with no wake word
should be heard, and silence should time out quietly) — the printed
instructions below adapt to whichever mode is actually configured.

For (6), this registers one harmless, temporary test skill
(`test.manual_confirmation_demo` — tier L3, does nothing but print/speak
that it ran) rather than pointing you at a real consequential skill like
WhatsApp send — the point is to prove voice confirmation works, not to
actually message someone or delete something while testing. Nothing here
runs a destructive or consequential *real* action automatically; every
command in this session, real or demo, still goes through the exact same
confirmation gate normal use would.

The demo phrase is dispatched directly to that one test skill rather than
via `BRAIN.teach()` — teaching is permanent (it writes to your real
data/friday.db and rebuilds your real matcher cache), which would leave a
dangling taught phrase pointing at a skill that no longer exists the moment
this script exits. Every other phrase in this session still goes through
the real BRAIN/EXECUTOR exactly as production does.

Usage:
    python scripts/smoke_conversation.py

Press Ctrl+C to stop.
"""

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.log import setup  # noqa: E402
from friday.registry import REGISTRY, SkillResult, skill  # noqa: E402
from friday.session import SESSION  # noqa: E402

DEMO_PHRASE = "run the confirmation demo"
DEMO_SKILL = "test.manual_confirmation_demo"


def _register_demo_skill() -> None:
    @skill(
        name=DEMO_SKILL,
        tier="L3",
        description="harmless demo action for manually testing voice confirmation "
        "(scripts/smoke_conversation.py) — does nothing but report that it ran",
    )
    def _demo() -> SkillResult:
        return SkillResult(speech="Demo action completed. Nothing real happened.")


class Backend:
    """Minimal stand-in for friday.gui.backend.Backend — just enough asyncio
    plumbing to run SESSION.handle() from the conversation loop's threads.
    """

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="friday-core")

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def ask(self, text: str) -> SkillResult:
        # The demo phrase is dispatched straight to Session._run (still the
        # real EXECUTOR/confirmation/audit path — see the module docstring
        # for why this skips BRAIN matching specifically for this one test
        # phrase instead of BRAIN.teach()'s permanent alternative) so it
        # doesn't need a taught intent match. Everything else goes through
        # the exact same SESSION.handle() any real command would.
        if text.strip().lower() == DEMO_PHRASE:
            coro = SESSION._run(DEMO_SKILL, {}, actor="voice")  # noqa: SLF001
        else:
            coro = SESSION.handle(text, actor="voice")
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=120)


def main() -> int:
    setup()
    store.init()
    print("\nFRIDAY Phase 8 conversation-mode manual test\n" + "=" * 44)

    print("\nLoading brain (skills, embedding matcher)...")
    REGISTRY.discover()
    BRAIN.warm()
    _register_demo_skill()

    backend = Backend()
    backend.start()

    from friday.voice.conversation import build_conversation_loop

    def on_state(state: str, **data: object) -> None:
        if state.startswith("conversation."):
            label = state.removeprefix("conversation.")
            if label == "idle":
                return  # too noisy — every interaction ends here
            print(f"\n[state] {label}")
        elif state == "executing":
            print(f"[heard] {data.get('text', '')!r}")
        elif state == "speaking":
            print(f"[FRIDAY] {data.get('text', '')}")
        elif state == "error":
            print(f"[error] {data.get('message', '')}")
        elif state in ("cancelled", "empty"):
            print(f"[{state}]")

    try:
        loop = build_conversation_loop(handle_text=backend.ask, on_state=on_state)
    except Exception as exc:
        print(f"\nFAILED to build the conversation loop: {exc}")
        return 1

    from friday.hotkey import HOTKEYS

    HOTKEYS.bind(CFG.voice.activation_hotkey, loop.activate_from_hotkey)

    strict = not CFG.voice.follow_up_enabled
    wake_phrase = CFG.voice.wakeword.model.replace("_", " ")

    if strict:
        steps = f"""
  1) IGNORED SPEECH (the gate)
     Say something completely unrelated, e.g. "hello" or "open Chrome",
     WITHOUT the wake phrase. Nothing should happen — no [state], no
     [heard], no [FRIDAY]. This is the most important check: strict mode
     means ordinary speech never reaches STT/the brain/TTS.

  2) WAKE PHRASE
     Say "{wake_phrase}" and wait for the "wake_detected" state below.
     (No Ctrl+Alt+V needed for this one.)

  3) SIMPLE COMMAND
     Right after waking it, say something harmless: "what time is it".
     You should see [heard], then [FRIDAY] speak the answer.
     (Also try it as one breath: "{wake_phrase}, what time is it".)

  4) NO AUTOMATIC FOLLOW-UP
     Immediately after FRIDAY answers, say another command with NO wake
     word: "what's my battery level". It must be IGNORED — no [heard],
     no [FRIDAY]. You should see it go straight back to wake-word standby.

  5) SECOND INDEPENDENT ACTIVATION
     Say "{wake_phrase}" again, then "what's my battery level". This
     time it must work, on its own, exactly like step 2-3 did.

  6) INTERRUPTION
     Wake it, trigger a command with a longer spoken response (e.g.
     "what can you do"), then press Esc partway through FRIDAY speaking
     it. Speech should stop immediately, and it should return to
     wake-word standby (no dangling follow-up window).

  7) CONFIRMATION-REQUIRED COMMAND
     Wake it, then say: "{DEMO_PHRASE}"
     FRIDAY should ask "...should I go ahead?" — answer "yes" or "no" by
     voice (no wake word needed for THIS answer — it's resolving the
     question FRIDAY just asked, not a new independent command) and
     confirm it either runs the harmless demo or cancels, matching your
     answer. Afterward it should return to wake-word standby — a
     following command again needs "{wake_phrase}".

  8) BARGE-IN (Phase 10.X.3)
     Wake it, trigger a command with a longer spoken response (e.g.
     "what can you do"). While FRIDAY is actively speaking it, say
     "{wake_phrase}" again. Speech should stop IMMEDIATELY (not after
     finishing the sentence), you should hear the activation chime, and
     it should go straight to listening for a NEW command — say
     "what time is it" right after, with no need to repeat the wake
     phrase a third time.

  9) WAKE CHIME
     Just say "{wake_phrase}" on its own and listen — you should hear a
     short (well under a second) activation chime right as it starts
     listening. Judge it for yourself: pleasant/short/not a generic
     notification sound? Swap voice.wake_sound_path in config.yaml for
     your own WAV if you want a different one."""
    else:
        steps = f"""
  1) WAKE PHRASE
     Say "{wake_phrase}" and wait for the "wake_detected" state below.
     (No Ctrl+Alt+V needed for this one.)

  2) SIMPLE COMMAND
     Right after waking it, say something harmless: "what time is it".
     You should see [heard], then [FRIDAY] speak the answer.

  3) FOLLOW-UP COMMAND (legacy mode: voice.follow_up_enabled: true)
     Within {CFG.voice.follow_up_timeout_s:.0f}s of FRIDAY finishing that answer, say another
     command with NO wake word: "what's my battery level". It should
     still be heard and answered.

  4) SILENCE TIMEOUT
     After FRIDAY answers, say nothing. After {CFG.voice.follow_up_timeout_s:.0f}s you should see
     it quietly return to "idle" — no "I didn't catch that" nagging.

  5) INTERRUPTION
     Trigger a command with a longer spoken response (e.g. "what can you
     do"), then press Esc partway through FRIDAY speaking it. Speech
     should stop immediately.

  6) CONFIRMATION-REQUIRED COMMAND
     Wake it, then say: "{DEMO_PHRASE}"
     FRIDAY should ask "...should I go ahead?" — answer "yes" or "no" by
     voice (no wake word needed) and confirm it either runs the harmless
     demo or cancels, matching your answer. This is a stand-in for a real
     consequential command like sending a WhatsApp message — the gate
     behaves identically either way."""

    print(
        f"""
Configuration:
  wake phrase       : {CFG.voice.wakeword.model!r} (see PLAN.md Phase 8A/8O — not literally "FRIDAY")
  wake threshold    : {CFG.voice.wakeword.threshold}
  follow_up_enabled : {CFG.voice.follow_up_enabled} ({"STRICT: every command needs its own wake word" if strict else "LEGACY: a short no-wake-word follow-up window is open after each response"})
  fallback          : press {CFG.voice.activation_hotkey} anytime instead of the wake word (also strict-gated: no automatic follow-up either, unless follow_up_enabled is true)
  confirm listen    : {CFG.voice.confirm_listen_timeout_s}s to answer a "should I go ahead?" prompt

Test script — try each of these, in order:
{steps}

Ctrl+C to stop.
""",
    )

    HOTKEYS.start()
    try:
        loop.start()
        print(f"Wake-word listening started. {CFG.voice.activation_hotkey} also works as a fallback.\n")
    except Exception as exc:
        print(f"wake-word listening failed to start ({exc}); {CFG.voice.activation_hotkey} still works.\n")

    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("\nStopping...")
        loop.stop()
        HOTKEYS.stop()
        from friday.voice.capture import shutdown_mic_streams

        shutdown_mic_streams()
        return 0


if __name__ == "__main__":
    sys.exit(main())
