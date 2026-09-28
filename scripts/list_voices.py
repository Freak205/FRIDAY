"""List installed SAPI voices so you can pick one for config.yaml's
`voice.tts.voice_id`.

    python scripts/list_voices.py

For each installed voice, prints its name, id (the exact string to paste
into config.yaml), and whatever language/gender/age metadata Windows
reports (not every voice reports all three). Doesn't speak anything, doesn't
touch the microphone, doesn't call any FRIDAY skill — safe to run anytime.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> bool:
    try:
        import pyttsx3
    except ImportError:
        print("pyttsx3 isn't installed — run: pip install -r requirements.txt")
        return False

    try:
        engine = pyttsx3.init()
    except Exception as exc:
        print(f"couldn't start the SAPI engine: {exc}")
        return False

    voices = engine.getProperty("voices")
    if not voices:
        print("no SAPI voices found on this machine.")
        return False

    current_id = engine.getProperty("voice")
    print(f"{len(voices)} voice(s) installed. Current default: {current_id}\n")

    for v in voices:
        languages = getattr(v, "languages", None) or []
        # pyttsx3/SAPI sometimes hands back raw bytes for language codes.
        languages = [
            lang.decode("utf-8", "ignore") if isinstance(lang, bytes) else str(lang)
            for lang in languages
        ]
        marker = " (current default)" if v.id == current_id else ""
        print(f"- {v.name}{marker}")
        print(f"    id:       {v.id}")
        if languages:
            print(f"    language: {', '.join(languages)}")
        if getattr(v, "gender", None):
            print(f"    gender:   {v.gender}")
        if getattr(v, "age", None):
            print(f"    age:      {v.age}")
        print()

    print("To use one, set in config.yaml:\n")
    print("  voice:")
    print("    tts:")
    print(f'      voice_id: "{voices[0].id}"')
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
