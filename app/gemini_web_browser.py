#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Gemini Web fallback reusing the existing browser tab
#
# Reuses the EXISTING page already open in BrowserManager (never opens a new
# tab).  Injects cookies first; if the page still shows the Sign-in screen,
# falls back to manual email/password login via GEMINI_EMAIL / GEMINI_PASSWORD.
#
# Workflow:
#   1. Navigate the existing page to gemini.google.com
#   2. Inject cookies → reload → check if logged in
#   3. If not logged in and creds set → do manual login
#   4. Screenshot the reel → copy to system clipboard → Ctrl+V into Gemini input
#   5. Type prompt as ONE paragraph (no trailing filler text)
#   6. Submit → wait for response → screenshot → send to Telegram
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Optional, Tuple

log = logging.getLogger("GeminiWebBrowser")

GEMINI_URL       = "https://gemini.google.com/app"
GOOGLE_LOGIN_URL = "https://accounts.google.com/signin"


class GeminiWebBrowser:
    def __init__(self, cookies_raw: str = ""):
        self._cookies_raw = cookies_raw
        self._notifier    = None
        self._ctx         = None   # injected via set_context()
        self._page        = None   # injected via set_page()

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def set_context(self, ctx) -> None:
        """Inject the existing Playwright BrowserContext from BrowserManager."""
        self._ctx = ctx
        log.info("GeminiWebBrowser: BrowserContext injected.")

    def set_page(self, page) -> None:
        """Inject the existing Page so we never open a new tab."""
        self._page = page
        log.info("GeminiWebBrowser: existing Page injected.")

    # ── Cookie helpers ────────────────────────────────────────────────────────

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

    def _inject_cookies(self) -> int:
        cookies = self._parse_cookies()
        if cookies and self._ctx:
            self._ctx.add_cookies(cookies)
            log.info(f"Injected {len(cookies)} Gemini cookie(s).")
        return len(cookies)

    # ── Login state ───────────────────────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        """Return True if we're inside the Gemini chat UI (not on sign-in)."""
        try:
            url = self._page.url
            if "accounts.google.com" in url or "signin" in url.lower():
                return False
            # Sign-in button visible → not logged in
            sign_in = self._page.query_selector(
                "a[href*='accounts.google.com'], [aria-label*='Sign in'], a[href*='signin']"
            )
            if sign_in and sign_in.is_visible():
                return False
            # If the prompt input exists → logged in
            for sel in [
                "rich-textarea div[contenteditable='true']",
                "div[contenteditable='true'][role='textbox']",
                "textarea[placeholder]",
            ]:
                el = self._page.query_selector(sel)
                if el and el.is_visible():
                    return True
            return False
        except Exception:
            return False

    def _manual_login(self) -> bool:
        """Perform Google account login using GEMINI_EMAIL + GEMINI_PASSWORD."""
        try:
            from config import Config
            email    = Config.GEMINI_EMAIL.strip()
            password = Config.GEMINI_PASSWORD.strip()
        except Exception:
            email = password = ""

        if not email or not password:
            log.warning("No GEMINI_EMAIL/GEMINI_PASSWORD set — cannot manual login.")
            return False

        log.info("Attempting manual Gemini/Google login...")
        try:
            page = self._page
            page.goto(GOOGLE_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)

            page.wait_for_selector("input[type='email']", timeout=10_000)
            page.fill("input[type='email']", email)
            page.keyboard.press("Enter")
            time.sleep(2)

            page.wait_for_selector("input[type='password']", timeout=10_000)
            page.fill("input[type='password']", password)
            page.keyboard.press("Enter")
            time.sleep(4)

            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            if self._is_logged_in():
                log.info("Manual Gemini login succeeded ✅")
                return True
            else:
                log.error("Manual Gemini login failed — still not logged in.")
                return False

        except Exception as exc:
            log.error(f"Manual login error: {exc}")
            return False

    # ── Image clipboard paste ─────────────────────────────────────────────────

    def _put_image_on_clipboard(self, image_bytes: bytes, tmp_path: str) -> bool:
        """
        Write image_bytes to tmp_path and copy it to the system clipboard.
        Tries xclip (X11) then wl-copy (Wayland).
        Returns True if the clipboard command succeeded.
        """
        try:
            with open(tmp_path, "wb") as f:
                f.write(image_bytes)

            # xclip (X11)
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", tmp_path],
                capture_output=True,
            )
            if r.returncode == 0:
                log.info("Image copied to clipboard via xclip ✅")
                return True

            # wl-copy (Wayland)
            with open(tmp_path, "rb") as f:
                r2 = subprocess.run(
                    ["wl-copy", "--type", "image/png"],
                    input=f.read(),
                    capture_output=True,
                )
            if r2.returncode == 0:
                log.info("Image copied to clipboard via wl-copy ✅")
                return True

            log.warning("xclip and wl-copy both failed — clipboard paste unavailable.")
            return False
        except FileNotFoundError as exc:
            log.warning(f"Clipboard tool not found ({exc}) — falling back to file upload.")
            return False
        except Exception as exc:
            log.warning(f"_put_image_on_clipboard error: {exc}")
            return False

    def _paste_image_into_gemini(self, image_bytes: bytes) -> bool:
        """
        Copy reel screenshot to clipboard then Ctrl+V into the Gemini input.
        Falls back to the hidden file-input element if clipboard fails.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()

        try:
            clipboard_ok = self._put_image_on_clipboard(image_bytes, tmp_path)
            page = self._page

            if clipboard_ok:
                # Click the input area first, then paste
                for sel in [
                    "rich-textarea div[contenteditable='true']",
                    "div[contenteditable='true'][role='textbox']",
                    "textarea[placeholder]",
                    "div[contenteditable='true']",
                ]:
                    try:
                        el = page.wait_for_selector(sel, timeout=8_000)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.4)
                            page.keyboard.press("Control+v")
                            time.sleep(2)
                            log.info("Reel screenshot pasted via Ctrl+V ✅")
                            return True
                    except Exception:
                        continue

            # Fallback: file input upload
            log.info("Clipboard paste failed — trying file-input upload...")
            for sel in ["input[type='file']", "[data-testid='file-upload']"]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        el.set_input_files(tmp_path)
                        log.info(f"Image uploaded via file input ({sel}) ✅")
                        time.sleep(2)
                        return True
                except Exception:
                    continue

            log.warning("Could not paste or upload image to Gemini.")
            return False

        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # ── Main entry point ──────────────────────────────────────────────────────

    def ask(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        timeout_ms: int = 60_000,
    ) -> Tuple[Optional[str], Optional[bytes]]:
        """
        Use the EXISTING browser page (never opens a new tab) to query Gemini.

        Order of operations:
          1. Inject cookies, navigate to Gemini
          2. If not logged in → manual login
          3. Paste the reel screenshot from clipboard (image FIRST)
          4. Type the prompt as one clean paragraph
          5. Submit → wait for response → screenshot → Telegram
        """
        if self._ctx is None:
            log.error("No BrowserContext — call set_context() first.")
            return None, None

        # Resolve page: use injected page or first open page in context
        page = self._page
        if page is None:
            pages = self._ctx.pages
            if pages:
                page = pages[0]
                self._page = page
                log.info("GeminiWebBrowser: using first existing page in context.")
            else:
                log.error("No existing page found in context.")
                return None, None

        try:
            # ── 1. Inject cookies + navigate ─────────────────────────────────
            self._inject_cookies()
            log.info("Navigating to Gemini (existing tab)...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # ── 2. Login check ────────────────────────────────────────────────
            if not self._is_logged_in():
                log.warning("Not logged in to Gemini — trying manual login...")
                ok = self._manual_login()
                if not ok:
                    snap = page.screenshot(type="jpeg", quality=80)
                    self._send_screenshot(snap, "❌ Gemini: login failed — check GEMINI_EMAIL/PASSWORD")
                    return None, snap

            # ── 3. Paste reel screenshot FIRST ────────────────────────────────
            if image_bytes:
                log.info("Pasting reel screenshot into Gemini input...")
                self._paste_image_into_gemini(image_bytes)
                time.sleep(1)

            # ── 4. Type prompt as ONE clean paragraph ────────────────────────
            # Collapse all whitespace so it's a single paragraph; no trailing text
            clean_prompt = " ".join(prompt.split())[:4000]

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
                    if el and el.is_visible():
                        el.click()
                        time.sleep(0.4)
                        el.type(clean_prompt, delay=12)
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

            # ── 5. Submit ─────────────────────────────────────────────────────
            time.sleep(0.5)
            page.keyboard.press("Enter")
            log.info("Prompt submitted — waiting for Gemini response...")

            # ── 6. Wait for response ──────────────────────────────────────────
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
            log.info(f"Screenshot taken: {len(snap)//1024} KB")

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

    def _send_screenshot(self, screenshot_bytes: bytes, caption: str) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier.send_photo(screenshot_bytes, caption=caption)
            log.info("Gemini screenshot sent to Telegram.")
        except Exception as exc:
            log.warning(f"Could not send screenshot to Telegram: {exc}")
