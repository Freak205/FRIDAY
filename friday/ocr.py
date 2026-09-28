"""Screen OCR — turn a screenshot into readable, locatable text.

Wraps pytesseract (a thin wrapper around the Tesseract OCR binary) around the
same Pillow screen capture `friday.skills.screen` already uses. Deliberately
small: no PyTorch, no vision model — Tesseract is a one-time ~50 MB install
that reads printed/UI text well enough for "what does my screen say" style
questions, and is the pixel-based fallback for content `ui.read` can't reach
(canvas-drawn UI, images, video, anything outside the accessibility tree).

The Tesseract *binary* is a separate install from the `pytesseract` package —
see `check_available()` for the actionable error raised when it's missing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday.log import get

log = get(__name__)

# Common install location for the UB-Mannheim Windows Tesseract build, used
# only as a fallback when the binary isn't on PATH and config doesn't name one.
_WINDOWS_DEFAULT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

_INSTALL_HINT = (
    "Tesseract OCR isn't installed (or isn't on PATH), so FRIDAY can't read "
    "screen text yet. Install it from "
    "https://github.com/UB-Mannheim/tesseract/wiki (Windows build), then "
    "either add its folder to PATH or set `ocr.tesseract_cmd` in config.yaml "
    "to the full path of tesseract.exe."
)


class OcrError(Exception):
    """Base class for OCR failures."""


class TesseractNotAvailable(OcrError):
    """The Tesseract binary (or the pytesseract package) can't be used."""


class ScreenCaptureFailed(OcrError):
    """The screenshot itself couldn't be taken."""


@dataclass(slots=True)
class OcrWord:
    """One recognized word plus where it sits on the captured image."""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float
    line: int = 0  # index of the OCR line this word belongs to, for phrase matching


@dataclass(slots=True)
class OcrResult:
    text: str
    words: list[OcrWord] = field(default_factory=list)
    width: int = 0
    height: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


_configured = False


def _configure() -> None:
    """Point pytesseract at a Tesseract binary if config or a default path names one."""
    global _configured
    if _configured:
        return
    _configured = True

    import pytesseract

    from friday.config import CFG

    cmd = CFG.ocr.tesseract_cmd
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
        return

    if shutil.which("tesseract"):
        return  # already resolvable, nothing to configure

    for candidate in _WINDOWS_DEFAULT_PATHS:
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            return


def check_available() -> str:
    """Raise TesseractNotAvailable with install instructions, else return its version."""
    try:
        import pytesseract
    except ImportError as exc:
        raise TesseractNotAvailable(
            "The pytesseract package isn't installed. Run: pip install pytesseract"
        ) from exc

    _configure()

    try:
        return str(pytesseract.get_tesseract_version())
    except Exception as exc:
        raise TesseractNotAvailable(_INSTALL_HINT) from exc


def capture_screen(region: str = "screen") -> Any:
    """Grab the current screen or active window as a PIL Image."""
    try:
        from PIL import ImageGrab

        bbox = None
        if region == "window":
            import win32gui

            hwnd = win32gui.GetForegroundWindow()
            bbox = win32gui.GetWindowRect(hwnd)

        image = ImageGrab.grab(bbox=bbox, all_screens=(region == "screen"))
    except Exception as exc:
        raise ScreenCaptureFailed(f"couldn't capture the screen: {exc}") from exc

    if image is None or image.width == 0 or image.height == 0:
        raise ScreenCaptureFailed("the screenshot came back empty")
    return image


def read_image(image: Any) -> OcrResult:
    """Run OCR on a PIL Image, returning full text plus per-word bounding boxes."""
    check_available()

    import pytesseract
    from pytesseract import Output

    from friday.config import CFG

    try:
        data = pytesseract.image_to_data(image, output_type=Output.DICT)
    except Exception as exc:
        raise OcrError(f"OCR failed: {exc}") from exc

    min_confidence = CFG.ocr.min_confidence
    words: list[OcrWord] = []
    lines: dict[tuple[int, int, int], list[str]] = {}
    line_index: dict[tuple[int, int, int], int] = {}

    for i, raw in enumerate(data["text"]):
        token = raw.strip()
        if not token:
            continue
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence < min_confidence:
            continue

        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in line_index:
            line_index[key] = len(line_index)

        words.append(
            OcrWord(
                text=token,
                left=int(data["left"][i]),
                top=int(data["top"][i]),
                width=int(data["width"][i]),
                height=int(data["height"][i]),
                confidence=confidence,
                line=line_index[key],
            )
        )
        lines.setdefault(key, []).append(token)

    text = "\n".join(" ".join(tokens) for tokens in lines.values())
    return OcrResult(text=text, words=words, width=image.width, height=image.height)


@dataclass(slots=True)
class TextMatch:
    """A phrase located on screen — a run of same-line OCR words matching a query."""

    text: str
    left: int
    top: int
    width: int
    height: int
    score: float

    @property
    def center(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2


def locate_text(result: OcrResult, query: str, min_score: int = 60) -> list[TextMatch]:
    """Find phrases on the OCR'd image whose text fuzzy-matches `query`.

    Tries every contiguous run of words on the same line (so a query spanning
    multiple words, e.g. "sign in", matches even though OCR emits one box per
    word), scores each run against the query, and returns the best matches
    ranked highest first.
    """
    from rapidfuzz import fuzz

    query_norm = query.strip().lower()
    if not query_norm or not result.words:
        return []

    by_line: dict[int, list[OcrWord]] = {}
    for w in result.words:
        by_line.setdefault(w.line, []).append(w)

    matches: list[TextMatch] = []
    for words in by_line.values():
        words = sorted(words, key=lambda w: w.left)
        n = len(words)
        for start in range(n):
            acc = ""
            left = words[start].left
            top = words[start].top
            right = words[start].left + words[start].width
            bottom = words[start].top + words[start].height
            for end in range(start, n):
                w = words[end]
                acc = f"{acc} {w.text}".strip()
                left = min(left, w.left)
                top = min(top, w.top)
                right = max(right, w.left + w.width)
                bottom = max(bottom, w.top + w.height)
                if len(acc) > len(query_norm) + 24:
                    break
                score = fuzz.WRatio(query_norm, acc.lower())
                if score >= min_score:
                    matches.append(
                        TextMatch(
                            text=acc, left=left, top=top,
                            width=right - left, height=bottom - top, score=score,
                        )
                    )

    matches.sort(key=lambda m: -m.score)
    return matches[:10]


def read_screen(region: str = "screen") -> OcrResult:
    """Capture the current screen (or active window) and OCR it."""
    image = capture_screen(region)
    return read_image(image)
