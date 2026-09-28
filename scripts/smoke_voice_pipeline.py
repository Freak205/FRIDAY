"""Deterministic voice-pipeline tests — no microphone, no real STT/TTS engine,
no Ollama. Everything that touches hardware or a model is replaced with an
injected fake (FakeWhisperModel, FakeTtsEngine, scripted capture results),
exactly the dependency-injection pattern smoke_plan.py/smoke_orchestrator.py
already use for the LLM. The one thing this deliberately does NOT fake is the
brain/executor/audit path — voice.SESSION.handle integration, confirmation
preservation, and audit/actor propagation are verified against the real
REGISTRY/BRAIN/EXECUTOR, because the whole point of this phase is that voice
adds no second brain and no separate permission system.

For a real microphone + real speakers, see scripts/smoke_voice.py instead.
"""

import asyncio
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import store  # noqa: E402
from friday.config import CFG  # noqa: E402
from friday.voice.capture import BLOCK_S, VadSession, drive  # noqa: E402
from friday.voice.normalize import normalize_transcript  # noqa: E402
from friday.voice.session import CaptureTuning, VoiceSession  # noqa: E402
from friday.voice.sound import WakeSoundPlayer  # noqa: E402
from friday.voice.stt import SttEngine  # noqa: E402
from friday.voice.summarize import for_speech  # noqa: E402
from friday.voice.tts import TtsEngine  # noqa: E402
from friday.voice.types import CaptureResult, TranscriptionResult, TtsBackendError  # noqa: E402
from friday.voice.vocabulary import DEFAULT_VOCABULARY, build_hotwords  # noqa: E402

SR = 16000
BLOCK = 480  # 30ms @ 16kHz


def speech_block(level: float = 0.3) -> np.ndarray:
    return (np.ones(BLOCK, dtype=np.float32) * level)


def silence_block() -> np.ndarray:
    return np.zeros(BLOCK, dtype=np.float32)


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeWhisperModel:
    def __init__(self, text: str = "", *, raises: bool = False) -> None:
        self.text = text
        self.raises = raises
        self.calls: list[np.ndarray] = []
        self.kwargs_seen: list[dict] = []

    def transcribe(self, audio: np.ndarray, **kwargs):
        self.calls.append(audio)
        self.kwargs_seen.append(kwargs)
        if self.raises:
            raise RuntimeError("fake backend exploded")
        return [FakeSegment(self.text)], object()


class FakeTtsBackend:
    def __init__(self, *, iterations_before_done: int = 2, fail_on_say: bool = False) -> None:
        self.fail_on_say = fail_on_say
        self.said: list[str] = []
        self.stopped = False
        self._iterations_before_done = iterations_before_done
        self._remaining = 0

    def say(self, text: str) -> None:
        if self.fail_on_say:
            raise RuntimeError("fake driver refused to speak")
        self.said.append(text)
        self._remaining = self._iterations_before_done

    def startLoop(self, _useDriverLoop: bool) -> None:
        pass

    def endLoop(self) -> None:
        pass

    def iterate(self) -> None:
        self._remaining -= 1

    def isBusy(self) -> bool:
        return self._remaining > 0

    def stop(self) -> None:
        self.stopped = True
        self._remaining = 0

    def setProperty(self, name: str, value) -> None:
        pass


class SlowFakeTtsBackend(FakeTtsBackend):
    """Like FakeTtsBackend, but iterate() takes a small real amount of time
    so a test calling stop_current_speech() from another thread has a real
    window to land mid-speech instead of racing a near-instant tight loop
    (plain FakeTtsBackend's iterate() is a no-op decrement — it can finish
    hundreds of "iterations" before another thread's first time.sleep()
    poll even wakes up).
    """

    def iterate(self) -> None:
        time.sleep(0.01)
        super().iterate()


def main() -> bool:
    overall = True

    # == 1. voice configuration =============================================
    print("\n--- voice configuration ---\n")
    v = CFG.voice
    checks = [
        ("voice enabled by default", v.enabled is True),
        ("activation hotkey set", v.activation_hotkey == "ctrl+alt+v"),
        ("stt model configured", bool(v.stt.model)),
        ("stt device defaults to cpu (verified working on this machine)", v.stt.device == "cpu"),
        ("silence timeout positive", v.stt.silence_timeout_s > 0),
        ("max recording bounded", 0 < v.stt.max_recording_s <= 60),
        ("min speech duration configured", v.stt.min_speech_s >= 0),
        ("pre-roll configured", v.stt.pre_roll_ms >= 0),
        ("beam size at least 1", v.stt.beam_size >= 1),
        ("vocabulary is a list (empty = off by default, see benchmark_stt.py)", isinstance(v.stt.vocabulary, list)),
        ("tts enabled by default", v.tts.enabled is True),
        ("recordings not saved by default (privacy)", v.save_recordings is False),
    ]
    for label, ok in checks:
        print(f"  {'OK  ' if ok else 'MISS'} {label}")
        overall &= ok

    # == 2. VAD: cancellation ===============================================
    print("\n--- cancellation (Esc during recording) ---\n")
    result = drive(
        [speech_block(), speech_block(), speech_block()],
        silence_timeout_s=1.0, max_duration_s=15.0,
        cancel_check=lambda: True,
    )
    ok = result.outcome == "cancelled" and not result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} cancel flag set on the first block -> outcome={result.outcome!r}")
    overall &= ok

    # == 3. VAD: empty / no speech at all ===================================
    print("\n--- empty transcription input (pure silence) ---\n")
    blocks = [silence_block() for _ in range(40)]  # 40*30ms = 1.2s < max_duration but no speech
    result = drive(blocks, silence_timeout_s=1.0, max_duration_s=1.0)
    ok = result.outcome == "no_speech" and not result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} silence-only input -> outcome={result.outcome!r}")
    overall &= ok

    # == 4. VAD: silence-terminated utterance (the normal case) =============
    print("\n--- normal utterance: speech then silence ends it ---\n")
    blocks = [speech_block() for _ in range(10)] + [silence_block() for _ in range(40)]
    result = drive(blocks, silence_timeout_s=1.0, max_duration_s=15.0)
    ok = result.outcome == "ok" and result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} speech+silence -> outcome={result.outcome!r}, "
          f"{result.audio.size if result.audio is not None else 0} samples captured")
    overall &= ok

    # == 5. VAD: max recording duration (timeout handling) ==================
    print("\n--- timeout handling: continuous speech hits max_recording_s ---\n")
    session = VadSession(silence_timeout_s=1.0, max_duration_s=0.3)  # 0.3s cap
    final = None
    for _ in range(100):  # far more than enough to exceed 0.3s of 30ms blocks
        final = session.feed(speech_block())
        if final is not None:
            break
    ok = final is not None and final.outcome == "max_duration" and final.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} uninterrupted speech past the cap -> outcome={final.outcome if final else None!r}")
    overall &= ok

    # == 5b. VAD: pre-roll splice (Phase 7P — fixes clipped first word) ======
    print("\n--- pre-roll: audio just before speech-detected is kept, not discarded ---\n")
    blocks = [silence_block() for _ in range(5)] + [speech_block() for _ in range(10)] + [silence_block() for _ in range(40)]
    events: list[str] = []
    result = drive(
        blocks, silence_timeout_s=1.0, max_duration_s=15.0,
        pre_roll_blocks=3, on_event=lambda name: events.append(name),
    )
    trailing = math.ceil(1.0 / BLOCK_S)  # silence blocks needed to reach the 1.0s timeout
    expected_blocks = 3 + 10 + trailing  # pre-roll + speech + trailing silence to timeout
    got_blocks = (result.audio.size // BLOCK) if result.has_audio else 0
    ok = (
        result.outcome == "ok" and got_blocks == expected_blocks
        and events == ["first_audio", "speech_start", "speech_end"]
    )
    print(f"  {'OK  ' if ok else 'MISS'} 5 leading silence blocks, pre_roll_blocks=3 -> "
          f"captured {got_blocks} blocks (expected {expected_blocks}), events={events}")
    overall &= ok

    print("\n--- pre-roll: disabled (pre_roll_blocks=0) behaves exactly like before ---\n")
    result = drive(blocks, silence_timeout_s=1.0, max_duration_s=15.0, pre_roll_blocks=0)
    expected_blocks = 10 + trailing
    got_blocks = (result.audio.size // BLOCK) if result.has_audio else 0
    ok = result.outcome == "ok" and got_blocks == expected_blocks
    print(f"  {'OK  ' if ok else 'MISS'} pre_roll_blocks=0 -> captured {got_blocks} blocks (expected {expected_blocks}, no leading silence)")
    overall &= ok

    # == 5c. VAD: min_speech_s rejects brief noise blips =====================
    print("\n--- min_speech_s: a single-block blip is discarded as noise, not sent to STT ---\n")
    blip_blocks = [speech_block()] + [silence_block() for _ in range(40)]
    result = drive(blip_blocks, silence_timeout_s=1.0, max_duration_s=15.0, min_speech_s=0.25)
    ok = result.outcome == "no_speech" and not result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} one 30ms speech block with min_speech_s=0.25s -> outcome={result.outcome!r}")
    overall &= ok

    print("\n--- min_speech_s: a real (longer) utterance still passes ---\n")
    real_blocks = [speech_block() for _ in range(10)] + [silence_block() for _ in range(40)]
    result = drive(real_blocks, silence_timeout_s=1.0, max_duration_s=15.0, min_speech_s=0.25)
    ok = result.outcome == "ok" and result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} 300ms of speech with min_speech_s=0.25s -> outcome={result.outcome!r}")
    overall &= ok

    # == 5c2. VAD: speech_prob_fn (Phase 10.X.4) overrides RMS when supplied ===
    print("\n--- speech_prob_fn: a fake speech classifier drives is_speech instead of RMS ---\n")
    # All-silence RMS (silence_block()) but the fake classifier calls the
    # middle stretch "speech" -- proves the decision comes from the injected
    # function, not from block amplitude, exactly the way record_utterance()
    # wires in the real SileroVad model for real microphone audio.
    fake_probs = [0.0] * 5 + [0.9] * 10 + [0.0] * 40
    calls = iter(fake_probs)
    result = drive(
        [silence_block() for _ in fake_probs],
        silence_timeout_s=1.0, max_duration_s=15.0,
        speech_prob_fn=lambda _block: next(calls), vad_threshold=0.5,
    )
    ok = result.outcome == "ok" and result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} RMS-silent blocks but fake classifier says speech -> outcome={result.outcome!r}")
    overall &= ok

    print("\n--- speech_prob_fn: without it, the same fake-classifier-shaped RMS silence is no_speech ---\n")
    result = drive(
        [silence_block() for _ in fake_probs], silence_timeout_s=1.0, max_duration_s=15.0,
    )
    ok = result.outcome == "no_speech" and not result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} no speech_prob_fn -> falls back to RMS -> outcome={result.outcome!r}")
    overall &= ok

    # == 5c3. VAD: vad_sustain_threshold hysteresis (Phase 10.X.4 regression) ==
    # Real bug found replaying actual mic recordings (see
    # scripts/replay_vad_samples.py): a short command's probability trace
    # dips mid-word below a single shared threshold, undercounting speech
    # blocks enough to fail min_speech_s and wrongly reject a real command as
    # noise. Script that exact shape: 8 blocks >= 0.5, interleaved with 3
    # blocks that land between 0.35 and 0.5 (real speech, just a mid-word
    # dip) -- 240ms of clearly-"speech" blocks alone is under a 0.25s
    # min_speech_s, but the dip blocks push total speech time comfortably
    # over it once sustain_threshold counts them too.
    dip_shape = [0.72, 0.60, 0.57, 0.40, 0.47, 0.53, 0.77, 0.63, 0.61, 0.54, 0.45]
    print("\n--- vad_sustain_threshold: no hysteresis -- mid-word dips undercount speech, false-reject ---\n")
    result = drive(
        [silence_block() for _ in dip_shape] + [silence_block() for _ in range(40)],
        silence_timeout_s=1.0, max_duration_s=15.0, min_speech_s=0.25,
        speech_prob_fn=lambda _block, c=iter(dip_shape + [0.0] * 40): next(c),
        vad_threshold=0.5, vad_sustain_threshold=0.5,  # no hysteresis
    )
    ok = result.outcome == "no_speech"
    print(f"  {'OK  ' if ok else 'MISS'} sustain==threshold (no hysteresis) -> real command wrongly dropped -> outcome={result.outcome!r}")
    overall &= ok

    print("\n--- vad_sustain_threshold: with hysteresis, the same dips still count as speech ---\n")
    result = drive(
        [silence_block() for _ in dip_shape] + [silence_block() for _ in range(40)],
        silence_timeout_s=1.0, max_duration_s=15.0, min_speech_s=0.25,
        speech_prob_fn=lambda _block, c=iter(dip_shape + [0.0] * 40): next(c),
        vad_threshold=0.5, vad_sustain_threshold=0.35,  # matches config default
    )
    ok = result.outcome == "ok" and result.has_audio
    print(f"  {'OK  ' if ok else 'MISS'} sustain=0.35 < threshold=0.5 -> mid-word dips still count -> outcome={result.outcome!r}")
    overall &= ok

    # == 5c4. SileroVad: the bundled model loads and scores a real block =====
    print("\n--- SileroVad: bundled model loads and predict() returns a 0-1 probability ---\n")
    try:
        from friday.voice.vad_model import SileroVad

        vad_model = SileroVad()
        prob_silence = vad_model.predict(silence_block())
        vad_model.reset()
        prob_loud = vad_model.predict(speech_block(level=0.9))
        model_ok = 0.0 <= prob_silence <= 1.0 and 0.0 <= prob_loud <= 1.0
        print(f"  {'OK  ' if model_ok else 'MISS'} predict(silence)={prob_silence:.3f} predict(loud constant)={prob_loud:.3f}")
        overall &= model_ok
    except Exception as exc:  # noqa: BLE001
        # Best-effort like production: missing onnxruntime/model must not be
        # treated as a hard smoke-test failure, only reported.
        print(f"  SKIP  SileroVad unavailable in this environment: {exc}")

    # == 5d. MicStream: session queues are isolated and independently closable ==
    print("\n--- MicStream: callback fan-out only reaches open sessions ---\n")
    from friday.voice.capture import MicStream

    mic = MicStream(device=None)
    q1 = mic.open_session()
    q2 = mic.open_session()
    mic._callback(speech_block().reshape(-1, 1), BLOCK, None, None)
    mic.close_session(q1)
    mic._callback(silence_block().reshape(-1, 1), BLOCK, None, None)
    ok = q1.qsize() == 1 and q2.qsize() == 2
    print(f"  {'OK  ' if ok else 'MISS'} q1 (closed after 1st block) got {q1.qsize()} block(s), "
          f"q2 (stayed open) got {q2.qsize()} block(s)")
    overall &= ok

    # == 5d2. MicStream: pre-roll ring buffer (Phase 10.X.7) ==================
    # A brand-new session opened with `preroll_since` must be seeded with
    # exactly the ring-buffered blocks captured at/after that timestamp —
    # nothing older (would leak audio from before the cutoff, e.g. the wake
    # phrase itself) and nothing missing (would reproduce the exact
    # "open VS Code" clipping bug this phase exists to fix) — with no gap or
    # duplication across the open_session() boundary itself.
    print("\n--- MicStream: pre-roll seeds a new session from a timestamp cutoff ---\n")
    mic2 = MicStream(device=None)
    old_block = np.full(BLOCK, 0.11, dtype=np.float32)
    new_block_1 = np.full(BLOCK, 0.22, dtype=np.float32)
    new_block_2 = np.full(BLOCK, 0.33, dtype=np.float32)
    live_block = np.full(BLOCK, 0.44, dtype=np.float32)

    mic2._callback(old_block.reshape(-1, 1), BLOCK, None, None)
    cutoff = time.perf_counter()
    mic2._callback(new_block_1.reshape(-1, 1), BLOCK, None, None)
    mic2._callback(new_block_2.reshape(-1, 1), BLOCK, None, None)
    q3 = mic2.open_session(preroll_since=cutoff)
    mic2._callback(live_block.reshape(-1, 1), BLOCK, None, None)

    seeded = [q3.get_nowait() for _ in range(q3.qsize())]
    ok = (
        len(seeded) == 3
        and np.allclose(seeded[0], new_block_1)
        and np.allclose(seeded[1], new_block_2)
        and np.allclose(seeded[2], live_block)
    )
    print(
        f"  {'OK  ' if ok else 'MISS'} session seeded with {len(seeded)} block(s) at/after cutoff "
        "(expected: new_block_1, new_block_2, live_block — old_block excluded)"
    )
    overall &= ok

    print("\n--- MicStream: open_session() with no preroll_since is unseeded (unchanged default) ---\n")
    q4 = mic2.open_session()
    ok = q4.qsize() == 0
    print(f"  {'OK  ' if ok else 'MISS'} unseeded session starts empty (qsize={q4.qsize()})")
    overall &= ok

    # == 5e. VAD: warmup_ignore_s (Phase 10.X.3 — wake-sound chime immunity) ==
    print("\n--- warmup_ignore_s: a loud blip in the warmup window is never mistaken for speech-start ---\n")
    warmup_s = 0.3  # matches ~10 blocks of 30ms
    chime_then_speech = (
        [speech_block(level=0.9) for _ in range(10)]  # "chime" energy during warmup
        + [silence_block() for _ in range(2)]           # brief real gap
        + [speech_block() for _ in range(10)]            # the actual command
        + [silence_block() for _ in range(40)]
    )
    events: list[str] = []
    result = drive(
        chime_then_speech, silence_timeout_s=1.0, max_duration_s=15.0,
        warmup_ignore_s=warmup_s, pre_roll_blocks=3, on_event=lambda name: events.append(name),
    )
    # speech_start must fire only once real (post-warmup) speech begins, not
    # during the "chime" blocks — and the captured audio must still include
    # the real command in full (nothing clipped by treating the chime as the
    # whole utterance).
    ok = (
        result.outcome == "ok" and result.has_audio
        and events[:1] == ["first_audio"]
        and "speech_start" in events
        and events.index("speech_start") > 0
    )
    got_samples = result.audio.size if result.has_audio else 0
    # 3 pre-roll blocks (from the tail of the ignored/silent-treated warmup
    # window) + 10 real speech blocks + trailing silence to the 1.0s timeout.
    trailing = math.ceil(1.0 / BLOCK_S)
    expected_samples = (3 + 10 + trailing) * BLOCK
    ok = ok and got_samples == expected_samples
    print(f"  {'OK  ' if ok else 'MISS'} chime (10 loud blocks) + gap + real speech -> "
          f"outcome={result.outcome!r}, events={events}, {got_samples} samples (expected {expected_samples})")
    overall &= ok

    print("\n--- warmup_ignore_s=0.0 (default): unaffected, identical to pre-Phase-10.X.3 behavior ---\n")
    result = drive(chime_then_speech, silence_timeout_s=1.0, max_duration_s=15.0, warmup_ignore_s=0.0)
    ok = result.outcome == "ok" and result.has_audio and result.audio.size > 0
    # With no warmup suppression, the loud "chime" blocks themselves are
    # speech-start — outcome is still "ok" but the utterance boundary is
    # different (starts at the chime, not the real command).
    print(f"  {'OK  ' if ok else 'MISS'} warmup_ignore_s=0.0 -> outcome={result.outcome!r} (no suppression applied)")
    overall &= ok

    # == 6. STT adapter: success =============================================
    print("\n--- STT adapter: transcribes via an injected fake model ---\n")
    fake_model = FakeWhisperModel(text="open chrome")
    engine = SttEngine(model=fake_model, device="cpu")
    tr = engine.transcribe(np.zeros(SR, dtype=np.float32))
    # "chrome" -> "Chrome": Phase 10.X.3's post-STT normalization (see
    # friday/voice/normalize.py) runs on every transcription now.
    ok = tr.ok and tr.text == "open Chrome" and len(fake_model.calls) == 1
    print(f"  {'OK  ' if ok else 'MISS'} fake model -> TranscriptionResult(ok={tr.ok}, text={tr.text!r})")
    overall &= ok

    # == 6b. STT adapter: vocabulary is passed through as `hotwords` (Phase 10.X.5) ==
    print("\n--- STT adapter: vocabulary reaches the backend as faster-whisper's `hotwords` ---\n")
    vocab_model = FakeWhisperModel(text="open vs code")
    vocab_engine = SttEngine(model=vocab_model, device="cpu", vocabulary=["FRIDAY", "VS Code"])
    vocab_engine.transcribe(np.zeros(SR, dtype=np.float32))
    ok = vocab_model.kwargs_seen[-1].get("hotwords") == "FRIDAY, VS Code"
    print(f"  {'OK  ' if ok else 'MISS'} vocabulary=['FRIDAY', 'VS Code'] -> "
          f"hotwords={vocab_model.kwargs_seen[-1].get('hotwords')!r}")
    overall &= ok

    print("\n--- STT adapter: no vocabulary configured -> hotwords stays None (no-op) ---\n")
    no_vocab_model = FakeWhisperModel(text="open chrome")
    no_vocab_engine = SttEngine(model=no_vocab_model, device="cpu")
    no_vocab_engine.transcribe(np.zeros(SR, dtype=np.float32))
    ok = no_vocab_model.kwargs_seen[-1].get("hotwords") is None
    print(f"  {'OK  ' if ok else 'MISS'} vocabulary unset -> "
          f"hotwords={no_vocab_model.kwargs_seen[-1].get('hotwords')!r}")
    overall &= ok

    # == 7. STT adapter: backend error comes back typed, not raised =========
    print("\n--- STT error: a broken backend fails cleanly ---\n")
    broken = SttEngine(model=FakeWhisperModel(raises=True), device="cpu")
    tr = broken.transcribe(np.zeros(SR, dtype=np.float32))
    ok = (not tr.ok) and "exploded" in (tr.error or "")
    print(f"  {'OK  ' if ok else 'MISS'} backend exception -> TranscriptionResult(ok=False, error={tr.error!r})")
    overall &= ok

    # == 8. TTS adapter: success ==============================================
    print("\n--- TTS adapter: speaks via an injected fake engine ---\n")
    fake_tts_backend = FakeTtsBackend(iterations_before_done=3)
    tts = TtsEngine(engine=fake_tts_backend)
    finished = tts.speak("hello there")
    ok = finished and fake_tts_backend.said == ["hello there"] and not fake_tts_backend.stopped
    print(f"  {'OK  ' if ok else 'MISS'} speak() completed={finished}, said={fake_tts_backend.said}")
    overall &= ok

    # == 9. TTS adapter: interruption ========================================
    print("\n--- TTS cancellation: cancel_check stops speech early ---\n")
    fake_tts_backend2 = FakeTtsBackend(iterations_before_done=1000)  # would run "forever"
    tts2 = TtsEngine(engine=fake_tts_backend2)
    finished2 = tts2.speak("a very long sentence", cancel_check=lambda: True)
    ok = (not finished2) and fake_tts_backend2.stopped
    print(f"  {'OK  ' if ok else 'MISS'} speak() completed={finished2} (expected False), stopped={fake_tts_backend2.stopped}")
    overall &= ok

    # == 10. TTS error ========================================================
    print("\n--- TTS error: a broken engine raises TtsBackendError, not a crash ---\n")
    broken_tts = TtsEngine(engine=FakeTtsBackend(fail_on_say=True))
    raised = False
    try:
        broken_tts.speak("anything")
    except TtsBackendError:
        raised = True
    print(f"  {'OK  ' if raised else 'MISS'} broken engine -> TtsBackendError raised")
    overall &= raised

    # == 10b. TTS regression: speak() from many threads never hangs ==========
    # Real desktop bug (see PLAN.md/incident notes): friday.voice.conversation
    # spawns a brand-new OS thread per Ctrl+Alt+V press, but TtsEngine caches
    # one pyttsx3/SAPI COM engine for the process's life. SAPI's COM object is
    # apartment-threaded — calling it from a different thread than the one
    # that created it can make isBusy() never return False, hanging speak()
    # forever and, since VoiceSession releases its busy-lock in a `finally`
    # after speak() returns, wedging every future Ctrl+Alt+V press behind a
    # cycle that never finishes. TtsEngine now serializes all real work onto
    # one dedicated worker thread (see friday/voice/tts.py) specifically so
    # this can't happen; this proves every speak() call, regardless of which
    # thread invoked it, actually lands on that same single thread.
    print("\n--- TTS regression: speak() from N different threads stays on one worker thread ---\n")
    thread_backend = FakeTtsBackend()
    threaded_tts = TtsEngine(engine=thread_backend)
    say_thread_idents: list[int] = []
    orig_say = thread_backend.say

    def _tracking_say(text: str) -> None:
        say_thread_idents.append(threading.get_ident())
        orig_say(text)

    thread_backend.say = _tracking_say  # type: ignore[method-assign]

    results: list[bool] = []
    errors: list[BaseException] = []

    def _press(n: int) -> None:
        try:
            results.append(threaded_tts.speak(f"press {n}"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    presses = [threading.Thread(target=_press, args=(i,)) for i in range(5)]
    for t in presses:
        t.start()
        t.join(timeout=5)

    ok = (
        not errors
        and len(results) == 5
        and all(results)
        and len(set(say_thread_idents)) == 1  # every say() landed on the same OS thread
        and threading.get_ident() not in say_thread_idents  # never the caller's own thread
    )
    print(
        f"  {'OK  ' if ok else 'MISS'} 5 presses from 5 different threads -> "
        f"say() always ran on 1 thread (distinct idents seen: {len(set(say_thread_idents))}), "
        f"no hang, no error"
    )
    overall &= ok

    # == 10c. TTS: stop_current_speech() interrupts from ANY thread (Phase 10.X.3 barge-in) ==
    print("\n--- TTS barge-in: stop_current_speech() from another thread interrupts speak() ---\n")
    bargein_backend = SlowFakeTtsBackend(iterations_before_done=30)  # ~0.3s of "speaking" until stopped
    bargein_tts = TtsEngine(engine=bargein_backend)
    speak_done = threading.Event()
    speak_result: list[bool] = []

    def _speak_long() -> None:
        speak_result.append(bargein_tts.speak("I've completed the operation and--"))
        speak_done.set()

    threading.Thread(target=_speak_long, daemon=True).start()
    # Give _speak_now a moment to actually start iterating (worker thread
    # start + first say()), same margin the multi-thread test above uses.
    started = False
    for _ in range(50):
        if bargein_backend.said:
            started = True
            break
        time.sleep(0.02)
    bargein_tts.stop_current_speech()  # simulates the barge-in watcher's call
    finished_in_time = speak_done.wait(timeout=3.0)
    ok = started and finished_in_time and speak_result == [False] and bargein_backend.stopped
    print(f"  {'OK  ' if ok else 'MISS'} speak() returned False (interrupted) after stop_current_speech() "
          f"from a different thread, engine.stop() called={bargein_backend.stopped}")
    overall &= ok

    print("\n--- TTS barge-in: the worker survives and speaks normally afterward ---\n")
    normal_result = bargein_tts.speak("Chrome is open.")
    ok = normal_result is True and bargein_backend.said[-1] == "Chrome is open."
    print(f"  {'OK  ' if ok else 'MISS'} a fresh speak() after an interruption completes normally "
          f"(said={bargein_backend.said})")
    overall &= ok

    print("\n--- TTS barge-in: stop_current_speech() with nothing speaking is a harmless no-op ---\n")
    idle_backend = FakeTtsBackend()
    idle_tts = TtsEngine(engine=idle_backend)
    idle_tts.stop_current_speech()  # nothing in progress
    idle_result = idle_tts.speak("hello")
    ok = idle_result is True
    print(f"  {'OK  ' if ok else 'MISS'} stop_current_speech() before any speak() doesn't poison the next one "
          f"(result={idle_result})")
    overall &= ok

    # == 10d. VoiceSession: stop_speech_fn exposure + CaptureTuning consume-once (Phase 10.X.3) ==
    print("\n--- VoiceSession.stop_speech_fn: exposed for ConversationLoop's barge-in watcher ---\n")
    stop_calls: list[int] = []
    vs_stop = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
        stop_speech=lambda: stop_calls.append(1),
    )
    vs_stop.stop_speech_fn()
    ok = stop_calls == [1]
    print(f"  {'OK  ' if ok else 'MISS'} stop_speech_fn() invokes the injected callable")
    overall &= ok

    print("\n--- CaptureTuning: set_pending_warmup is consume-once, no-op without a tuning object ---\n")
    tuning = CaptureTuning()
    seen_warmups: list[float] = []

    def capture_with_tuning(max_duration_s: float | None = None) -> CaptureResult:
        seen_warmups.append(tuning.warmup_ignore_s)
        tuning.warmup_ignore_s = 0.0  # mirrors build_voice_session's consume-once read
        return CaptureResult(outcome="no_speech")

    vs_tuning = VoiceSession(
        capture=capture_with_tuning,
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
        capture_tuning=tuning,
    )
    vs_tuning.set_pending_warmup(0.85)
    vs_tuning.run_cycle()
    vs_tuning.run_cycle()  # second call should see 0.0 (already consumed)
    ok = seen_warmups == [0.85, 0.0]
    print(f"  {'OK  ' if ok else 'MISS'} warmup applied to exactly the next capture() call: {seen_warmups}")
    overall &= ok

    vs_no_tuning = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="no_speech"),
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
    )
    vs_no_tuning.set_pending_warmup(1.0)  # no capture_tuning wired -- must not raise
    ok = True
    print("  OK   set_pending_warmup() without a CaptureTuning wired is a harmless no-op")
    overall &= ok

    print("\n--- CaptureTuning: set_pending_preroll is consume-once (Phase 10.X.7) ---\n")
    preroll_tuning = CaptureTuning()
    seen_prerolls: list[float | None] = []

    def capture_with_preroll(max_duration_s: float | None = None) -> CaptureResult:
        seen_prerolls.append(preroll_tuning.preroll_since)
        preroll_tuning.preroll_since = None  # mirrors build_voice_session's consume-once read
        return CaptureResult(outcome="no_speech")

    vs_preroll = VoiceSession(
        capture=capture_with_preroll,
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
        capture_tuning=preroll_tuning,
    )
    marker = time.perf_counter()
    vs_preroll.set_pending_preroll(marker)
    vs_preroll.run_cycle()
    vs_preroll.run_cycle()  # second call should see None (already consumed)
    ok = seen_prerolls == [marker, None]
    print(f"  {'OK  ' if ok else 'MISS'} preroll timestamp applied to exactly the next capture() call: {seen_prerolls}")
    overall &= ok

    vs_no_tuning.set_pending_preroll(marker)  # no capture_tuning wired -- must not raise
    print("  OK   set_pending_preroll() without a CaptureTuning wired is a harmless no-op")

    # == 10e. WakeSoundPlayer: loads the bundled asset, plays without raising ==
    print("\n--- WakeSoundPlayer: bundled asset loads and reports a sub-1s duration ---\n")
    player = WakeSoundPlayer(enabled=True)
    duration = player.duration_s
    ok = 0.0 < duration <= 1.5
    print(f"  {'OK  ' if ok else 'MISS'} friday/assets/audio/wake_chime.wav duration={duration:.3f}s")
    overall &= ok

    print("\n--- WakeSoundPlayer: disabled or missing asset never raises ---\n")
    disabled_player = WakeSoundPlayer(enabled=False)
    missing_player = WakeSoundPlayer(enabled=True, path="data/does_not_exist_wake_chime.wav")
    raised = False
    try:
        disabled_player.play()
        missing_player.play()
        _ = missing_player.duration_s
    except Exception:
        raised = True
    ok = not raised and missing_player.duration_s == 0.0
    print(f"  {'OK  ' if ok else 'MISS'} disabled/missing-asset playback never raises (duration falls back to 0.0)")
    overall &= ok

    # == 10f. normalize_transcript: cosmetic aliasing only, never changes unmatched text ==
    print("\n--- normalize_transcript: known product-name aliases are re-cased ---\n")
    cases = [
        ("open vs code", "open VS Code"),
        ("open visual studio code please", "open Visual Studio Code please"),
        ("open vscode", "open VS Code"),
        ("check whats app", "check WhatsApp"),
        ("open chrome and github", "open Chrome and GitHub"),
        ("open chat gpt", "open ChatGPT"),  # Phase 10.X.5 addition
        ("open chatgpt", "open ChatGPT"),
        ("hey jarvis open notepad", "hey jarvis open notepad"),  # no alias -- unchanged
        ("", ""),
    ]
    all_ok = True
    for raw, expected in cases:
        got = normalize_transcript(raw)
        row_ok = got == expected
        all_ok &= row_ok
        print(f"  {'OK  ' if row_ok else 'MISS'} {raw!r} -> {got!r} (expected {expected!r})")
    overall &= all_ok

    # == 10g. build_hotwords: dedupes case-insensitively, drops blanks, "" for empty (Phase 10.X.5) ==
    print("\n--- build_hotwords: joins a vocabulary list into faster-whisper's `hotwords` string ---\n")
    hotword_cases = [
        (["FRIDAY", "VS Code", "WhatsApp"], "FRIDAY, VS Code, WhatsApp"),
        ([], ""),
        (None, ""),
        (["  Chrome  ", "", "  "], "Chrome"),  # blank/whitespace-only entries dropped
        (["Chrome", "chrome", "CHROME"], "Chrome"),  # case-insensitive dedupe, first casing wins
        (list(DEFAULT_VOCABULARY), ", ".join(DEFAULT_VOCABULARY)),  # no accidental dedupe/reorder
    ]
    all_ok = True
    for raw, expected in hotword_cases:
        got = build_hotwords(raw)
        row_ok = got == expected
        all_ok &= row_ok
        print(f"  {'OK  ' if row_ok else 'MISS'} {raw!r} -> {got!r} (expected {expected!r})")
    overall &= all_ok

    # == 11. summarize: deterministic truncation, no LLM ======================
    print("\n--- speech truncation is deterministic (no LLM involved) ---\n")
    long_text = "This is sentence one. " * 40
    short = for_speech(long_text, max_chars=60)
    ok = len(short) <= 61 and short.strip() != "" and "LLM" not in short
    print(f"  {'OK  ' if ok else 'MISS'} {len(long_text)} chars -> {len(short)} chars: {short!r}")
    overall &= ok

    # == 12. VoiceSession: cancelled / empty / STT-error never reach handle_text ==
    print("\n--- VoiceSession: cancelled, empty, and STT-error never call handle_text ---\n")
    for label, capture_result, transcribe_fn in [
        ("cancelled", CaptureResult(outcome="cancelled"), None),
        ("no_speech", CaptureResult(outcome="no_speech"), None),
        ("silence-terminated but empty text",
         CaptureResult(outcome="ok", audio=np.zeros(1600, dtype=np.float32)),
         lambda audio: TranscriptionResult(text="", ok=True)),
        ("stt backend error",
         CaptureResult(outcome="ok", audio=np.zeros(1600, dtype=np.float32)),
         lambda audio: TranscriptionResult(text="", ok=False, error="boom")),
    ]:
        handled: list[str] = []
        spoken: list[str] = []
        vs = VoiceSession(
            capture=lambda max_duration_s=None, cr=capture_result: cr,
            transcribe=transcribe_fn or (lambda audio: TranscriptionResult(text="", ok=True)),
            handle_text=lambda text: handled.append(text) or None,
            speak=lambda text, **_: spoken.append(text) or True,
        )
        vs.run_cycle()
        ok = len(handled) == 0
        print(f"  {'OK  ' if ok else 'MISS'} [{label}] handle_text called {len(handled)} time(s) (expected 0)")
        overall &= ok

    # == 13. VoiceSession: TTS failure degrades gracefully, still returns result ==
    print("\n--- VoiceSession: a TTS failure doesn't lose the executed result ---\n")
    from friday.registry import SkillResult

    def handle_ok(_text: str):
        return SkillResult(speech="Done that.", ok=True)

    def failing_speak(_text: str, **_kwargs):
        raise TtsBackendError("speaker unplugged")

    states: list[str] = []
    vs = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=np.ones(1600, dtype=np.float32) * 0.5),
        transcribe=lambda audio: TranscriptionResult(text="do the thing", ok=True),
        handle_text=handle_ok,
        speak=failing_speak,
        on_state=lambda state, **_d: states.append(state),
    )
    returned = vs.run_cycle()
    ok = returned is not None and returned.ok and "error" in states and "done" in states
    print(f"  {'OK  ' if ok else 'MISS'} states={states}, returned result speech={returned.speech if returned else None!r}")
    overall &= ok

    # == 14. VoiceSession controller lifecycle: re-press while busy is ignored ==
    print("\n--- controller lifecycle: activation while busy is ignored ---\n")
    gate = threading.Event()
    calls = []
    lock = threading.Lock()

    def blocking_capture(max_duration_s=None):
        with lock:
            calls.append(1)
        gate.wait(timeout=5)
        return CaptureResult(outcome="cancelled")

    vs2 = VoiceSession(
        capture=blocking_capture,
        transcribe=lambda audio: TranscriptionResult(text="", ok=True),
        handle_text=lambda text: None,
    )
    vs2.activate()
    time.sleep(0.1)  # let the first cycle actually enter capture()
    busy_during = vs2.busy
    vs2.activate()  # should be ignored — a cycle is already running
    vs2.activate()  # and again
    time.sleep(0.1)
    gate.set()
    time.sleep(0.2)  # let the first cycle finish
    not_busy_after = not vs2.busy
    ok = busy_during and len(calls) == 1 and not_busy_after
    print(f"  {'OK  ' if ok else 'MISS'} busy while running={busy_during}, "
          f"capture() invoked {len(calls)} time(s) across 3 activate() calls, busy after={vs2.busy}")
    overall &= ok

    # re-activation after completion works again
    vs2.activate()
    time.sleep(0.1)
    gate.set()
    time.sleep(0.2)
    ok2 = len(calls) == 2
    print(f"  {'OK  ' if ok2 else 'MISS'} activation after completion runs a new cycle -> {len(calls)} total calls")
    overall &= ok2

    store.init()
    overall &= asyncio.run(_async_checks())

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    return overall


async def _async_checks() -> bool:
    """Everything that needs the real brain/executor/audit: SESSION.handle
    integration, confirmation preservation, and actor/audit propagation.
    Deliberately not faked — this is the part of the design that matters most:
    voice must go through the exact same pipeline text does.
    """
    from friday import audit
    from friday.brain import BRAIN
    from friday.permissions import EXECUTOR
    from friday.registry import REGISTRY, SkillResult, skill
    from friday.session import SESSION

    overall = True

    REGISTRY.discover()
    await asyncio.to_thread(BRAIN.warm)

    # == 15. voice -> SESSION.handle integration (real brain + real executor) ==
    print("\n--- voice -> SESSION.handle integration (real pipeline, faked mic/speaker) ---\n")

    loop = asyncio.get_running_loop()

    def handle_via_session(text: str) -> SkillResult:
        future = asyncio.run_coroutine_threadsafe(SESSION.handle(text, actor="voice"), loop)
        return future.result(timeout=30)

    spoken: list[str] = []
    states: list[str] = []
    vs = VoiceSession(
        capture=lambda max_duration_s=None: CaptureResult(outcome="ok", audio=np.ones(1600, dtype=np.float32) * 0.5),
        transcribe=lambda audio: TranscriptionResult(text="what time is it", ok=True),
        handle_text=handle_via_session,
        speak=lambda text, **_: spoken.append(text) or True,
        on_state=lambda state, **_d: states.append(state),
    )
    result = await asyncio.to_thread(vs.run_cycle)
    ok = (
        result is not None and result.ok
        and len(spoken) == 1 and spoken[0] == result.speech
        and "executing" in states and "speaking" in states and "done" in states
    )
    print(f"  {'OK  ' if ok else 'MISS'} \"what time is it\" (by voice) -> {result.speech if result else None!r}")
    overall &= ok

    # == 16. audit/source propagation ========================================
    print("\n--- audit log records the voice actor, not a generic one ---\n")
    last = audit.recent(1)[0]
    ok = last["actor"] == "voice" and last["skill"] == "system.time"
    print(f"  {'OK  ' if ok else 'MISS'} audit row: actor={last['actor']!r}, skill={last['skill']!r}, decision={last['decision']!r}")
    overall &= ok

    # == 17. confirmation preservation: voice does not bypass L2/L3 confirm ==
    print("\n--- confirmation preservation: an L3 call via actor='voice' still pauses ---\n")

    executed = {"ran": False}

    @skill(
        name="test.voice_consequential_action",
        tier="L3",
        description="test-only consequential action, never does anything real",
    )
    def _consequential() -> SkillResult:
        executed["ran"] = True
        return SkillResult(speech="risky thing done")

    confirm_prompts: list[str] = []

    async def decline(_skill, _args, preview: str) -> bool:
        confirm_prompts.append(preview)
        return False

    EXECUTOR.set_confirm_handler(decline)
    declined_result = await EXECUTOR.run("test.voice_consequential_action", {}, actor="voice")
    decline_ok = (
        not declined_result.ok and declined_result.speech == "Cancelled."
        and not executed["ran"] and len(confirm_prompts) == 1
    )
    print(f"  {'OK  ' if decline_ok else 'MISS'} voice + decline -> Cancelled, skill body never ran "
          f"(executed={executed['ran']})")
    overall &= decline_ok

    async def approve(_skill, _args, preview: str) -> bool:
        confirm_prompts.append(preview)
        return True

    EXECUTOR.set_confirm_handler(approve)
    approved_result = await EXECUTOR.run("test.voice_consequential_action", {}, actor="voice")
    approve_ok = approved_result.ok and executed["ran"]
    print(f"  {'OK  ' if approve_ok else 'MISS'} voice + approve -> executes exactly like a confirmed text command "
          f"(executed={executed['ran']})")
    overall &= approve_ok

    last_confirmed = audit.recent(1)[0]
    audit_ok = last_confirmed["actor"] == "voice" and last_confirmed["decision"] == "confirmed"
    print(f"  {'OK  ' if audit_ok else 'MISS'} audit row for the approved call: "
          f"actor={last_confirmed['actor']!r}, decision={last_confirmed['decision']!r}")
    overall &= audit_ok

    # Restore a harmless default so nothing later hangs waiting on a handler.
    EXECUTOR.set_confirm_handler(SESSION._confirm)

    return overall


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
