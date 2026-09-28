"""Web skills: search, fetch, download. No API keys, no paid services.

Search uses DuckDuckGo's HTML endpoint, which needs no key and no account.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus, urlparse

import httpx

from friday import paths
from friday.log import get
from friday.registry import SkillResult, skill

log = get(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_TIMEOUT = 20.0

# Elements that never contain readable page content.
_STRIP = "script, style, noscript, nav, header, footer, aside, form, iframe, svg"


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


@skill(
    name="web.search",
    tier="L0",
    description="Search the web and return the top results",
    examples=[
        "search the web for python tutorials",
        "google how to fix a flat tyre",
        "look up the weather in mumbai",
        "search for the latest news",
        "find information about quantum computing",
        "what does the internet say about this",
    ],
)
def search(
    query: Annotated[str, "what to search for"],
    count: Annotated[int, "how many results to return"] = 5,
) -> SkillResult:
    from selectolax.parser import HTMLParser

    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        response = httpx.post(
            url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True
        )
        response.raise_for_status()
    except Exception as exc:
        return SkillResult(speech=f"The search failed: {exc}", ok=False)

    tree = HTMLParser(response.text)
    results = []
    for node in tree.css("div.result")[: count * 2]:
        title_node = node.css_first("a.result__a")
        snippet_node = node.css_first("a.result__snippet") or node.css_first(".result__snippet")
        if not title_node:
            continue
        results.append({
            "title": title_node.text(strip=True),
            "url": title_node.attributes.get("href", ""),
            "snippet": snippet_node.text(strip=True) if snippet_node else "",
        })
        if len(results) >= count:
            break

    if not results:
        return SkillResult(speech=f"I found nothing for '{query}'.", ok=False)

    top = results[0]
    speech = f"Top result: {top['title']}."
    if top["snippet"]:
        speech += f" {top['snippet'][:200]}"

    return SkillResult(speech=speech, data={"query": query, "results": results})


@skill(
    name="web.fetch",
    tier="L0",
    description=(
        "Fetch a public http(s) web page and extract its readable text, in one call, "
        "with no browser and no login. Not for file:// URLs or local files — use "
        "browser.open + browser.read for those, or for anything needing login/JS/clicking"
    ),
    examples=[
        "read that web page",
        "fetch this url",
        "what does that page say",
        "get the content of this link",
        "open and read this article",
    ],
)
def fetch(
    url: Annotated[str, "the URL to fetch"],
    limit: Annotated[int, "maximum characters of text to return"] = 6000,
) -> SkillResult:
    from selectolax.parser import HTMLParser

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        response = httpx.get(
            url, headers={"User-Agent": _UA}, timeout=_TIMEOUT, follow_redirects=True
        )
        response.raise_for_status()
    except Exception as exc:
        return SkillResult(speech=f"I couldn't fetch that page: {exc}", ok=False)

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type:
        body = response.text[:limit]
        return SkillResult(
            speech=f"Fetched {len(body)} characters of {content_type}.",
            data={"url": url, "text": body, "content_type": content_type},
        )

    tree = HTMLParser(response.text)
    for node in tree.css(_STRIP):
        node.decompose()

    title = tree.css_first("title")
    title_text = title.text(strip=True) if title else urlparse(url).netloc

    body_node = tree.css_first("main") or tree.css_first("article") or tree.body
    text = _clean(body_node.text(separator="\n") if body_node else "")[:limit]

    return SkillResult(
        speech=f"{title_text} — {len(text)} characters of text.",
        data={"url": url, "title": title_text, "text": text},
    )


@skill(
    name="web.open",
    tier="L1",
    action="navigate",
    description="Open a URL in the default browser",
    examples=[
        "open youtube",
        "go to github dot com",
        "open that link in my browser",
        "take me to google",
        "browse to wikipedia",
    ],
)
def open_url(
    url: Annotated[str, "URL or site name to open"],
) -> SkillResult:
    import webbrowser

    target = url.strip()
    # "open youtube" -> a bare word, not a URL.
    if not target.startswith(("http://", "https://")):
        if "." not in target:
            target = f"https://{target.replace(' ', '')}.com"
        else:
            target = "https://" + target

    webbrowser.open(target)
    return SkillResult(speech=f"Opening {urlparse(target).netloc}.", data={"url": target})


@skill(
    name="web.download",
    tier="L1",
    action="modify",
    description="Download a file from a URL",
    examples=["download that file", "save this url to my downloads", "grab that file"],
)
def download(
    url: Annotated[str, "URL of the file to download"],
    filename: Annotated[str, "optional name to save it as"] = "",
) -> SkillResult:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    name = filename or Path(urlparse(url).path).name or "download.bin"
    destination = Path.home() / "Downloads" / name
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.stream(
            "GET", url, headers={"User-Agent": _UA},
            timeout=60.0, follow_redirects=True,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=65536):
                    handle.write(chunk)
    except Exception as exc:
        return SkillResult(speech=f"The download failed: {exc}", ok=False)

    size_mb = destination.stat().st_size / 1024**2
    return SkillResult(
        speech=f"Downloaded {name}, {size_mb:.1f} megabytes.",
        data={"path": str(destination), "size_mb": round(size_mb, 2)},
    )
