"""Manual real-microphone smoke test for the voice interface.

Unlike scripts/smoke_voice_pipeline.py (fully deterministic, no hardware),
this one actually opens your microphone and, optionally, speaks through your
speakers. It does NOT run any command through FRIDAY's session/executor —
it only proves the mic -> STT -> (optional) TTS path works on this machine,
so it's safe to run without triggering anything consequential.

Usage:
    python scripts/smoke_voice.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday.config import CFG  # noqa: E402
from friday.voice.capture import record_utterance  # noqa: E402
from friday.voice.stt import SttEngine  # noqa: E402
from friday.voice.tts import TtsEngine  # noqa: E402
from friday.voice.types import MicrophoneUnavailable  # noqa: E402


def main() -> int:
    voice = CFG.voice
    print(f"\nFRIDAY voice smoke test — model={voice.stt.model!r} device={voice.stt.device!r}\n")

    # -- 1. microphone access -------------------------------------------------
    print("1) Checking microphone access...")
    try:
        import sounddevice as sd

        default_in = sd.query_devices(kind="input")
        print(f"   default input device: {default_in['name']!r}")
    except Exception as exc:
        print(f"   FAILED — sounddevice couldn't list input devices: {exc}")
        print("   Is `sounddevice` installed? `pip install -r requirements.txt`")
        return 1

    # -- 2. record one short utterance ----------------------------------------
    print(f"\n2) Speak now — recording stops after {voice.stt.silence_timeout_s}s of "
          f"silence or {voice.stt.max_recording_s}s max. Press Esc to cancel.\n")
    print("   🎤 Listening...")
    try:
        capture = record_utterance(
            silence_timeout_s=voice.stt.silence_timeout_s,
            max_duration_s=voice.stt.max_recording_s,
            rms_threshold=voice.stt.silence_rms_threshold,
            device=voice.stt.input_device,
        )
    except MicrophoneUnavailable as exc:
        print(f"   FAILED — {exc}")
        return 1

    if capture.outcome == "cancelled":
        print("   Cancelled (Esc pressed). Nothing recorded.")
        return 0
    if not capture.has_audio:
        print("   No speech detected — try again closer to the mic, or louder.")
        return 0

    print(f"   Captured {capture.duration_s:.1f}s of audio "
          f"({'hit the max-duration cap' if capture.outcome == 'max_duration' else 'silence-terminated'}).")

    # -- 3/4. transcribe and display ------------------------------------------
    print("\n3) Transcribing locally (first run downloads the model — may take a "
          "minute)...")
    engine = SttEngine(
        model_size=voice.stt.model, language=voice.stt.language,
        device=voice.stt.device, compute_type=voice.stt.compute_type,
    )
    result = engine.transcribe(capture.audio)
    if not result.ok:
        print(f"   FAILED — {result.error}")
        return 1
    if not result.text.strip():
        print("   Transcription came back empty — try speaking louder or closer to the mic.")
        return 0

    print(f"\n   You said: \"{result.text}\"\n")

    # -- 5. optional speak-back ------------------------------------------------
    if not voice.tts.enabled:
        print("4) TTS is disabled in config.yaml (voice.tts.enabled: false) — skipping.")
        return 0

    try:
        answer = input("4) Speak it back through your speakers? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer not in ("y", "yes"):
        print("   Skipped.")
        return 0

    tts = TtsEngine(voice_id=voice.tts.voice_id, rate=voice.tts.rate)
    print("   🔊 Speaking...")
    try:
        tts.speak(result.text)
    except Exception as exc:
        print(f"   FAILED — {exc}")
        return 1

    print("\nDone. This did not run any FRIDAY command — it only tested mic/STT/TTS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
