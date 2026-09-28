"""Local text-to-speech via pyttsx3 (Windows SAPI5 under the hood).

No cloud, no model download — SAPI voices ship with Windows. pyttsx3 already
depends only on comtypes/pywin32, both already FRIDAY dependencies.

Speaking runs pyttsx3's non-blocking loop (`startLoop(False)` + `iterate()`)
instead of the usual blocking `runAndWait()`, so `cancel_check` can be polled
between iterations — that's what lets Esc interrupt FRIDAY mid-sentence.

SAPI's COM object is apartment-threaded: it must be created and driven from
one single, consistent OS thread for its entire life, or `isBusy()` can stop
reflecting reality and `iterate()` spins forever. `speak()` is called from a
fresh thread every time (a new thread per Ctrl+Alt+V press in
friday.voice.conversation, plus asyncio's to_thread pool for the confirmation
prompt) while `_ensure_engine()` caches one engine instance for the process's
whole life — so without this, whichever thread happens to call speak() first
"owns" the engine and every later call from a different thread risks exactly
that hang. All real engine work therefore runs on one dedicated worker thread
(`_worker_loop`) that every `speak()` call — regardless of its own thread —
hands a request to and blocks on.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol



from friday.log import get
from friday.voice.types import TtsBackendError

log = get(__name__)


@dataclass
class _SpeakRequest:
    text: str
    cancel_check: Callable[[], bool] | None
    result: "queue.Queue[tuple[bool, bool | BaseException]]"


class EngineLike(Protocol):
    def say(self, text: str) -> None: ...
    def startLoop(self, useDriverLoop: bool) -> None: ...
    def endLoop(self) -> None: ...
    def iterate(self) -> None: ...
    def isBusy(self) -> bool: ...
    def stop(self) -> None: ...
    def setProperty(self, name: str, value: Any) -> None: ...


class TtsEngine:
    """Lazily initializes a pyttsx3 engine and speaks text, interruptibly."""

    def __init__(
        self,
        *,
        voice_id: str = "",
        rate: int | None = None,
        volume: float | None = None,
        engine: EngineLike | None = None,
    ) -> None:
        self.voice_id = voice_id
        self.rate = rate
        self.volume = volume
        self._engine: EngineLike | None = engine
        self._lock = threading.Lock()
        self._requests: "queue.Queue[_SpeakRequest]" = queue.Queue()
        self._worker: threading.Thread | None = None
        # Phase 10.X.3 (barge-in): set from any thread to interrupt whatever
        # _speak_now() is currently doing, without touching the worker thread
        # or the COM engine directly — see stop_current_speech().
        self._interrupt = threading.Event()

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._lock:
            if self._worker is None:
                worker = threading.Thread(
                    target=self._worker_loop, daemon=True, name="tts-worker"
                )
                worker.start()
                self._worker = worker

    def _worker_loop(self) -> None:
        """Owns the pyttsx3 engine for the rest of the process's life — every
        `speak()` call, from any thread, is serialized through here so the
        underlying SAPI COM object only ever sees one thread.
        """
        while True:
            req = self._requests.get()
            try:
                finished = self._speak_now(req.text, req.cancel_check)
                req.result.put((True, finished))
            except BaseException as exc:  # handed back to the caller's thread
                req.result.put((False, exc))

    def _ensure_engine(self) -> EngineLike:
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is None:
                try:
                    import pyttsx3

                    engine = pyttsx3.init()
                except Exception as exc:
                    log.exception("pyttsx3.init() failed")
                    raise TtsBackendError(f"couldn't start the TTS engine: {exc}") from exc
                if self.rate:
                    engine.setProperty("rate", self.rate)
                if self.volume is not None:
                    engine.setProperty("volume", self.volume)
                if self.voice_id:
                    engine.setProperty("voice", self.voice_id)
                try:
                    current_voice = engine.getProperty("voice")
                except Exception:
                    current_voice = "<unknown>"
                log.info(
                    "tts engine initialized on thread %s (voice=%s, rate=%s)",
                    threading.current_thread().name, current_voice, engine.getProperty("rate"),
                )
                self._engine = engine
            return self._engine

    def speak(self, text: str, *, cancel_check: Callable[[], bool] | None = None) -> bool:
        """Speak `text`. Returns True if it finished, False if cancelled.
        Raises TtsBackendError if the engine itself fails.

        Blocks the calling thread, but the actual pyttsx3/COM work always
        runs on the single dedicated worker thread (see module docstring) —
        never on whatever thread happened to call this.
        """
        text = (text or "").strip()
        if not text:
            return True

        self._ensure_worker()
        result: "queue.Queue[tuple[bool, bool | BaseException]]" = queue.Queue(maxsize=1)
        self._requests.put(_SpeakRequest(text, cancel_check, result))
        ok, payload = result.get()
        if not ok:
            assert isinstance(payload, BaseException)
            raise payload
        assert isinstance(payload, bool)
        return payload

    def _speak_now(self, text: str, cancel_check: Callable[[], bool] | None) -> bool:
        """Runs on `_worker_loop`'s thread only — see `speak()`."""
        engine = self._ensure_engine()
        # Clear any interrupt that arrived before this request even started
        # (e.g. a stray/late stop_current_speech() from the previous
        # utterance) so it can't immediately cancel a brand-new one.
        self._interrupt.clear()
        log.info("TTS STARTED on thread %s: %r", threading.current_thread().name, text)
        try:
            engine.say(text)
            engine.startLoop(False)
            cancelled = False
            try:
                while engine.isBusy():
                    if (cancel_check is not None and cancel_check()) or self._interrupt.is_set():
                        cancelled = True
                        engine.stop()
                        break
                    engine.iterate()
            finally:
                engine.endLoop()
                self._interrupt.clear()
            log.info("tts speak() finished (cancelled=%s)", cancelled)
            return not cancelled
        except Exception as exc:
            log.exception("tts speak() raised")
            raise TtsBackendError(f"speech failed: {exc}") from exc

    def stop_current_speech(self) -> None:
        """Interrupt whatever `speak()` call is currently in progress, from
        any thread (Phase 10.X.3 barge-in). Returns immediately — it only
        sets a flag that `_speak_now`'s loop notices on its next `iterate()`,
        the same way an Esc-driven `cancel_check` already works. A no-op if
        nothing is currently speaking; never raises, never touches the COM
        engine directly (that stays on the dedicated worker thread), and
        never kills or replaces the worker — the next `speak()` call works
        normally afterward.
        """
        self._interrupt.set()

    def stop(self) -> None:
        self.stop_current_speech()
        if self._engine is not None:
            try:
                self._engine.stop()
            except Exception:
                log.exception("tts stop() failed")
