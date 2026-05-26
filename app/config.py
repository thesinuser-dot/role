#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Visible Playwright browser for Gemini Web fallback
#
# Triggered automatically when GEMINI_API_KEY is not set (or quota-exhausted).
# Opens a VISIBLE Chromium window (headless=False) so the user can see it,
# navigates to gemini.google.com, types the prompt, takes a screenshot of the
# response, then sends the screenshot to Telegram.
#
# The text response is also returned so vision.py / ai_router.py can use it.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import time
import json
import base64
from io import BytesIO
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger("GeminiWebBrowser")


class GeminiWebBrowser:
    """
    Opens a visible Chromium window, goes to gemini.google.com,
    injects cookies (if provided), types the prompt, waits for the response,
    takes a screenshot and returns the response text + screenshot bytes.

    headless is always False — the user can watch it work.
    """

    GEMINI_URL = "https://gemini.google.com/app"

    def __init__(self, cookies_raw: str = ""):
        from config import Config
        self._cookies_raw = cookies_raw
        self._notifier = None  # injected by caller if needed
        self._screenshot_dir = Config.SCREENSHOT_DIR

    def set_notifier(self, notifier) -> None:
        """Inject the Telegram notifier so screenshots can be forwarded."""
        self._notifier = notifier

    def _parse_cookies(self) -> list[dict]:
        """
        Parse GEMINI_COOKIES — supports:
          - JSON array of {name, value, domain, ...} dicts
          - Semicolon-separated key=value pairs (converted to .google.com cookies)
        """
        raw = self._cookies_raw.strip()
        if not raw:
            return []
        # Try JSON array first
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        # Fall back to semicolon-separated key=value string
        cookies = []
        for part in raw.replace(";", "\n").splitlines():
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies.append({
                    "name": k.strip(),
                    "value": v.strip(),
                    "domain": ".google.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                })
        return cookies

    def ask(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        timeout_ms: int = 60_000,
    ) -> tuple[Optional[str], Optional[bytes]]:
        """
        Open a visible Gemini browser, send the prompt (optionally with an
        image), wait for the response, capture a screenshot, send it to
        Telegram, and return (response_text, screenshot_bytes).

        Returns (None, None) on failure.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error("playwright not installed — Gemini Web Browser unavailable.")
            return None, None

        from config import Config

        log.info("🌐 Opening VISIBLE Gemini browser (headless=False)...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=False,  # always visible
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                    ],
                )
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )

                # Inject cookies if provided
                cookies = self._parse_cookies()
                if cookies:
                    ctx.add_cookies(cookies)
                    log.info(f"Injected {len(cookies)} Gemini cookie(s).")

                page = ctx.new_page()
                log.info(f"Navigating to {self.GEMINI_URL} ...")
                page.goto(self.GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(3)

                # ── Upload image if provided ───────────────────────────────
                if image_bytes:
                    # Save image to temp file for upload
                    import tempfile
                    import os
                    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                    tmp.write(image_bytes)
                    tmp.close()
                    tmp_path = tmp.name

                    try:
                        # Try to find file upload button
                        upload_selectors = [
                            "input[type='file']",
                            "[data-testid='file-upload']",
                            "button[aria-label*='upload']",
                            "button[aria-label*='image']",
                            ".file-upload-button",
                        ]
                        for sel in upload_selectors:
                            try:
                                el = page.query_selector(sel)
                                if el:
                                    el.set_input_files(tmp_path)
                                    log.info(f"Image uploaded via selector: {sel}")
                                    time.sleep(2)
                                    break
                            except Exception:
                                continue
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass

                # ── Type the prompt ────────────────────────────────────────
                input_selectors = [
                    "rich-textarea div[contenteditable='true']",
                    "div[contenteditable='true'][role='textbox']",
                    "div.ql-editor[contenteditable='true']",
                    "textarea[placeholder]",
                    "div[contenteditable='true']",
                ]
                typed = False
                for sel in input_selectors:
                    try:
                        el = page.wait_for_selector(sel, timeout=10_000)
                        if el:
                            el.click()
                            time.sleep(0.5)
                            el.type(prompt[:4000], delay=20)
                            typed = True
                            log.info(f"Prompt typed via selector: {sel}")
                            break
                    except Exception:
                        continue

                if not typed:
                    log.error("Could not find Gemini prompt input field.")
                    snap = page.screenshot(type="jpeg", quality=80)
                    browser.close()
                    self._send_screenshot(snap, "❌ Gemini Web: could not find prompt input")
                    return None, snap

                # ── Submit ─────────────────────────────────────────────────
                time.sleep(0.5)
                page.keyboard.press("Enter")
                log.info("Prompt submitted — waiting for response...")

                # Wait for response to appear
                response_selectors = [
                    "model-response",
                    ".model-response-text",
                    "[data-testid='response']",
                    ".response-container",
                    "message-content",
                ]
                response_text = None
                for _ in range(30):  # wait up to ~30s
                    time.sleep(1)
                    for sel in response_selectors:
                        try:
                            els = page.query_selector_all(sel)
                            if els:
                                last = els[-1]
                                text = last.inner_text()
                                if text and len(text.strip()) > 10:
                                    response_text = text.strip()
                                    break
                        except Exception:
                            continue
                    if response_text:
                        break

                if response_text:
                    log.info(f"Gemini Web response: {response_text[:200]!r}...")
                else:
                    log.warning("Could not extract Gemini Web response text.")

                # ── Screenshot ────────────────────────────────────────────
                time.sleep(1)
                snap = page.screenshot(type="jpeg", quality=85)
                log.info(f"Gemini Web screenshot captured ({len(snap)//1024} KB)")

                # Save to disk
                try:
                    ts = int(time.time())
                    out_path = self._screenshot_dir / f"gemini_web_{ts}.jpg"
                    out_path.write_bytes(snap)
                    log.info(f"Screenshot saved: {out_path}")
                except Exception as exc:
                    log.warning(f"Could not save Gemini screenshot: {exc}")

                # ── Send screenshot to Telegram ───────────────────────────
                caption = (
                    f"🌐 <b>Gemini Web Response</b>\n"
                    f"<code>{response_text[:300] if response_text else 'No text extracted'}</code>"
                )
                self._send_screenshot(snap, caption)

                browser.close()
                return response_text, snap

        except Exception as exc:
            log.error(f"GeminiWebBrowser.ask() failed: {type(exc).__name__}: {exc}")
            return None, None

    def _send_screenshot(self, screenshot_bytes: bytes, caption: str) -> None:
        """Forward screenshot to Telegram if notifier is available."""
        if self._notifier is None:
            return
        try:
            self._notifier.send_photo(screenshot_bytes, caption=caption)
            log.info("Gemini Web screenshot sent to Telegram.")
        except Exception as exc:
            log.warning(f"Could not send Gemini Web screenshot to Telegram: {exc}")
