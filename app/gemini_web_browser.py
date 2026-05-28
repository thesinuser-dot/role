#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# gemini_web_browser.py — Gemini Web fallback reusing the existing browser tab
#
# Fixes applied (2026-05):
#   • NEVER mutate host-only cookie domains — preserve exact domain from export
#   • Validate required Google auth cookies before attempting login
#   • Expanded login-failure detection (consent/ogs/myaccount/reauth/challenge)
#   • sameSite values sanitised without touching domain field
#   • Expired cookies stay expired — no forced 2147483647 revival
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

# These cookies must be present for Gemini to authenticate reliably
_REQUIRED_GEMINI_COOKIES = {"__Secure-1PSID", "SAPISID", "SID"}

# URL patterns that indicate Google is NOT showing the chat UI
_NOT_LOGGED_IN_URLS = (
    "accounts.google.com",
    "consent.google.com",
    "ogs.google.com/widget",
    "myaccount.google.com",
    "challenge",
    "reauth",
    "signin",
    "checkconnection",
)

# URL patterns that mean Google has hard-blocked this browser/app from signing
# in — retrying or entering a password will never work.  Only fresh cookies
# exported from a real Chrome session will fix this.
_BLOCKED_BROWSER_URLS = (
    "disallowed_useragent",
    "errorCode=disallowed",
    "oauth2/v2/auth",           # OAuth redirect that ends in the blocked screen
)
_BLOCKED_BROWSER_TEXT = (
    "couldn't sign you in",
    "this browser or app may not be secure",
    "couldn\u2019t sign you in",   # curly apostrophe variant
)


def _sanitize_cookie(c: dict, default_domain: str) -> dict:
    """
    Normalise a raw cookie dict so Playwright's add_cookies() won't crash.

    KEY FIX: Domain is preserved exactly as exported.
    Prepending a dot to host-only cookies (e.g. accounts.google.com →
    .accounts.google.com) changes browser behaviour and breaks Google auth.
    We only add a leading dot when the original domain is genuinely a
    subdomain wildcard (already starts with dot) OR when no domain is given.
    """
    # sameSite: map any export value → Strict | Lax | None
    raw_ss = str(c.get("sameSite") or c.get("same_site") or "").lower().replace("_", "").replace("-", "")
    if raw_ss == "strict":
        same_site = "Strict"
    elif raw_ss == "lax":
        same_site = "Lax"
    else:
        same_site = "None"  # 'no_restriction', 'unspecified', empty → None

    # expires: must be a positive int; keep -1/None as session cookie
    raw_exp = c.get("expirationDate") or c.get("expires")
    try:
        expires = int(float(raw_exp)) if raw_exp is not None else -1
        # Don't force-revive expired cookies — keep them as-is so Google
        # sees the real expiry instead of a bogus far-future timestamp
        if expires <= 0:
            expires = -1
    except (TypeError, ValueError):
        expires = -1

    # ── KEY FIX: preserve domain exactly; never force-add a dot prefix ──
    domain = str(c.get("domain") or default_domain)
    # Only strip "http(s)://" prefixes — never add/remove dots
    if domain.startswith("http"):
        from urllib.parse import urlparse
        domain = urlparse(domain).hostname or default_domain

    cookie: dict = {
        "name":     str(c.get("name", "")),
        "value":    str(c.get("value", "")),
        "domain":   domain,
        "path":     str(c.get("path") or "/"),
        "secure":   bool(c.get("secure", True)),
        "httpOnly": bool(c.get("httpOnly", False)),
        "sameSite": same_site,
    }
    # Only include 'expires' if it's a valid positive timestamp
    if expires > 0:
        cookie["expires"] = expires

    return cookie


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

    def _validate_required_cookies(self, cookies: list) -> bool:
        """
        Check that the critical Google auth cookies are present.
        Gemini silently redirects to login if any of these are missing.
        """
        present = {c["name"] for c in cookies}
        missing = _REQUIRED_GEMINI_COOKIES - present
        if missing:
            log.warning(
                f"Gemini cookies missing critical auth cookies: {missing}. "
                "Login will likely fail. Export cookies from a fresh logged-in "
                "Chrome session and ensure __Secure-1PSID, SAPISID, and SID are included."
            )
            return False
        return True

    def _inject_cookies(self) -> int:
        cookies = self._parse_cookies()
        if not cookies or not self._ctx:
            return 0

        # Warn if critical cookies are missing (but still try — partial is better than nothing)
        self._validate_required_cookies(cookies)

        try:
            self._ctx.add_cookies(cookies)
            log.info(f"Injected {len(cookies)} Gemini cookie(s).")
            return len(cookies)
        except Exception as exc:
            log.warning(f"Cookie injection error: {exc} — skipping cookies.")
            return 0

    # ── Login detection ───────────────────────────────────────────────────────

    def _is_logged_in(self) -> bool:
        """
        Check whether the current page is the Gemini chat UI.

        KEY FIX: Google routes through many non-login URLs before showing
        the chat. We check for ANY known gate/challenge URL, not just
        accounts.google.com.
        """
        try:
            current_url = self._page.url or ""

            # Any known Google auth/challenge URL = not logged in
            for pattern in _NOT_LOGGED_IN_URLS:
                if pattern in current_url:
                    log.debug(f"Login check: gate URL detected ({pattern}) in {current_url}")
                    return False

            # If a sign-in button is explicitly visible, not logged in
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

            # Verify Gemini chat input is actually present
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

            # No chat input found but no gate detected — uncertain; treat as not logged in
            log.debug(f"Login check: no chat input found on {current_url}")
            return False
        except Exception:
            return False

    def _manual_login(self) -> bool:
        """
        Attempt to log in to Gemini via Google account credentials.

        Google blocks automated browsers from the OAuth sign-in flow with the
        "Couldn't sign you in — This browser or app may not be secure" screen.
        When that screen is detected we abort immediately and instruct the user
        to export cookies from a real Chrome session instead of wasting time
        retrying a login that will never succeed.

        Password-based login is kept as a last resort for cases where Google
        hasn't yet blocked this UA, but the recommended path is always cookies.
        """
        page = self._page

        # Check if we've already hit the hard-blocked screen
        try:
            current_url  = page.url or ""
            page_content = page.content().lower()

            url_blocked  = any(p in current_url for p in _BLOCKED_BROWSER_URLS)
            text_blocked = any(t in page_content for t in _BLOCKED_BROWSER_TEXT)

            if url_blocked or text_blocked:
                log.error(
                    "Google blocked this browser from signing in "
                    "('Couldn't sign you in — This browser or app may not be secure'). "
                    "Password login will NOT work from an automated browser. "
                    "FIX: export cookies from a real logged-in Chrome session on "
                    "gemini.google.com using the Cookie-Editor extension, paste the "
                    "JSON array as the GEMINI_COOKIES GitHub secret, then re-run."
                )
                return False
        except Exception:
            pass

        # Fall back to password login (works only if Google hasn't blocked UA)
        try:
            from config import Config
            email    = Config.GEMINI_EMAIL.strip()
            password = Config.GEMINI_PASSWORD.strip()
        except Exception:
            email = password = ""

        if not email or not password:
            log.warning(
                "GEMINI_EMAIL/GEMINI_PASSWORD not set — cannot attempt password login. "
                "Set the GEMINI_COOKIES secret with cookies exported from a real Chrome "
                "session on gemini.google.com to authenticate without a password."
            )
            return False

        log.info("Attempting manual Google/Gemini login (password fallback)...")
        try:
            from playwright.sync_api import TimeoutError as PWTimeout

            log.info("Gemini login step 1: opening gemini.google.com/app...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # Abort immediately if we hit the blocked-browser screen
            try:
                content = page.content().lower()
                if any(t in content for t in _BLOCKED_BROWSER_TEXT) or \
                   any(p in (page.url or "") for p in _BLOCKED_BROWSER_URLS):
                    log.error(
                        "Blocked-browser screen appeared after navigation. "
                        "Password login cannot proceed — update GEMINI_COOKIES instead."
                    )
                    return False
            except Exception:
                pass

            log.info("Gemini login step 2: clicking Sign in...")
            for sel in [
                "a:has-text('Sign in')",
                "button:has-text('Sign in')",
                "[href*='accounts.google.com/signin']",
            ]:
                try:
                    page.click(sel, timeout=6_000)
                    break
                except PWTimeout:
                    continue
            time.sleep(2)

            # Check again after clicking Sign in
            try:
                content = page.content().lower()
                if any(t in content for t in _BLOCKED_BROWSER_TEXT):
                    log.error(
                        "Blocked-browser screen appeared after clicking Sign in. "
                        "Update GEMINI_COOKIES secret with fresh Chrome cookies."
                    )
                    return False
            except Exception:
                pass

            log.info("Gemini login step 3: filling Google email...")
            page.wait_for_selector("input[type='email']", timeout=15_000)
            page.fill("input[type='email']", email)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(3)

            log.info("Gemini login step 4: filling Google password...")
            page.wait_for_selector("input[type='password']", timeout=15_000)
            page.fill("input[type='password']", password)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(6)

            if "gemini.google.com" not in page.url:
                log.info("Gemini login step 5: navigating back to Gemini...")
                page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(4)

            if self._is_logged_in():
                log.info("Manual Gemini login succeeded ✅")
                try:
                    cookies = page.context.cookies()
                    log.info(f"Gemini: captured {len(cookies)} cookies after login.")
                except Exception:
                    pass
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

    def _close_any_open_dialog(self) -> None:
        try:
            page = self._page
            for sel in ["button[aria-label='Close']", "button[aria-label='Cancel']",
                        "[role='dialog'] button", "mat-dialog-container button"]:
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
                                log.info(f"Image uploaded via {btn_sel} + file input ✅")
                                return True
                            except Exception:
                                continue
                except Exception:
                    continue

            for inp in page.query_selector_all("input[type='file']"):
                try:
                    inp.set_input_files(tmp_path)
                    time.sleep(2)
                    log.info("Image uploaded via hidden file input ✅")
                    return True
                except Exception:
                    continue

            if self._put_image_on_clipboard(image_bytes, tmp_path):
                for sel in self._INPUT_SELS:
                    try:
                        el = page.wait_for_selector(sel, timeout=4_000)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.3)
                            page.keyboard.press("Control+v")
                            time.sleep(2)
                            log.info("Image pasted via xclip Ctrl+V ✅")
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
            log.info(f"Navigating to Gemini (existing tab, {injected} cookies pre-injected)...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            if not self._is_logged_in():
                log.warning("Gemini: cookies didn't authenticate — trying manual login...")
                if not self._manual_login():
                    log.error("Gemini: manual login also failed — cannot proceed.")
                    snap = page.screenshot(type="jpeg", quality=80)
                    self._send_screenshot(snap, "❌ Gemini: login failed.\n\nFIX: Export cookies from a logged-in Chrome session on gemini.google.com using the Cookie-Editor extension, paste the JSON array as the GEMINI_COOKIES GitHub secret, then re-run.\n\nPassword login does NOT work from automated browsers — Google blocks it.")
                    return None, snap

            if image_bytes:
                log.info("Pasting reel screenshot into Gemini...")
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
            for _ in range(30):
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
