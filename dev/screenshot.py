"""
Take dashboard screenshots for the README using Playwright.

Prereqs:
    pip install playwright
    playwright install chromium

Run:
    python dev/dev_server.py &        # in another terminal
    python dev/screenshot.py          # writes docs/dashboard-*.png

Outputs (relative to repo root):
    docs/dashboard-light.png
    docs/dashboard-dark.png
    docs/dashboard-mobile.png
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL     = os.getenv("URL", "http://127.0.0.1:8089/")
OUT_DIR = Path(__file__).resolve().parent.parent / "docs"
OUT_DIR.mkdir(exist_ok=True)


def shoot(page, path: Path) -> None:
    page.goto(URL, wait_until="networkidle")
    # Wait until the header pill stops saying "connecting…" (i.e. first
    # /api/panels response has rendered).
    page.wait_for_function(
        "document.querySelector('#conn .label').textContent.trim() !== 'connecting…'",
        timeout=5000,
    )
    # Disable transitions so the screenshot is deterministic.
    page.add_style_tag(content="* { transition: none !important; animation: none !important; }")
    page.screenshot(path=str(path), full_page=True)
    print(f"  wrote {path.relative_to(OUT_DIR.parent)}")


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()

        for scheme, name in (("light", "dashboard-light.png"),
                             ("dark",  "dashboard-dark.png")):
            ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                color_scheme=scheme,
                device_scale_factor=2,
            )
            shoot(ctx.new_page(), OUT_DIR / name)
            ctx.close()

        # Phone viewport
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            color_scheme="dark",
            device_scale_factor=3,
            is_mobile=True,
        )
        shoot(ctx.new_page(), OUT_DIR / "dashboard-mobile.png")
        ctx.close()

        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
