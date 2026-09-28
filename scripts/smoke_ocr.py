"""Screen OCR: capability import, Tesseract-missing handling, skill registration.

Runs without a real desktop: OCR correctness is checked against a generated
image with known text rather than a live screenshot. If Tesseract isn't
installed on this machine, the test instead asserts that every path fails
cleanly with an actionable error rather than crashing.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from friday import ocr  # noqa: E402
from friday.brain import BRAIN  # noqa: E402
from friday.registry import REGISTRY  # noqa: E402
from friday.session import SESSION  # noqa: E402

MATCH_CASES = [
    ("read my screen", "screen.read_text"),
    ("what text is on my screen", "screen.read_text"),
    ("read what's currently open", "screen.read_text"),
    ("what does my screen say", "screen.read_text"),
]


def _known_text_image():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 100), color="white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 30), "HELLO FRIDAY", fill="black")
    return image


async def main() -> None:
    REGISTRY.discover()
    BRAIN.warm()
    overall = True

    print("\n--- registration ---\n")
    registered = REGISTRY.get("screen.read_text") is not None
    print(f"  {'OK  ' if registered else 'MISS'} screen.read_text registered")
    overall &= registered

    print("\n--- tesseract availability ---\n")
    try:
        version = ocr.check_available()
        available = True
        print(f"  OK   tesseract found, version {version}")
    except ocr.TesseractNotAvailable as exc:
        available = False
        print(f"  OK   tesseract missing -> clean actionable error:\n       {exc}")

    print("\n--- OCR on a generated image with known text ---\n")
    image = _known_text_image()
    if available:
        try:
            result = ocr.read_image(image)
            found = "HELLO" in result.text.upper() and "FRIDAY" in result.text.upper()
            print(f"  {'OK  ' if found else 'MISS'} recognized text: {result.text!r}")
            has_boxes = bool(result.words) and all(w.width > 0 and w.height > 0 for w in result.words)
            print(f"  {'OK  ' if has_boxes else 'MISS'} bounding boxes present: {len(result.words)} words")
            overall &= found and has_boxes
        except Exception as exc:
            print(f"  FAIL unexpected exception during OCR: {type(exc).__name__}: {exc}")
            overall = False
    else:
        try:
            ocr.read_image(image)
            print("  FAIL expected TesseractNotAvailable, OCR silently ran")
            overall = False
        except ocr.TesseractNotAvailable:
            print("  OK   read_image() raises the same clear error instead of crashing")
        except Exception as exc:
            print(f"  FAIL wrong exception type: {type(exc).__name__}: {exc}")
            overall = False

    print("\n--- intent matching ---\n")
    match_ok = 0
    for utterance, expected in MATCH_CASES:
        u = BRAIN.understand(utterance)
        good = u.skill == expected
        match_ok += good
        print(f"  {'OK  ' if good else 'MISS'} {utterance:38} -> {u.skill} ({u.score:.2f})")
    print(f"\n  {match_ok}/{len(MATCH_CASES)} correct")
    overall &= match_ok == len(MATCH_CASES)

    print("\n--- end to end via SESSION.handle ---\n")
    result = await SESSION.handle("read my screen", actor="text")
    if available:
        print(f"  {'OK  ' if result.ok else 'WARN'} -> {result.speech[:100]}")
    else:
        clean = (not result.ok) and (
            "tesseract" in result.speech.lower() or "install" in result.speech.lower()
        )
        print(f"  {'OK  ' if clean else 'MISS'} refused cleanly, no crash -> {result.speech[:100]}")
        overall &= clean

    print(f"\n{'ALL OK' if overall else 'FAILURES ABOVE'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
