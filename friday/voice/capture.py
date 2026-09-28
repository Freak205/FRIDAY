"""Microphone capture with voice activity detection — energy (RMS) based by
default, or driven by a neural speech classifier when one is supplied (Phase
10.X.4: see `VadSession.speech_prob_fn` and friday/voice/vad_model.py — a
fixed RMS threshold can't reliably separate a room's own noise floor from
quiet speech once the two are close, which is the real-world failure mode
that motivated adding this).

No wake word, no always-on listening (that's a later phase — see PLAN.md).
Listening still only happens between an explicit activation (the Ctrl+Alt+V
hotkey) and either silence or the configured maximum duration — but the
underlying microphone stream (`MicStream`) is opened once and kept running
for the life of the process (Phase 7P), not reopened per utterance. Opening
a PortAudio stream on Windows measured 200-800ms on this machine; paying
that cost once instead of on every hotkey press means the stream is already
flowing by the time VAD starts watching it, instead of the first half-second
of whatever the user says happening while the stream is still spinning up —
this was the dominant cause of clipped/lost first words (see PLAN.md Phase
7P for the measurements).

The VAD itself (`VadSession`) is a pure state machine fed one audio block at
a time, deliberately separated from the actual microphone I/O so it can be
tested with synthetic numpy blocks — no hardware, no mocking of PortAudio
required. See scripts/smoke_voice_pipeline.py.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

import numpy as np

from friday.log import get
from friday.voice.keys import esc_pressed
from friday.voice.types import CaptureResult, MicrophoneUnavailable

log = get(__name__)

SAMPLE_RATE = 16000
BLOCK_MS = 30
BLOCK_S = BLOCK_MS / 1000
BLOCK_SIZE = SAMPLE_RATE * BLOCK_MS // 1000


class VadSession:
    """Decides, block by block, whether an utterance has finished.

    Feed it consecutive ~30ms float32 mono blocks via `feed()`. It returns
    `None` while still listening, or a final `CaptureResult` once the
    utterance is complete (silence after speech, the time cap was hit, it
    was cancelled, or the speech was too short to count as a real utterance).

    `pre_roll_blocks` keeps the last N pre-speech blocks around and splices
    them onto the front of the recording the moment speech is detected —
    without it, a soft-onset word's first phoneme is reliably clipped
    because RMS only crosses the threshold a block or two into the word.
    `min_speech_s` rejects utterances that never had that many total seconds
    of actual speech (as opposed to trailing silence) — a cough or a chair
    creak, not a command.

    `speech_prob_fn` (Phase 10.X.4): an optional per-block speech classifier
    (`block -> 0..1 probability`, e.g. `SileroVad.predict` from
    friday/voice/vad_model.py) used instead of the raw RMS threshold to
    decide `is_speech`, compared against `vad_threshold`. Left as `None` by
    default so every existing caller — including the synthetic-block smoke
    tests in scripts/smoke_voice_pipeline.py, which script constant-amplitude
    arrays a neural VAD would never recognize as speech — keeps the original
    RMS-only behavior with zero change. `record_utterance` below is the one
    production caller that wires in the real model. See that module's
    docstring for *why* RMS alone isn't enough in a noisy room: in short, a
    fixed energy threshold can't be set anywhere that reliably separates a
    steady-state noise floor from quiet speech once the two overlap, no
    matter how the decision is smoothed/debounced — telling broadband noise
    apart from a voice needs an actual classifier.

    `vad_sustain_threshold`: once speech has started, `is_speech` is compared
    against this (lower) threshold instead of `vad_threshold` — a real word's
    probability trace isn't a clean plateau, it dips mid-word (a consonant,
    a brief dip in loudness) and a single shared threshold right at the
    entry point occasionally drops enough of those dips below it to
    undercount `min_speech_s` and wrongly reject a short-but-real command as
    noise (measured: a quick "Open Chrome." lost 3 of its ~11 speech blocks
    to a mid-word dip at threshold=0.5, landing at 0.24s counted speech
    against a 0.25s minimum). Defaults to `vad_threshold` itself (no
    hysteresis) when not given; `record_utterance` passes a validated 0.35.
    """

    def __init__(
        self,
        *,
        silence_timeout_s: float,
        max_duration_s: float,
        rms_threshold: float = 0.012,
        block_s: float = BLOCK_S,
        min_speech_s: float = 0.0,
        pre_roll_blocks: int = 0,
        warmup_ignore_s: float = 0.0,
        speech_prob_fn: Callable[[np.ndarray], float] | None = None,
        vad_threshold: float = 0.5,
        vad_sustain_threshold: float | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.silence_timeout_s = silence_timeout_s
        self.max_duration_s = max_duration_s
        self.rms_threshold = rms_threshold
        self.block_s = block_s
        self.min_speech_s = min_speech_s
        # Phase 10.X.3: the first `warmup_ignore_s` seconds of this session
        # never count as the start of speech, no matter how loud — used to
        # let a short wake-sound chime finish playing (and being picked up
        # by the mic) without VAD mistaking it for the user's command. Blocks
        # in this window are still buffered as pre-roll, so real speech that
        # starts right at the boundary isn't clipped once warmup ends.
        self.warmup_ignore_s = warmup_ignore_s
        self._speech_prob_fn = speech_prob_fn
        self.vad_threshold = vad_threshold
        self.vad_sustain_threshold = (
            vad_sustain_threshold if vad_sustain_threshold is not None else vad_threshold
        )
        self._on_event = on_event or (lambda name: None)
        self._blocks: list[np.ndarray] = []
        self._preroll: deque[np.ndarray] | None = (
            deque(maxlen=pre_roll_blocks) if pre_roll_blocks > 0 else None
        )
        self._speech_started = False
        self._speech_block_count = 0
        self._silence_run_s = 0.0
        self._elapsed_s = 0.0
        self._first_audio_seen = False
        # Diagnostics (Phase 10.X.4) — read by record_utterance() to log a
        # one-line summary per capture: speech_start_s/last_speech_s let you
        # see the actual speech span independent of when the stop fired,
        # last_rms/last_vad_prob/stop_reason pin down *why* the final block
        # ended the capture.
        self.speech_start_s: float | None = None
        self.last_speech_s: float | None = None
        self.last_rms: float = 0.0
        self.last_vad_prob: float | None = None
        self.stop_reason: str = ""

    def feed(self, block: np.ndarray, *, cancelled: bool = False) -> CaptureResult | None:
        if cancelled:
            self.stop_reason = "cancelled"
            return CaptureResult(outcome="cancelled", duration_s=self._elapsed_s)

        if not self._first_audio_seen and block.size:
            self._first_audio_seen = True
            self._on_event("first_audio")

        self._elapsed_s += self.block_s
        if self._elapsed_s >= self.max_duration_s:
            self.stop_reason = "max_duration" if self._blocks else "no_speech (max_duration, empty)"
            log.debug(
                "vad stop: reason=max_duration elapsed_s=%.2f speech_start_s=%s "
                "last_speech_s=%s silence_run_s=%.2f last_rms=%.4f last_vad_prob=%s",
                self._elapsed_s, self.speech_start_s, self.last_speech_s,
                self._silence_run_s, self.last_rms, self.last_vad_prob,
            )
            if self._blocks:
                return CaptureResult(
                    outcome="max_duration",
                    audio=np.concatenate(self._blocks),
                    duration_s=self._elapsed_s,
                )
            return CaptureResult(outcome="no_speech", duration_s=self._elapsed_s)

        rms = float(np.sqrt(np.mean(np.square(block)))) if block.size else 0.0
        self.last_rms = rms
        if self._speech_prob_fn is not None and block.size:
            vad_prob = self._speech_prob_fn(block)
            self.last_vad_prob = vad_prob
            threshold = self.vad_sustain_threshold if self._speech_started else self.vad_threshold
            is_speech = vad_prob >= threshold
        else:
            is_speech = rms >= self.rms_threshold
        # 1e-9 slack absorbs float accumulation drift on self._elapsed_s (many
        # small += additions) so the boundary block lands consistently on the
        # warmup side regardless of rounding direction.
        if is_speech and not self._speech_started and self._elapsed_s <= self.warmup_ignore_s + 1e-9:
            is_speech = False

        log.debug(
            "vad block: t=%.2f rms=%.4f vad_prob=%s is_speech=%s speech_started=%s silence_run_s=%.2f",
            self._elapsed_s, rms, self.last_vad_prob, is_speech, self._speech_started, self._silence_run_s,
        )

        if is_speech:
            if not self._speech_started:
                self._speech_started = True
                self.speech_start_s = self._elapsed_s
                self._on_event("speech_start")
                if self._preroll:
                    self._blocks.extend(self._preroll)
                    self._preroll.clear()
            self.last_speech_s = self._elapsed_s
            self._silence_run_s = 0.0
            self._speech_block_count += 1
            self._blocks.append(block)
        elif self._speech_started:
            self._silence_run_s += self.block_s
            self._blocks.append(block)
            if self._silence_run_s >= self.silence_timeout_s:
                self._on_event("speech_end")
                speech_duration_s = self._speech_block_count * self.block_s
                too_short = speech_duration_s < self.min_speech_s
                self.stop_reason = "no_speech (too_short)" if too_short else "silence_timeout"
                log.debug(
                    "vad stop: reason=%s elapsed_s=%.2f speech_start_s=%s last_speech_s=%s "
                    "silence_run_s=%.2f speech_duration_s=%.2f last_rms=%.4f last_vad_prob=%s",
                    self.stop_reason, self._elapsed_s, self.speech_start_s, self.last_speech_s,
                    self._silence_run_s, speech_duration_s, self.last_rms, self.last_vad_prob,
                )
                if too_short:
                    return CaptureResult(outcome="no_speech", duration_s=self._elapsed_s)
                return CaptureResult(
                    outcome="ok", audio=np.concatenate(self._blocks), duration_s=self._elapsed_s
                )
        elif self._preroll is not None:
            # Leading silence before speech starts: buffer it for the splice
            # above instead of discarding it outright.
            self._preroll.append(block)
        return None


def drive(
    blocks: Iterable[np.ndarray],
    *,
    silence_timeout_s: float,
    max_duration_s: float,
    rms_threshold: float = 0.012,
    block_s: float = BLOCK_S,
    min_speech_s: float = 0.0,
    pre_roll_blocks: int = 0,
    warmup_ignore_s: float = 0.0,
    speech_prob_fn: Callable[[np.ndarray], float] | None = None,
    vad_threshold: float = 0.5,
    vad_sustain_threshold: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    on_event: Callable[[str], None] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> CaptureResult:
    """Run a VadSession to completion over a block source. Used by both the
    real microphone reader below and deterministic tests with scripted blocks.

    `diagnostics`, if given, is updated in place with a snapshot of the
    session's end-of-capture state (speech_start_s, last_speech_s, last_rms,
    last_vad_prob, silence_run_s) — record_utterance() uses this to log a
    one-line diagnostic summary per capture.
    """
    session = VadSession(
        silence_timeout_s=silence_timeout_s,
        max_duration_s=max_duration_s,
        rms_threshold=rms_threshold,
        block_s=block_s,
        min_speech_s=min_speech_s,
        pre_roll_blocks=pre_roll_blocks,
        warmup_ignore_s=warmup_ignore_s,
        speech_prob_fn=speech_prob_fn,
        vad_threshold=vad_threshold,
        vad_sustain_threshold=vad_sustain_threshold,
        on_event=on_event,
    )
    try:
        for block in blocks:
            cancelled = cancel_check() if cancel_check else False
            result = session.feed(block, cancelled=cancelled)
            if result is not None:
                result.stop_reason = session.stop_reason
                return result
        # Block source exhausted without the session declaring an end — treat
        # whatever was captured as a max-duration stop rather than hanging.
        result = session.feed(np.zeros(0, dtype=np.float32), cancelled=False) or CaptureResult(
            outcome="no_speech", duration_s=session._elapsed_s
        )
        result.stop_reason = session.stop_reason or "block_source_exhausted"
        return result
    finally:
        if diagnostics is not None:
            diagnostics.update(
                speech_start_s=session.speech_start_s,
                last_speech_s=session.last_speech_s,
                last_rms=session.last_rms,
                last_vad_prob=session.last_vad_prob,
                silence_run_s=session._silence_run_s,
            )



# Phase 10.X.7: how much raw audio MicStream keeps in its always-on ring
# buffer so a brand-new session can be seeded with whatever was said in the
# gap between a wake-word detection firing and this session actually opening
# (model scoring + FSM transitions + chime dispatch — all measured to be
# well under 100ms on this machine, see PLAN.md Phase 10.X.7, but this is a
# generous multiple of that so command capture is never one architecture
# change away from clipping "open VS Code" again).
RING_BUFFER_S = 1.5


class MicStream:
    """A single persistent `sounddevice.InputStream`, opened once (lazily,
    or eagerly via `ensure_started()` at startup) and kept running for the
    life of the process rather than per utterance.

    Multiple "sessions" (one per voice activation) can tap the same running
    stream via `open_session()` / `close_session()` — each gets its own
    queue fed by the shared PortAudio callback, so a session that isn't
    listening just doesn't register a queue rather than the stream itself
    starting and stopping.

    Phase 10.X.7 (pre-roll): every block is also timestamped
    (`time.monotonic()`) and kept in a short rolling ring buffer regardless
    of whether any session is listening. `open_session(preroll_since=...)`
    can then seed a brand-new session's queue with whatever was already
    captured from that timestamp onward — see that method's docstring for
    why this matters for the wake-word -> command-capture handoff.
    """

    def __init__(self, *, device: int | None) -> None:
        self.device = device
        self._stream: Any | None = None
        self._start_lock = threading.Lock()
        self._sessions: list[queue.Queue[np.ndarray]] = []
        # Guards both `_sessions` and `_ring` — a session must be registered
        # for live delivery in the same locked section it's seeded with the
        # ring buffer's current contents, or a block could land in neither
        # (dropped: arrives between the snapshot and registration) or both
        # (duplicated: arrives before the snapshot but session is already
        # live). See open_session().
        self._sessions_lock = threading.Lock()
        self._ring: deque[tuple[float, np.ndarray]] = deque(maxlen=round(RING_BUFFER_S / BLOCK_S))

    def _callback(self, indata: np.ndarray, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            log.debug("mic stream status flag: %s", status)
        block = indata[:, 0].copy()
        # perf_counter(), not monotonic(): measured on this machine, monotonic()
        # is GetTickCount64-backed with only ~15.6ms resolution (see PLAN.md
        # Phase 10.X.7) — several blocks in a row can tie, which combined with
        # the `>=` cutoff below would leak pre-detection audio (up to ~15ms of
        # the wake phrase itself) into a "preroll_since"-seeded session.
        # perf_counter() is QueryPerformanceCounter-backed (sub-microsecond)
        # and still monotonic, so it's a strict drop-in for this internal
        # timestamp-ordering use (never shown to the user, never compared
        # against wall-clock time).
        now = time.perf_counter()
        with self._sessions_lock:
            self._ring.append((now, block))
            sessions = list(self._sessions)
        for q in sessions:
            try:
                q.put_nowait(block)
            except queue.Full:
                log.warning("voice session queue full — dropping an audio block")

    def ensure_started(self) -> None:
        if self._stream is not None:
            return
        with self._start_lock:
            if self._stream is not None:
                return
            import sounddevice as sd

            try:
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                    blocksize=BLOCK_SIZE, device=self.device, callback=self._callback,
                )
                stream.start()
            except Exception as exc:
                raise MicrophoneUnavailable(
                    f"couldn't open the microphone ({exc}). Check Windows' microphone "
                    "privacy settings and that a default input device is selected."
                ) from exc
            self._stream = stream
            log.info("persistent mic stream started (device=%s)", self.device)

    def open_session(self, *, preroll_since: float | None = None) -> "queue.Queue[np.ndarray]":
        """`preroll_since` (Phase 10.X.7): if given (a `time.perf_counter()`
        timestamp — see `_callback`'s docstring note for why not
        `time.monotonic()`), the new queue is pre-seeded with every ring-buffered
        block captured at or after that instant, before it starts receiving
        live blocks — so a command-listen session opened right after a wake
        detection doesn't lose whatever was said in the gap between the
        detection firing and this call actually running. Seeding and
        registering happen under the same lock the callback also holds, so
        no block can be skipped (arrived between snapshot and registration)
        or duplicated (arrived before the snapshot but the session was
        already live).
        """
        q: queue.Queue[np.ndarray] = queue.Queue(maxsize=4000)
        with self._sessions_lock:
            if preroll_since is not None:
                for ts, block in self._ring:
                    if ts >= preroll_since:
                        q.put_nowait(block)
            self._sessions.append(q)
        return q

    def close_session(self, q: "queue.Queue[np.ndarray]") -> None:
        with self._sessions_lock:
            if q in self._sessions:
                self._sessions.remove(q)

    def stop(self) -> None:
        with self._start_lock:
            if self._stream is None:
                return
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("error stopping mic stream")
            self._stream = None
            log.info("persistent mic stream stopped (device=%s)", self.device)


_streams: dict[int | None, MicStream] = {}
_streams_lock = threading.Lock()


def get_mic_stream(device: int | None) -> MicStream:
    with _streams_lock:
        stream = _streams.get(device)
        if stream is None:
            stream = MicStream(device=device)
            _streams[device] = stream
        return stream


def warm_up_microphone(device: int | None = None) -> None:
    """Best-effort: opens the persistent mic stream ahead of time (called
    from friday.gui.app at startup, on a background thread) so the first
    real activation doesn't pay PortAudio's stream-open latency. Failures
    are logged and swallowed — record_utterance() will try again (and raise
    MicrophoneUnavailable normally) whenever voice is actually used.
    """
    try:
        get_mic_stream(device).ensure_started()
    except MicrophoneUnavailable as exc:
        log.warning("microphone warm-up failed, will retry on first activation: %s", exc)


def shutdown_mic_streams() -> None:
    """Release all open microphone streams. Call on app quit."""
    with _streams_lock:
        streams = list(_streams.values())
    for stream in streams:
        stream.stop()


def _session_blocks(
    q: "queue.Queue[np.ndarray]", *, idle_timeout_s: float
) -> Iterable[np.ndarray]:
    """Yields blocks pushed onto `q` by the mic stream's callback until no
    new block arrives for `idle_timeout_s` (only happens if the stream
    itself has stalled/died — normal operation always has a block every
    ~30ms) — a safety net so a dead stream can't hang a capture forever.
    """
    while True:
        try:
            yield q.get(timeout=idle_timeout_s)
        except queue.Empty:
            return


def record_utterance(
    *,
    silence_timeout_s: float,
    max_duration_s: float,
    rms_threshold: float = 0.012,
    min_speech_s: float = 0.0,
    pre_roll_ms: float = 300.0,
    warmup_ignore_s: float = 0.0,
    vad_enabled: bool = True,
    vad_threshold: float = 0.5,
    vad_sustain_threshold: float = 0.35,
    device: int | None = None,
    preroll_since: float | None = None,
) -> CaptureResult:
    """Record one utterance from the real microphone, bounded by silence
    detection, a hard time cap, and Esc cancellation. Raises
    MicrophoneUnavailable if the device can't be opened or read.

    `warmup_ignore_s` (Phase 10.X.3): see VadSession — pass this right after
    playing the wake-sound chime so its own tail can't be VAD-detected as
    the start of the command.

    `preroll_since` (Phase 10.X.7): a `time.perf_counter()` timestamp —
    usually the instant a wake word was detected — passed straight to
    `MicStream.open_session()` so this capture starts with whatever audio
    already arrived since then instead of only what arrives from here on.
    Independent of (and stacks with) `pre_roll_ms`/`warmup_ignore_s`, which
    are about VAD's own pre-speech buffer and the chime's tail, not the
    session-open handoff gap.

    `vad_enabled`/`vad_threshold` (Phase 10.X.4): when the bundled Silero VAD
    model (friday.voice.vad_model) is available, it drives the is_speech
    decision instead of the raw `rms_threshold` compare — see
    friday/voice/vad_model.py's docstring for why: a fixed energy threshold
    can't reliably tell a room's own noise floor apart from quiet speech once
    the two are close, which is exactly what caused captures to run to
    `max_duration_s` in a real, moderately noisy room even after the user had
    stopped talking. `rms_threshold` is still honored as the fallback if the
    model fails to load (and RMS is always computed for the diagnostic log
    line below, regardless of which one is deciding).

    `result.timings` carries perf_counter() timestamps for `stream_ready`
    (mic stream confirmed running for this call), `first_audio` (first block
    actually read), `speech_start`, and `speech_end` — see friday.voice.session
    for how these feed into the end-to-end latency log line. `result.stop_reason`
    carries why the capture ended (silence_timeout/max_duration/no_speech/
    cancelled) — see VadSession.feed.
    """
    speech_prob_fn = None
    vad_backend = "rms_only"
    if vad_enabled:
        from friday.voice.vad_model import get_shared as get_vad_model

        vad_model = get_vad_model()
        if vad_model is not None:
            vad_model.reset()
            speech_prob_fn = vad_model.predict
            vad_backend = "silero"

    log.info(
        "mic capture starting (device=%s, vad_backend=%s, rms_threshold=%s, "
        "vad_threshold=%s, vad_sustain_threshold=%s, silence_timeout_s=%s, pre_roll_ms=%s)",
        device, vad_backend, rms_threshold, vad_threshold, vad_sustain_threshold,
        silence_timeout_s, pre_roll_ms,
    )
    t0 = time.perf_counter()
    stream = get_mic_stream(device)
    stream.ensure_started()
    t_ready = time.perf_counter()
    if t_ready - t0 > 0.05:
        log.info("mic stream cold-start took %.3fs (first activation this run)", t_ready - t0)

    q = stream.open_session(preroll_since=preroll_since)
    timings: dict[str, float] = {"stream_ready": t_ready}

    def on_event(name: str) -> None:
        timings[name] = time.perf_counter()

    pre_roll_blocks = max(0, round((pre_roll_ms / 1000.0) / BLOCK_S))
    # A dead/stalled stream would otherwise hang forever waiting on the
    # queue; max_duration_s plus slack is generous enough to never fire
    # during normal operation but still bounded.
    idle_timeout_s = max(max_duration_s, 5.0) + 2.0
    diagnostics: dict[str, Any] = {}
    try:
        result = drive(
            _session_blocks(q, idle_timeout_s=idle_timeout_s),
            silence_timeout_s=silence_timeout_s,
            max_duration_s=max_duration_s,
            rms_threshold=rms_threshold,
            min_speech_s=min_speech_s,
            pre_roll_blocks=pre_roll_blocks,
            warmup_ignore_s=warmup_ignore_s,
            speech_prob_fn=speech_prob_fn,
            vad_threshold=vad_threshold,
            vad_sustain_threshold=vad_sustain_threshold,
            cancel_check=esc_pressed,
            on_event=on_event,
            diagnostics=diagnostics,
        )
    finally:
        stream.close_session(q)

    result.timings = timings
    log.info(
        "mic capture finished: outcome=%s stop_reason=%s duration_s=%.2f has_audio=%s "
        "(stream ready in %.3fs, backend=%s)",
        result.outcome, result.stop_reason, result.duration_s, result.has_audio,
        t_ready - t0, vad_backend,
    )
    log.info(
        "mic capture diagnostics: speech_start_s=%s last_speech_s=%s silence_run_s=%s "
        "last_rms=%.4f last_vad_prob=%s",
        diagnostics.get("speech_start_s"), diagnostics.get("last_speech_s"),
        diagnostics.get("silence_run_s"), diagnostics.get("last_rms", 0.0),
        diagnostics.get("last_vad_prob"),
    )
    return result
