#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Gemini Web fallback (browser-based, no API key)
#
# How it works:
#   1. Injects Google cookies exported from a real Chrome session.
#   2. Navigates to gemini.google.com/app.
#   3. Checks login status — if not logged in, aborts with a clear message.
#      (Password login is intentionally NOT attempted: Google hard-blocks
#       automated browsers from the OAuth sign-in flow with the
#       "Couldn't sign you in" screen.  Only cookies work.)
#   4. Pastes the reel screenshot via file-chooser or clipboard.
#   5. Types the prompt and waits for the response.
#
# Cookie setup:
#   - Export cookies from a logged-in Chrome tab on gemini.google.com
#     using the Cookie-Editor extension (Export → JSON).
#   - Paste the JSON array as the GEMINI_COOKIES GitHub secret.
#   - The write_google_cookies.py script normalises this into
#     ~/.secrets/gemini_cookies.json at workflow start.
#   - Pass the file path OR the raw JSON string as cookies_raw.
#
# Required cookies:  __Secure-1PSID, __Secure-3PSID, SAPISID, SID
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger("GeminiWebBrowser")

GEMINI_URL = "https://gemini.google.com/app"

# Playwright only accepts these three sameSite values
_VALID_SAME_SITE = {"Strict", "Lax", "None"}

# These must be present for Gemini to authenticate
_REQUIRED_COOKIES = {"__Secure-1PSID", "SAPISID", "SID"}

# URL fragments that mean we are NOT on the chat UI
_GATE_URL_PATTERNS = (
    "accounts.google.com",
    "consent.google.com",
    "ogs.google.com/widget",
    "myaccount.google.com",
    "challenge",
    "reauth",
    "signin",
    "checkconnection",
    "disallowed_useragent",
    "errorCode=disallowed",
    "oauth2/v2/auth",
)

# Page text fragments that indicate Google blocked the browser from signing in
_BLOCKED_TEXT_PATTERNS = (
    "couldn't sign you in",
    "couldn\u2019t sign you in",
    "this browser or app may not be secure",
)

_SAME_SITE_MAP = {
    "no_restriction": "None",
    "norestriction":  "None",
    "lax":            "Lax",
    "strict":         "Strict",
    "none":           "None",
    "unspecified":    "None",
    "":               "None",
}


# ─────────────────────────────────────────────────────────────────────────────
# Cookie helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize_cookie(c: dict, domain: str = ".google.com") -> Optional[dict]:
    """
    Alias used by agent.py — accepts an optional domain override and
    delegates to _normalise_cookie.
    """
    if domain and not c.get("domain"):
        c = {**c, "domain": domain}
    return _normalise_cookie(c)


def _normalise_cookie(c: dict) -> Optional[dict]:
    """
    Convert a raw cookie dict (from JSON export or file) into a
    Playwright-compatible dict.  Returns None if the cookie has no name.

    Domain is NEVER mutated — Google auth is extremely sensitive to exact
    domain values (host-only vs subdomain-wildcard).
    """
    name  = str(c.get("name",  "")).strip()
    value = str(c.get("value", ""))
    if not name:
        return None

    domain = str(c.get("domain", ".google.com"))
    if domain.startswith("http"):
        from urllib.parse import urlparse
        domain = urlparse(domain).hostname or ".google.com"

    ss_raw   = str(c.get("sameSite") or c.get("same_site") or "").lower()\
                 .replace("_", "").replace("-", "").replace(" ", "")
    same_site = _SAME_SITE_MAP.get(ss_raw, "None")

    raw_exp = c.get("expirationDate") or c.get("expires")
    if raw_exp is None:
        expires = -1
    else:
        try:
            expires = int(float(raw_exp))
            if expires <= 0:
                expires = -1
        except (TypeError, ValueError):
            expires = -1

    entry: dict = {
        "name":     name,
        "value":    value,
        "domain":   domain,
        "path":     str(c.get("path") or "/"),
        "secure":   bool(c.get("secure", True)),
        "httpOnly": bool(c.get("httpOnly", False)),
        "sameSite": same_site,
    }
    if expires > 0:
        entry["expires"] = expires
    return entry


def _parse_cookies_raw(raw: str) -> list:
    """
    Parse a cookie string into a list of Playwright-compatible dicts.
    Accepts JSON array or semicolon-separated key=value string.
    """
    raw = raw.strip()
    if not raw:
        return []

    # Try JSON array first
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            result = []
            for c in parsed:
                entry = _normalise_cookie(c)
                if entry:
                    result.append(entry)
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: semicolon key=value
    result = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        entry = _normalise_cookie({"name": k.strip(), "value": v.strip(), "domain": ".google.com"})
        if entry:
            result.append(entry)
    return result


def _load_cookies_from_file(path: str) -> list:
    """Load cookies from a JSON file written by write_google_cookies.py."""
    try:
        data = json.loads(Path(path).read_text())
        if isinstance(data, list):
            result = []
            for c in data:
                entry = _normalise_cookie(c)
                if entry:
                    result.append(entry)
            return result
    except Exception as exc:
        log.warning(f"Could not load cookies from {path}: {exc}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────────────────────

class GeminiWebBrowser:
    """
    Uses an existing Playwright BrowserContext + Page to query Gemini Web.
    Call set_context() and set_page() before ask().
    """

    def __init__(self, cookies_raw: str = ""):
        self._cookies_raw = cookies_raw
        self._notifier    = None
        self._ctx         = None
        self._page        = None

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def set_context(self, ctx) -> None:
        self._ctx = ctx
        log.info("GeminiWebBrowser: BrowserContext injected.")

    def set_page(self, page) -> None:
        self._page = page
        log.info("GeminiWebBrowser: Page injected.")

    # ── Cookies ───────────────────────────────────────────────────────────────

    def _get_cookies(self) -> list:
        """
        Return the best available cookie list, in priority order:
          1. Normalised file at ~/.secrets/gemini_cookies.json
          2. Raw string passed to __init__ (GEMINI_COOKIES env var)
        """
        file_path = os.path.expanduser("~/.secrets/gemini_cookies.json")
        if os.path.exists(file_path):
            cookies = _load_cookies_from_file(file_path)
            if cookies:
                log.info(f"Loaded {len(cookies)} cookies from {file_path}")
                return cookies
            log.warning(f"Cookie file exists but is empty/invalid: {file_path}")

        if self._cookies_raw.strip():
            cookies = _parse_cookies_raw(self._cookies_raw)
            if cookies:
                log.info(f"Parsed {len(cookies)} cookies from GEMINI_COOKIES env var.")
                return cookies

        return []

    def _inject_cookies(self) -> int:
        """Inject cookies into the browser context. Returns number injected."""
        if not self._ctx:
            return 0

        cookies = self._get_cookies()
        if not cookies:
            log.warning(
                "No Gemini cookies available. "
                "Export cookies from gemini.google.com using Cookie-Editor → Export → JSON "
                "and set as GEMINI_COOKIES secret."
            )
            return 0

        # Warn if critical cookies are missing
        present = {c["name"] for c in cookies}
        missing = _REQUIRED_COOKIES - present
        if missing:
            log.warning(
                f"Missing critical Google auth cookies: {sorted(missing)}. "
                "Gemini will likely redirect to login. "
                "Re-export cookies from a fresh Chrome session on gemini.google.com."
            )

        try:
            self._ctx.add_cookies(cookies)
            log.info(f"Injected {len(cookies)} Google cookie(s).")
            return len(cookies)
        except Exception as exc:
            log.warning(f"Cookie injection failed: {exc}")
            return 0

    # ── Login detection ───────────────────────────────────────────────────────

    def _is_blocked_browser(self) -> bool:
        """
        Return True if Google has shown the hard-blocked browser screen:
        "Couldn't sign you in — This browser or app may not be secure."
        When this happens, NO amount of retrying or credential entry helps.
        The only fix is to set fresh cookies from a real Chrome session.
        """
        try:
            url     = self._page.url or ""
            content = self._page.content().lower()
            if any(p in url for p in ("disallowed_useragent", "errorCode=disallowed")):
                return True
            if any(t in content for t in _BLOCKED_TEXT_PATTERNS):
                return True
        except Exception:
            pass
        return False

    def _is_logged_in(self) -> bool:
        """Return True only when the Gemini chat input is visible."""
        try:
            url = self._page.url or ""

            # Any known gate/auth URL = not logged in
            if any(p in url for p in _GATE_URL_PATTERNS):
                log.debug(f"Login check: gate URL detected in {url}")
                return False

            # Sign-in button visible = not logged in
            for sel in [
                "a[href*='accounts.google.com/signin']",
                "a[aria-label*='Sign in']",
                "button:has-text('Sign in')",
                "a:has-text('Sign in')",
                ".sign-in-button",
            ]:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        return False
                except Exception:
                    pass

            # Chat input present = logged in
            for sel in [
                "rich-textarea div[contenteditable='true']",
                "div[contenteditable='true'][role='textbox']",
                "ms-autosize-textarea textarea",
            ]:
                try:
                    el = self._page.query_selector(sel)
                    if el and el.is_visible():
                        return True
                except Exception:
                    pass

            log.debug(f"Login check uncertain — no chat input found on {url}")
            return False
        except Exception:
            return False

    # ── Image upload ──────────────────────────────────────────────────────────

    _INPUT_SELS = [
        "rich-textarea div[contenteditable='true']",
        "div[contenteditable='true'][role='textbox']",
        "div.ql-editor[contenteditable='true']",
        "ms-autosize-textarea textarea",
        "textarea.input-area",
        "textarea[placeholder]",
        "div[contenteditable='true'][data-placeholder]",
        "p[data-placeholder]",
        "div[contenteditable='true']",
    ]

    _ATTACH_BTN_SELS = [
        "button[aria-label*='Upload' i]",
        "button[aria-label*='Add image' i]",
        "button[aria-label*='attach' i]",
        "button[aria-label*='image' i]",
        "button[data-test-id*='attach' i]",
        "[role='button'][aria-label*='Upload' i]",
        "[role='button'][aria-label*='attach' i]",
        "button[jsname][aria-label*='add' i]",
    ]

    def _put_image_on_clipboard(self, image_bytes: bytes, tmp_path: str) -> bool:
        with open(tmp_path, "wb") as f:
            f.write(image_bytes)
        for cmd in (
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", tmp_path],
            None,  # wl-copy handled separately below
        ):
            if cmd:
                try:
                    r = subprocess.run(cmd, capture_output=True, timeout=5)
                    if r.returncode == 0:
                        log.info("Image on clipboard via xclip ✅")
                        return True
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
            else:
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

    def _close_any_open_dialog(self) -> None:
        try:
            page = self._page
            for sel in [
                "button[aria-label='Close']", "button[aria-label='Cancel']",
                "[role='dialog'] button", "mat-dialog-container button",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        time.sleep(0.5)
                        break
                except Exception:
                    pass
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass

    def _paste_image_into_gemini(self, image_bytes: bytes) -> bool:
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            with open(tmp_path, "wb") as f:
                f.write(image_bytes)
            page = self._page

            # Try attach button + file chooser
            for btn_sel in self._ATTACH_BTN_SELS:
                try:
                    btn = page.query_selector(btn_sel)
                    if not btn or not btn.is_visible():
                        continue
                    try:
                        with page.expect_file_chooser(timeout=4_000) as fc_info:
                            btn.click()
                        fc_info.value.set_files(tmp_path)
                        time.sleep(2)
                        log.info(f"Image uploaded via file chooser ({btn_sel}) ✅")
                        return True
                    except Exception:
                        time.sleep(0.5)
                        for inp in page.query_selector_all("input[type='file']"):
                            try:
                                inp.set_input_files(tmp_path)
                                time.sleep(2)
                                log.info(f"Image uploaded via hidden file input ✅")
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue

            # Try all hidden file inputs directly
            for inp in page.query_selector_all("input[type='file']"):
                try:
                    inp.set_input_files(tmp_path)
                    time.sleep(2)
                    log.info("Image uploaded via hidden file input ✅")
                    return True
                except Exception:
                    continue

            # Clipboard paste fallback
            if self._put_image_on_clipboard(image_bytes, tmp_path):
                for sel in self._INPUT_SELS:
                    try:
                        el = page.wait_for_selector(sel, timeout=4_000)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.3)
                            page.keyboard.press("Control+v")
                            time.sleep(2)
                            log.info("Image pasted via clipboard Ctrl+V ✅")
                            return True
                    except Exception:
                        continue

            log.warning("Image upload failed — sending text-only prompt.")
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
            injected = self._inject_cookies()
            log.info(f"Navigating to Gemini ({injected} cookies pre-injected)...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # Check for the hard-blocked browser screen FIRST
            if self._is_blocked_browser():
                log.error(
                    "Google blocked this browser from signing in "
                    "(\"Couldn't sign you in — This browser or app may not be secure\"). "
                    "Password login will NOT work. "
                    "FIX: Export cookies from a logged-in Chrome session on "
                    "gemini.google.com using Cookie-Editor → Export → JSON, "
                    "then set as the GEMINI_COOKIES GitHub secret and re-run."
                )
                snap = page.screenshot(type="jpeg", quality=80)
                self._send_screenshot(
                    snap,
                    "❌ Gemini: Google blocked sign-in.\n\n"
                    "FIX: Export cookies from Chrome on gemini.google.com "
                    "(Cookie-Editor → Export → JSON) and set as GEMINI_COOKIES secret.",
                )
                return None, snap

            if not self._is_logged_in():
                log.error(
                    "Gemini cookies did not authenticate. "
                    "Re-export cookies from a fresh Chrome session on gemini.google.com "
                    "and update the GEMINI_COOKIES secret."
                )
                snap = page.screenshot(type="jpeg", quality=80)
                self._send_screenshot(
                    snap,
                    "❌ Gemini: not logged in after cookie injection.\n\n"
                    "Re-export cookies from Chrome on gemini.google.com "
                    "(Cookie-Editor → Export → JSON) and update GEMINI_COOKIES secret.",
                )
                return None, snap

            log.info("Gemini login confirmed ✅")

            if image_bytes:
                log.info("Uploading reel screenshot to Gemini...")
                self._paste_image_into_gemini(image_bytes)
                self._close_any_open_dialog()
                time.sleep(1)

            clean_prompt = " ".join(prompt.split())[:4000]

            typed = False
            for sel in self._INPUT_SELS:
                try:
                    el = page.wait_for_selector(sel, timeout=8_000)
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

            time.sleep(0.5)
            page.keyboard.press("Enter")
            log.info("Prompt submitted — waiting for response...")

            response_text = None
            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                time.sleep(1)
                for sel in [
                    "model-response .markdown",
                    "model-response",
                    "message-content .markdown",
                    "message-content",
                    ".response-content",
                    ".model-response-text",
                    "[data-testid='response']",
                    "ms-cmark-node",
                    ".gemini-response-text",
                ]:
                    try:
                        els = page.query_selector_all(sel)
                        if els:
                            text = els[-1].inner_text()
                            if text and len(text.strip()) > 2:
                                response_text = text.strip()
                                break
                    except Exception:
                        continue
                if response_text:
                    break

            if response_text:
                log.info(f"Gemini response ({len(response_text)} chars): {response_text[:200]!r}")
            else:
                log.warning("No response text extracted from Gemini.")

            time.sleep(1)
            snap = page.screenshot(type="jpeg", quality=85)

            try:
                from config import Config
                out = Config.SCREENSHOT_DIR / f"gemini_web_{int(time.time())}.jpg"
                out.write_bytes(snap)
            except Exception as exc:
                log.debug(f"Could not save screenshot: {exc}")

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
