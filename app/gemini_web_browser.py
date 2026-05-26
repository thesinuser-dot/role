#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Gemini Web fallback reusing the existing browser tab
#
# Fix log:
#   • sameSite values from JSON exports (no_restriction, lax, etc.) are
#     sanitized to Playwright-legal values (Strict|Lax|None) before inject.
#   • Never opens a new tab — reuses the injected existing page.
#   • Cookie inject → login check → manual login fallback if needed.
#   • Pastes reel screenshot via clipboard (xclip/wl-copy) FIRST, then
#     types the prompt as one clean paragraph.
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
GOOGLE_LOGIN_URL = "https://accounts.google.com/signin/v2/identifier"

# Playwright only accepts these three sameSite values
_VALID_SAME_SITE = {"Strict", "Lax", "None"}


def _sanitize_cookie(c: dict, default_domain: str) -> dict:
    """
    Normalise a raw cookie dict so Playwright's add_cookies() won't crash.
    Fixes: sameSite, expires, domain, missing fields.
    """
    # sameSite: map any export value → Strict | Lax | None
    raw_ss = str(c.get("sameSite") or c.get("same_site") or "").lower().replace("_", "").replace("-", "")
    if raw_ss == "strict":
        same_site = "Strict"
    elif raw_ss == "lax":
        same_site = "Lax"
    else:
        same_site = "None"   # 'no_restriction', 'unspecified', empty → None

    # expires: must be a positive int; some exports use float or -1
    raw_exp = c.get("expirationDate") or c.get("expires") or 2147483647
    try:
        expires = int(float(raw_exp))
        if expires <= 0:
            expires = 2147483647
    except (TypeError, ValueError):
        expires = 2147483647

    # domain: must start with dot for host-cookies
    domain = str(c.get("domain") or default_domain)
    if domain and not domain.startswith(".") and not domain.startswith("http"):
        domain = "." + domain.lstrip(".")

    return {
        "name":     str(c.get("name", "")),
        "value":    str(c.get("value", "")),
        "domain":   domain,
        "path":     str(c.get("path") or "/"),
        "secure":   bool(c.get("secure", True)),
        "httpOnly": bool(c.get("httpOnly", False)),
        "sameSite": same_site,
        "expires":  expires,
    }


class GeminiWebBrowser:
    def __init__(self, cookies_raw: str = ""):
        self._cookies_raw = cookies_raw
        self._notifier    = None
        self._ctx         = None   # injected via set_context()
        self._page        = None   # injected via set_page()

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def set_context(self, ctx) -> None:
        self._ctx = ctx
        log.info("GeminiWebBrowser: BrowserContext injected.")

    def set_page(self, page) -> None:
        self._page = page
        log.info("GeminiWebBrowser: existing Page injected.")

    # ── Cookie helpers ────────────────────────────────────────────────────────

    def _parse_cookies(self) -> list:
        raw = self._cookies_raw.strip()
        if not raw:
            return []
        cookies = []
        # Try JSON array first
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for c in parsed:
                    if c.get("name"):
                        cookies.append(_sanitize_cookie(c, ".google.com"))
                if cookies:
                    return cookies
        except (json.JSONDecodeError, ValueError):
            pass

        # Fallback: semicolon-separated key=value string
        for part in raw.replace(";", "\n").splitlines():
            part = part.strip()
            if "=" in part:
                k, _, v = part.partition("=")
                cookies.append(_sanitize_cookie({
                    "name": k.strip(), "value": v.strip(),
                    "domain": ".google.com",
                }, ".google.com"))
        return cookies

    def _inject_cookies(self) -> int:
        cookies = self._parse_cookies()
        if not cookies or not self._ctx:
            return 0
        try:
            self._ctx.add_cookies(cookies)
            log.info(f"Injected {len(cookies)} Gemini cookie(s).")
            return len(cookies)
        except Exception as exc:
            log.warning(f"Cookie injection error: {exc} — skipping cookies.")
            return 0

    # ── Login detection ───────────────────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        try:
            url = self._page.url or ""
            if "accounts.google.com" in url:
                return False
            # Sign-in button present = not logged in
            for sel in [
                "a[href*='accounts.google.com/signin']",
                "a[aria-label*='Sign in']",
                "a[data-action='sign in']",
                ".sign-in-button",
            ]:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        return False
                except Exception:
                    pass
            # Prompt input present = logged in
            for sel in [
                "rich-textarea div[contenteditable='true']",
                "div[contenteditable='true'][role='textbox']",
                "textarea[placeholder]",
            ]:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        return True
                except Exception:
                    pass
            # If none found, assume not logged in
            return False
        except Exception:
            return False

    def _manual_login(self) -> bool:
        try:
            from config import Config
            email    = Config.GEMINI_EMAIL.strip()
            password = Config.GEMINI_PASSWORD.strip()
        except Exception:
            email = password = ""

        if not email or not password:
            log.warning("GEMINI_EMAIL/GEMINI_PASSWORD not set — cannot manual login.")
            return False

        log.info("Attempting manual Google/Gemini login...")
        try:
            page = self._page
            page.goto(GOOGLE_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)

            # Email step
            page.wait_for_selector("input[type='email']", timeout=12_000)
            page.fill("input[type='email']", email)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(2)

            # Password step
            page.wait_for_selector("input[type='password']", timeout=12_000)
            page.fill("input[type='password']", password)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(5)

            # Navigate to Gemini
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(4)

            if self._is_logged_in():
                log.info("Manual Gemini login succeeded ✅")
                return True
            log.error("Manual Gemini login failed — still not showing chat UI.")
            return False
        except Exception as exc:
            log.error(f"Manual login error: {type(exc).__name__}: {exc}")
            return False

    # ── Image clipboard paste ─────────────────────────────────────────────────

    def _put_image_on_clipboard(self, image_bytes: bytes, tmp_path: str) -> bool:
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        # Try xclip (X11)
        try:
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", tmp_path],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                log.info("Image on clipboard via xclip ✅")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Try wl-copy (Wayland)
        try:
            with open(tmp_path, "rb") as f:
                r2 = subprocess.run(
                    ["wl-copy", "--type", "image/png"],
                    input=f.read(), capture_output=True, timeout=5,
                )
            if r2.returncode == 0:
                log.info("Image on clipboard via wl-copy ✅")
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        log.warning("xclip and wl-copy both unavailable.")
        return False

    def _paste_image_into_gemini(self, image_bytes: bytes) -> bool:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            clipboard_ok = self._put_image_on_clipboard(image_bytes, tmp_path)
            page = self._page

            if clipboard_ok:
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

            # Fallback: file input
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
        if self._ctx is None:
            log.error("No BrowserContext — call set_context() first.")
            return None, None

        # Resolve page
        page = self._page
        if page is None:
            pages = self._ctx.pages
            if pages:
                page = pages[0]
                self._page = page
                log.info("Using first existing page in context.")
            else:
                log.error("No existing page found in context.")
                return None, None

        try:
            # 1. Inject cookies + navigate
            self._inject_cookies()
            log.info("Navigating to Gemini (existing tab)...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # 2. Login check → manual login if needed
            if not self._is_logged_in():
                log.warning("Gemini: not logged in — attempting manual login...")
                if not self._manual_login():
                    snap = page.screenshot(type="jpeg", quality=80)
                    self._send_screenshot(snap, "❌ Gemini: login failed — set GEMINI_EMAIL + GEMINI_PASSWORD secrets")
                    return None, snap

            # 3. Paste reel screenshot FIRST
            if image_bytes:
                log.info("Pasting reel screenshot into Gemini...")
                self._paste_image_into_gemini(image_bytes)
                time.sleep(1)

            # 4. Type prompt as ONE clean paragraph
            clean_prompt = " ".join(prompt.split())[:4000]

            typed = False
            for sel in [
                "rich-textarea div[contenteditable='true']",
                "div[contenteditable='true'][role='textbox']",
                "div.ql-editor[contenteditable='true']",
                "textarea[placeholder]",
                "div[contenteditable='true']",
            ]:
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
                self._send_screenshot(snap, "❌ Gemini Web: prompt input not found")
                return None, snap

            # 5. Submit
            time.sleep(0.5)
            page.keyboard.press("Enter")
            log.info("Prompt submitted — waiting for response...")

            # 6. Wait for response
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
        except Exception as exc:
            log.warning(f"Could not send screenshot: {exc}")
