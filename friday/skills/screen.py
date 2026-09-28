"""Screen skills: capture and inspect what's on the display."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from friday import paths
from friday.registry import SkillResult, skill
from friday.intent import CLICK_RULE


@skill(
    name="screen.capture",
    tier="L0",
    description="Take a screenshot of the screen or the active window",
    examples=[
        "take a screenshot",
        "capture the screen",
        "screenshot this",
        "grab a picture of my screen",
        "screenshot the active window",
    ],
)
def capture(
    region: Annotated[str, "'screen' for everything, 'window' for the active window"] = "screen",
    save: Annotated[bool, "write the image to data/cache"] = True,
) -> SkillResult:
    from PIL import ImageGrab

    bbox = None
    title = ""

    if region == "window":
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        bbox = (left, top, right, bottom)

    image = ImageGrab.grab(bbox=bbox, all_screens=(region == "screen"))

    data = {"width": image.width, "height": image.height, "region": region}
    if title:
        data["window"] = title

    if save:
        paths.ensure()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = paths.CACHE / f"screenshot-{stamp}.png"
        image.save(out, "PNG")
        data["path"] = str(out)

    where = f" of {title[:40]}" if title else ""
    return SkillResult(speech=f"Screenshot taken{where}.", data=data)


@skill(
    name="screen.active_window",
    tier="L0",
    description="Report which window is currently in the foreground",
    examples=[
        "what am I looking at",
        "what's the active window",
        "what window is focused",
        "what's on my screen right now",
        "what am I doing",
    ],
)
def active_window() -> SkillResult:
    import psutil
    import win32gui
    import win32process

    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc = psutil.Process(pid).name()
    except Exception:
        proc = ""

    if not title:
        return SkillResult(speech="Nothing seems to be focused right now.")

    return SkillResult(
        speech=f"You're in {title[:80]}.",
        data={"title": title, "process": proc},
    )


@skill(
    name="screen.read_text",
    tier="L0",
    description=(
        "Capture the screen or active window and read the text on it with OCR "
        "(works on canvas-drawn content that ui.read can't reach)"
    ),
    examples=[
        "read my screen",
        "what text is on my screen",
        "read what's currently open",
        "what does my screen say",
        "ocr my screen",
        "extract the text from my screen",
        "scan my screen for text",
        "can you read what's on the screen",
    ],
)
def read_text(
    region: Annotated[str, "'screen' for everything, 'window' for the active window"] = "screen",
) -> SkillResult:
    from friday import ocr

    try:
        result = ocr.read_screen(region)
    except ocr.TesseractNotAvailable as exc:
        return SkillResult(speech=str(exc), ok=False)
    except ocr.ScreenCaptureFailed as exc:
        return SkillResult(speech=f"I couldn't capture the screen: {exc}", ok=False)
    except ocr.OcrError as exc:
        return SkillResult(speech=f"OCR failed: {exc}", ok=False)

    if result.is_empty:
        return SkillResult(
            speech="I don't see any readable text on your screen.",
            data={"text": "", "words": []},
        )

    snippet = result.text.strip()
    speech = snippet if len(snippet) <= 400 else snippet[:400].rstrip() + "…"

    return SkillResult(
        speech=speech,
        data={
            "text": result.text,
            "words": [
                {
                    "text": w.text, "left": w.left, "top": w.top,
                    "width": w.width, "height": w.height, "confidence": w.confidence,
                }
                for w in result.words
            ],
            "width": result.width,
            "height": result.height,
        },
    )


@skill(
    name="screen.find_text",
    tier="L0",
    description=(
        "Locate text on the screen and report where it is, without clicking it — "
        "the bridge between what OCR sees and screen coordinates"
    ),
    examples=[
        "find the word settings on my screen",
        "where is the login button on my screen",
        "locate the text sign in on the screen",
        "is the word done visible on my screen",
        "find sign up on my screen",
    ],
)
def find_text(
    query: Annotated[str, "the text to look for on screen"],
    region: Annotated[str, "'screen' for everything, 'window' for the active window"] = "screen",
) -> SkillResult:
    from friday import ocr

    try:
        result = ocr.read_screen(region)
    except ocr.TesseractNotAvailable as exc:
        return SkillResult(speech=str(exc), ok=False)
    except ocr.ScreenCaptureFailed as exc:
        return SkillResult(speech=f"I couldn't capture the screen: {exc}", ok=False)
    except ocr.OcrError as exc:
        return SkillResult(speech=f"OCR failed: {exc}", ok=False)

    matches = ocr.locate_text(result, query)
    if not matches:
        return SkillResult(speech=f"I don't see '{query}' on your screen.", ok=False)

    best = matches[0]
    return SkillResult(
        speech=f"Found '{best.text}' at ({best.center[0]}, {best.center[1]}).",
        data={
            "matches": [
                {
                    "text": m.text, "left": m.left, "top": m.top,
                    "width": m.width, "height": m.height,
                    "center": list(m.center), "score": m.score,
                }
                for m in matches
            ]
        },
    )


@skill(
    name="screen.observe",
    tier="L0",
    description=(
        "Take a structured, read-only snapshot of the current desktop state — "
        "active window, open windows, screen size, visible text, and browser "
        "state when relevant — the basis for 'what's on my screen' style questions"
    ),
    examples=[
        "what's on my screen",
        "look at what's on my screen",
        "look at this error and tell me what it says",
        "give me a full picture of my screen",
        "take stock of my desktop",
        "what's going on with my computer right now",
        "check my screen before we continue",
        "give me a snapshot of my desktop",
        "understand what I'm currently doing on screen",
    ],
)
async def observe() -> SkillResult:
    from friday import desktop_observer
    from friday.config import CFG

    if not CFG.desktop_observer.enabled:
        return SkillResult(speech="Desktop observation is disabled in configuration.", ok=False)

    obs = await desktop_observer.observe()
    return SkillResult(speech=obs.summary(), data=obs.to_dict())


@skill(
    name="screen.click_text",
    tier="L1",
    action=CLICK_RULE,
    description=(
        "Find text on the screen with OCR and click it — for canvas-drawn or "
        "otherwise non-accessible UI that ui.click can't reach by control name"
    ),
    examples=[
        "click on the text sign in on my screen",
        "click where it says next on the screen",
        "tap the ok label on my screen",
        "click the word continue on screen",
        "click wherever it says submit on my screen",
    ],
    dry_run=lambda query, region="screen": f"Find '{query}' on screen with OCR and click it",
)
def click_text(
    query: Annotated[str, "the text to click"],
    region: Annotated[str, "'screen' for everything, 'window' for the active window"] = "screen",
) -> SkillResult:
    from friday import ocr, winput

    try:
        result = ocr.read_screen(region)
    except ocr.TesseractNotAvailable as exc:
        return SkillResult(speech=str(exc), ok=False)
    except ocr.ScreenCaptureFailed as exc:
        return SkillResult(speech=f"I couldn't capture the screen: {exc}", ok=False)
    except ocr.OcrError as exc:
        return SkillResult(speech=f"OCR failed: {exc}", ok=False)

    matches = ocr.locate_text(result, query)
    if not matches:
        return SkillResult(speech=f"I don't see '{query}' on your screen.", ok=False)

    best = matches[0]
    cx, cy = best.center
    if region == "window":
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        left, top, _, _ = win32gui.GetWindowRect(hwnd)
        cx, cy = left + cx, top + cy

    winput.click(cx, cy)
    return SkillResult(
        speech=f"Clicked '{best.text}'.",
        data={"clicked": best.text, "x": cx, "y": cy, "score": best.score},
    )
