#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Gemini Web fallback reusing the existing browser
#
# Uses the BrowserContext already open in BrowserManager — opens a new tab,
# goes to gemini.google.com, sends the prompt + image, captures the response,
# screenshots it, sends to Telegram, then closes the tab.
#
# NO new sync_playwright() call — that would crash inside the existing loop.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import time
from typing import Optional, Tuple

log = logging.getLogger("GeminiWebBrowser")

GEMINI_URL = "https://gemini.google.com/app"


class GeminiWebBrowser:
    def __init__(self, cookies_raw: str = ""):
        self._cookies_raw = cookies_raw
        self._notifier = None
        self._ctx = None  # injected via set_context()

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def set_context(self, ctx) -> None:
        """Inject the existing Playwright BrowserContext from BrowserManager."""
        self._ctx = ctx
        log.info("GeminiWebBrowser: BrowserContext injected.")

    def _parse_cookies(self) -> list:
        raw = self._cookies_raw.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        cookies = []
        for part in raw.replace(";", "\n").splitlines():
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies.append({
                    "name": k.strip(), "value": v.strip(),
                    "domain": ".google.com", "path": "/",
                    "httpOnly": False, "secure": True,
                })
        return cookies

    def ask(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        timeout_ms: int = 60_000,
    ) -> Tuple[Optional[str], Optional[bytes]]:
        """
        Open a new tab in the existing browser context, navigate to Gemini,
        type the prompt, capture the response + screenshot, send to Telegram,
        close the tab.  Returns (response_text, screenshot_bytes).
        """
        if self._ctx is None:
            log.error("No BrowserContext — call set_context() first.")
            return None, None

        page = None
        try:
            log.info("🌐 Opening Gemini tab in existing browser...")
            page = self._ctx.new_page()

            # Inject cookies
            cookies = self._parse_cookies()
            if cookies:
                self._ctx.add_cookies(cookies)
                log.info(f"Injected {len(cookies)} Gemini cookie(s).")

            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # Upload image if provided
            if image_bytes:
                import tempfile, os as _os
                tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                tmp.write(image_bytes)
                tmp.close()
                try:
                    for sel in ["input[type='file']", "[data-testid='file-upload']"]:
                        try:
                            el = page.query_selector(sel)
                            if el:
                                el.set_input_files(tmp.name)
                                log.info(f"Image uploaded via {sel}")
                                time.sleep(2)
                                break
                        except Exception:
                            continue
                finally:
                    try:
                        _os.unlink(tmp.name)
                    except Exception:
                        pass

            # Type the prompt
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
                        el.type(prompt[:4000], delay=15)
                        typed = True
                        log.info(f"Prompt typed via: {sel}")
                        break
                except Exception:
                    continue

            if not typed:
                log.error("Could not find Gemini prompt input.")
                snap = page.screenshot(type="jpeg", quality=80)
                self._send_screenshot(snap, "❌ Gemini Web: could not find prompt input")
                return None, snap

            time.sleep(0.5)
            page.keyboard.press("Enter")
            log.info("Prompt submitted — waiting for response...")

            # Wait for response text
            response_text = None
            for _ in range(30):
                time.sleep(1)
                for sel in [
                    "model-response", ".model-response-text",
                    "message-content", "[data-testid='response']",
                ]:
                    try:
                        els = page.query_selector_all(sel)
                        if els:
                            text = els[-1].inner_text()
                            if text and len(text.strip()) > 5:
                                response_text = text.strip()
                                break
                    except Exception:
                        continue
                if response_text:
                    break

            if response_text:
                log.info(f"Gemini response: {response_text[:200]!r}")
            else:
                log.warning("No response text extracted from Gemini.")

            time.sleep(1)
            snap = page.screenshot(type="jpeg", quality=85)
            log.info(f"Screenshot: {len(snap)//1024} KB")

            # Save to disk
            try:
                from config import Config
                out = Config.SCREENSHOT_DIR / f"gemini_web_{int(time.time())}.jpg"
                out.write_bytes(snap)
            except Exception as exc:
                log.warning(f"Could not save screenshot: {exc}")

            self._send_screenshot(
                snap,
                f"🌐 <b>Gemini Web</b>\n<code>{(response_text or 'No text')[:300]}</code>",
            )
            return response_text, snap

        except Exception as exc:
            log.error(f"GeminiWebBrowser.ask() failed: {type(exc).__name__}: {exc}")
            return None, None
        finally:
            if page:
                try:
                    page.close()
                    log.info("Gemini tab closed.")
                except Exception:
                    pass

    def _send_screenshot(self, screenshot_bytes: bytes, caption: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.send_photo(screenshot_bytes, caption=caption)
            log.info("Gemini screenshot sent to Telegram.")
        except Exception as exc:
            log.warning(f"Could not send screenshot to Telegram: {exc}")
