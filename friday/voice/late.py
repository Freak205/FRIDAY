"""Phase 42: speak the outcome of a voice request that finished AFTER `Backend.ask` stopped waiting for it.

Phase 41 publishes `backend.late_result` on the BUS when the ORIGINAL request ends (its own result, exception or
cancellation; nothing is re-run and `ask` is not called). The GUI shows it. This module adds the spoken half, and only
the part that can be done safely:

- only a request that came from the voice path (`actor == "voice"`) is ever spoken; typed requests stay on screen;
- the words are the outcome's own (`ok` is never promoted to success; a failure / error / cancellation is spoken as
  exactly that), put through the same credential redactor the planner's tool data uses, then the usual speech
  shortening. The request's own words are not read back aloud;
- delivery (`VoiceSession.announce`) is once per request id, never over a running cycle or other speech, and never
  interrupts anything -- if it cannot be spoken safely it is dropped, and the on-screen result is the record.

The BUS handler runs on the backend loop, which must not block on TTS, so the speaking happens on its own short thread.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from friday.bus import BUS, Event
from friday.log import get
from friday.voice.summarize import for_speech

log = get(__name__)

LATE_TOPIC = "backend.late_result"


def spoken_text(info: dict[str, Any], *, max_chars: int = 200) -> str:
    """What is said aloud for a late result. Empty when there is nothing truthful to say."""
    speech = " ".join(str(info.get("speech") or "").split())
    if not speech:
        return ""
    if info.get("error") or info.get("cancelled"):
        lead = ""  # the outcome's own words already say it ended with an error / was cancelled
    elif info.get("ok"):
        lead = "Your earlier request has finished. "
    else:
        lead = "Your earlier request finished, but it didn't succeed. "
    text = lead + speech
    try:
        from friday import toolview

        text = toolview.sanitize(text)
    except Exception:
        log.exception("late-result redaction failed; speaking nothing rather than unredacted text")
        return ""
    return for_speech(text, max_chars=max_chars)


def subscribe_late_announcements(announce: Callable[[dict[str, Any]], bool]) -> Callable[[Event], Any]:
    """Subscribe `announce(info) -> bool` to late results. It is called on a new daemon thread per event (it may block
    on TTS); an event that is not from the voice path is ignored here. Returns the handler (for `BUS.unsubscribe`)."""

    async def _on_late(event: Event) -> None:
        info = dict(event.data)
        if info.get("actor") != "voice" or not info.get("request_id"):
            return

        def _run() -> None:
            try:
                announce(info)
            except Exception:
                log.exception("late-result announcement failed")

        threading.Thread(target=_run, daemon=True, name="voice-late-result").start()

    BUS.subscribe(LATE_TOPIC, _on_late)
    return _on_late
