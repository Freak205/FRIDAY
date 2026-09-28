"""Local wake-word detection via openWakeWord (ONNX runtime, CPU-only).

Honest status (see PLAN.md Phase 8A/8O for the full evaluation): openWakeWord
ships pretrained models for "alexa", "hey_jarvis", "hey_mycroft",
"hey_rhasspy", "timer", and "weather" — there is no pretrained "FRIDAY"
model, and training a reliable custom one needs a synthetic-speech data
pipeline plus GPU hours this phase doesn't spend. Rather than fake it with
an always-on Whisper transcript scan (explicitly ruled out — see PLAN.md),
this adapter defaults to "hey_jarvis" as a practical placeholder phrase and
is built to be reconfigured or swapped for a real "FRIDAY" model the moment
one exists (`voice.wakeword.model` in config.yaml). Ctrl+Alt+V remains the
reliable, always-available activation regardless of wake-word state.

Kept as a small adapter (`WakeWordDetector`) around whatever
`openwakeword.Model` needs so a future engine swap touches one file, the
same shape as `friday.voice.stt.SttEngine`/`friday.voice.tts.TtsEngine`.
"""

from __future__ import annotations

import threading
from collections import deque
from pathlib import Path
from typing import Protocol

import numpy as np

from friday import paths
from friday.log import get
from friday.voice.types import WakeWordBackendError

log = get(__name__)

# openWakeWord's native frame size: 80ms of 16kHz audio. Its own internal
# streaming buffer only produces a fresh prediction once this many samples
# have accumulated — see `feed()` below for why blocks are re-buffered to it.
CHUNK_SAMPLES = 1280
FRAME_S = CHUNK_SAMPLES / 16000  # 0.08s

# Phase 10.X.9: per-frame RMS gain normalization was tried here (pulling
# each frame toward a target level before quantizing/scoring) on the theory
# that the Phase 10.X.8 corpus's near-zero-scoring clips were an input-level
# problem. Measured against the real held-out test split
# (data/wakeword_samples/20260913_234931_split/test) via
# scripts/evaluate_wake_samples.py --verifier-model data/models/wake_verifier.pkl:
# detection dropped from the documented 9/12 (75%) to 7/12 (58%), both with
# and without attenuation of loud frames — the verifier was fit on the base
# model's *unmodified* embeddings (Phase 10.X.8), so shifting input level at
# inference time moves frames off the distribution it learned, hurting more
# clips than it recovers. Reverted; kept as a documented negative result so
# a future session doesn't re-try the same thing on the same theory.


class WakeScoreWindow:
    """Rolling-window wake decision (Phase 10.X.6) — pure logic, no I/O, so
    it's directly unit-testable with a scripted score sequence (see
    scripts/smoke_voice_conversation.py).

    The old behavior compared a single fresh openWakeWord frame's score
    against `threshold` the instant it arrived. That's brittle for an
    accented "hey jarvis": the phrase spans ~8-15 frames as it's spoken, and
    it only takes one of them landing a hair under threshold at the moment
    the model's peak confidence happens to occur for the whole utterance to
    be missed. Keeping the last `window_frames` scores and firing once
    `persist_frames` of them have crossed `threshold` fixes that without
    just lowering the threshold blind — `persist_frames >= 2` also means a
    single random noise spike (which real speech reliably beats, since it
    spans many frames) can't fire on its own even at a lower threshold.
    """

    def __init__(self, *, threshold: float, window_frames: int, persist_frames: int = 1) -> None:
        self.threshold = threshold
        self.window_frames = max(1, window_frames)
        self.persist_frames = max(1, persist_frames)
        self._scores: deque[float] = deque(maxlen=self.window_frames)

    def reset(self) -> None:
        self._scores.clear()

    def push(self, score: float) -> tuple[float, bool]:
        """Feed one new per-frame score. Returns (rolling_peak, triggered)."""
        self._scores.append(score)
        above = sum(1 for s in self._scores if s >= self.threshold)
        peak = max(self._scores) if self._scores else 0.0
        return peak, above >= self.persist_frames

    @property
    def peak(self) -> float:
        return max(self._scores) if self._scores else 0.0


class WakeModelLike(Protocol):
    models: dict[str, object]

    def predict(self, x: np.ndarray) -> dict[str, float]: ...


class WakeWordDetector:
    """Lazily loads an openWakeWord model and scores 16kHz mono audio for
    one configured wake phrase.

    Feed it audio blocks of any size via `feed()` — the persistent
    microphone stream (friday.voice.capture.MicStream) delivers 30ms/480-
    sample blocks, which don't divide evenly into openWakeWord's native
    1280-sample frames, so this buffers internally and only scores once a
    full frame is available.
    """

    def __init__(
        self,
        *,
        model_name: str = "hey_jarvis",
        models_dir: Path | None = None,
        model: WakeModelLike | None = None,
        verifier_model_path: Path | str | None = None,
        verifier_threshold: float = 0.1,
    ) -> None:
        self.model_name = model_name
        self.models_dir = models_dir or (paths.MODELS / "openwakeword")
        self._model: WakeModelLike | None = model
        self._pred_key: str = model_name
        # Phase 10.X.7: an optional voice-specific verifier (see
        # openwakeword.custom_verifier_model.train_custom_verifier, wrapped
        # by scripts/train_wake_verifier.py) layered on top of the pretrained
        # model via openWakeWord's own built-in `custom_verifier_models`
        # support — see _load() below. None (the default) is a complete
        # no-op: behavior is identical to before this phase.
        self.verifier_model_path = Path(verifier_model_path) if verifier_model_path else None
        self.verifier_threshold = verifier_threshold
        self._lock = threading.Lock()
        self._buffer = np.zeros(0, dtype=np.int16)
        # Diagnostics (Phase 10.X.6) — read by ConversationLoop's debug
        # logging (voice.wakeword.debug) so score/RMS/frame-count can be
        # inspected without a mic-level trace tool.
        self.last_rms: float = 0.0
        self.frames_processed: int = 0

    def _load(self) -> tuple[WakeModelLike, str]:
        try:
            from openwakeword.model import Model
            from openwakeword.utils import download_models
        except ImportError as exc:
            raise WakeWordBackendError(
                f"openwakeword isn't installed ({exc}); run "
                "`pip install openwakeword` or set voice.wakeword.enabled: false"
            ) from exc

        self.models_dir.mkdir(parents=True, exist_ok=True)
        try:
            download_models([self.model_name], target_directory=str(self.models_dir))
        except Exception as exc:
            raise WakeWordBackendError(
                f"couldn't download the '{self.model_name}' wake-word model "
                f"(needs internet on first run only, then it's cached locally): {exc}"
            ) from exc

        candidates = sorted(self.models_dir.glob(f"{self.model_name}*.onnx"))
        if not candidates:
            raise WakeWordBackendError(
                f"model file for '{self.model_name}' not found in {self.models_dir} after download"
            )
        model_path = candidates[0]
        melspec_path = self.models_dir / "melspectrogram.onnx"
        embedding_path = self.models_dir / "embedding_model.onnx"

        model_kwargs: dict[str, object] = {}
        pred_key_hint = self.model_name
        if self.verifier_model_path is not None:
            if not self.verifier_model_path.exists():
                raise WakeWordBackendError(
                    f"wake-word verifier model not found at {self.verifier_model_path} "
                    "(train one with scripts/train_wake_verifier.py, or unset "
                    "voice.wakeword.verifier_model_path)"
                )
            # openWakeWord keys `custom_verifier_models` by the *base* model's
            # prediction key, which for a bare filename like "hey_jarvis" is
            # the filename stem — matches `pred_key` below once the model is
            # actually loaded, but the dict has to be built before that.
            pred_key_hint = Path(model_path).stem
            model_kwargs["custom_verifier_models"] = {pred_key_hint: str(self.verifier_model_path)}
            model_kwargs["custom_verifier_threshold"] = self.verifier_threshold

        try:
            # ncpu=1: this runs continuously in the background for as long as
            # FRIDAY is idle, so it's deliberately pinned to one thread rather
            # than left to onnxruntime's default (which can spin up one
            # intra-op thread per core) — see PLAN.md Phase 8J for the
            # measured CPU cost this keeps to ~2-3% of one core.
            model = Model(
                wakeword_models=[str(model_path)],
                inference_framework="onnx",
                melspec_model_path=str(melspec_path),
                embedding_model_path=str(embedding_path),
                ncpu=1,
                **model_kwargs,
            )
        except Exception as exc:
            raise WakeWordBackendError(f"couldn't load wake-word model: {exc}") from exc

        pred_key = next(iter(model.models.keys()), pred_key_hint)
        if self.verifier_model_path is not None and pred_key not in getattr(model, "custom_verifier_models", {}):
            log.warning(
                "wake-word verifier configured but not attached (prediction key %r != "
                "expected %r) — falling back to the base model only", pred_key, pred_key_hint,
            )
        return model, pred_key

    def _ensure_model(self) -> tuple[WakeModelLike, str]:
        if self._model is not None:
            return self._model, self._pred_key
        with self._lock:
            if self._model is None:
                self._model, self._pred_key = self._load()
            return self._model, self._pred_key

    def warm_up(self) -> None:
        """Best-effort pre-load, mirrors SttEngine.warm_up — a failure here
        just means the first real `feed()` pays the load/download cost and
        raises the same WakeWordBackendError it always would.
        """
        try:
            self._ensure_model()
        except WakeWordBackendError as exc:
            log.warning("wake-word warm-up failed, will retry on first use: %s", exc)

    def reset(self) -> None:
        """Drop any buffered-but-not-yet-scored audio, AND (Phase 10.X.6) the
        underlying openWakeWord model's own internal buffers. Called whenever
        the conversation loop re-enters IDLE after a suppressed period (Phase
        8D) so stale audio buffered while FRIDAY was speaking/executing can't
        produce a delayed false trigger the instant listening resumes.

        openWakeWord's `Model` keeps up to ~10s of raw audio and ~10s of
        computed embedding history internally (`AudioFeatures.raw_data_buffer`/
        `feature_buffer`) purely as a sliding window fed by every `predict()`
        call — clearing only this adapter's own pre-frame `_buffer` (as
        before Phase 10.X.6) left that longer-lived context untouched, so a
        detection episode's own audio (the wake phrase, the chime, room
        echo of FRIDAY's TTS) could still be sitting in the model's window
        the next time scanning resumed. `Model.reset()` clears both.
        """
        self._buffer = np.zeros(0, dtype=np.int16)
        if self._model is not None and hasattr(self._model, "reset"):
            try:
                self._model.reset()  # type: ignore[attr-defined]
            except Exception:
                log.debug("wake-word model reset() failed (non-fatal)", exc_info=True)

    def feed(self, block: np.ndarray) -> float | None:
        """`block`: float32 mono samples in [-1, 1], the same format the
        persistent MicStream delivers. Returns the newest confidence score
        (0-1) for the configured wake phrase, or None if this call didn't
        complete a full 1280-sample frame (nothing new to report yet — not
        the same as "no wake word heard").

        Raises WakeWordBackendError if the model can't be loaded (caller
        should catch this once, log it, and disable wake-word listening for
        the run rather than retrying every block).
        """
        model, key = self._ensure_model()
        block_f32 = np.asarray(block, dtype=np.float32)
        if block_f32.size:
            self.last_rms = float(np.sqrt(np.mean(np.square(block_f32))))
        int16_block = np.clip(block_f32 * 32768.0, -32768, 32767).astype(np.int16)
        self._buffer = np.concatenate([self._buffer, int16_block])

        score: float | None = None
        while len(self._buffer) >= CHUNK_SAMPLES:
            chunk, self._buffer = self._buffer[:CHUNK_SAMPLES], self._buffer[CHUNK_SAMPLES:]
            predictions = model.predict(chunk)
            score = float(predictions.get(key, next(iter(predictions.values()), 0.0)))
            self.frames_processed += 1
        return score
