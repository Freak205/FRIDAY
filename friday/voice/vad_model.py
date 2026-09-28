"""Neural (Silero) voice-activity detection — a per-block speech-probability
model that `VadSession` (see capture.py) can use instead of a bare RMS
threshold to decide whether a block is speech.

Why this exists (Phase 10.X.4): a 20-utterance real-mic test showed 13/20
captures running all the way to the 15s `max_duration_s` cap even though the
user had stopped talking within 1-2s. Replaying the raw recordings offline
(scripts/voice_mic_latency_test.py --save-samples writes them to
data/voice_test_samples/) showed why: this room/mic's background noise floor
has a median block RMS of ~0.010-0.015 — almost exactly the 0.012
`silence_rms_threshold` — so on roughly half of all *silent* blocks, noise
alone pushes RMS back over the threshold and resets VadSession's silence
timer before it can accumulate the configured 0.8s of quiet. No amount of
temporal smoothing/debounce on the RMS signal fixes this, because the
problem isn't noise (a transient outlier smoothing can average away), it's
that the noise floor's own steady-state distribution straddles the
threshold. What's needed is a signal that can actually tell broadband room
noise apart from a human voice — which, replayed against the exact same
recordings, this model does cleanly (background stayed below ~0.45
probability everywhere; real speech scored 0.7-0.99).

Reuses the Silero VAD ONNX model openWakeWord already downloads into
`data/models/openwakeword/` for its own wake-word gating (see wakeword.py) —
no new model, no new download, no new dependency (onnxruntime is already
required). ~0.2ms per 30ms block measured on this machine — cheap enough to
run on every block without a fast-path RMS pre-filter.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from friday import paths
from friday.log import get

log = get(__name__)

_SR = np.array(16000, dtype=np.int64)
_STATE_SHAPE = (2, 1, 64)


def default_model_path() -> Path:
    return paths.MODELS / "openwakeword" / "silero_vad.onnx"


class SileroVad:
    """Wraps the bundled Silero VAD ONNX model. Carries its own LSTM hidden
    state (`h`/`c`) across calls, so it is NOT safe to share between two
    concurrent utterances — but FRIDAY only ever records one utterance at a
    time, so a single warmed-once instance with `reset()` called at the start
    of each new capture (see `record_utterance`) avoids paying the ~50ms
    ONNX session-load cost on every activation.
    """

    def __init__(self, model_path: str | None = None) -> None:
        import onnxruntime as ort  # local import: keeps this module importable

        path = model_path or str(default_model_path())
        self._session = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._c = np.zeros(_STATE_SHAPE, dtype=np.float32)

    def predict(self, block: np.ndarray) -> float:
        """`block`: float32 mono samples in [-1, 1] (exactly what capture.py's
        MicStream already produces). Returns a 0-1 speech probability.
        Validated at 480 samples (30ms @ 16kHz) — capture.py's BLOCK_SIZE —
        but any length works, the model just chunks internally.
        """
        x = np.ascontiguousarray(block, dtype=np.float32)[None, :]
        out, self._h, self._c = self._session.run(
            None, {"input": x, "h": self._h, "c": self._c, "sr": _SR}
        )
        return float(out[0][0])


_singleton: SileroVad | None = None
_singleton_lock = threading.Lock()
_load_failed = False


def get_shared() -> SileroVad | None:
    """Returns the process-wide SileroVad instance, loading it on first call
    (or returning None if it can't be loaded — missing model file, broken
    onnxruntime install, etc.). Best-effort: a failure here must never break
    voice capture, only fall it back to plain RMS-threshold VAD (see
    capture.record_utterance) — logged once, not on every capture attempt.
    """
    global _singleton, _load_failed
    if _singleton is not None:
        return _singleton
    if _load_failed:
        return None
    with _singleton_lock:
        if _singleton is not None:
            return _singleton
        if _load_failed:
            return None
        try:
            _singleton = SileroVad()
            log.info("Silero VAD model loaded (%s)", default_model_path())
        except Exception:
            log.warning(
                "Silero VAD model failed to load — voice capture will fall back "
                "to RMS-threshold silence detection (noisier rooms may over-run "
                "toward max_recording_s)",
                exc_info=True,
            )
            _load_failed = True
    return _singleton


def warm_up() -> None:
    """Best-effort pre-load, run on a background thread at startup (mirrors
    capture.warm_up_microphone) so the first real activation doesn't pay the
    ~50ms ONNX session-load cost.
    """
    get_shared()
