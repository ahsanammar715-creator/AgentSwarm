"""Design mode: real screenshots + a vision critique, so 'looks great!' has to be earned."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from .llm import LLM

CRITIQUE_PROMPT = """You are a strict UI/UX reviewer. You are looking at a screenshot of a web page
rendered at {width}x{height} ({label}). The page is meant to: {intent}

Critique what you actually SEE. Cover: layout and alignment, overflow/clipping, spacing rhythm,
typography hierarchy, contrast/legibility, broken or missing images, responsive problems for this
viewport, and anything that looks unfinished. List concrete, fixable problems in priority order
(max 8). If something is genuinely fine, say so briefly. Never say it looks great without evidence."""


def take_screenshot(target: str, workdir: Path, out_dir: Path, width: int, height: int) -> Path:
    """target: http(s) URL, or a path to an HTML file inside the workdir."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError as e:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "design mode needs Playwright: pip install playwright && playwright install chromium"
        ) from e
    if not target.startswith(("http://", "https://", "file://")):
        p = (workdir / target).resolve()
        if not p.is_file():
            raise RuntimeError(f"no such file: {target}")
        target = p.as_uri()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"shot-{int(time.time() * 1000)}-{width}x{height}.png"
    launch: dict = {}
    if os.environ.get("PLAYWRIGHT_CHROMIUM_PATH"):
        launch["executable_path"] = os.environ["PLAYWRIGHT_CHROMIUM_PATH"]
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(target, wait_until="networkidle", timeout=30000)
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    return out


def critique(vision: LLM, image: Path, width: int, height: int, label: str, intent: str) -> str:
    b64 = base64.b64encode(image.read_bytes()).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": CRITIQUE_PROMPT.format(width=width, height=height, label=label, intent=intent)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }
    ]
    return vision.chat(messages).get("content") or "(no critique returned)"
