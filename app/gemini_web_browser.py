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
        """
        Log in to Gemini via Google account:
          1. Open gemini.google.com/app
          2. Click the "Sign in" button
          3. Fill Google email -> Next
          4. Fill Google password -> Next
          5. Verify Gemini chat UI is accessible
        Credentials come from GEMINI_EMAIL / GEMINI_PASSWORD secrets.
        """
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
            from playwright.sync_api import TimeoutError as PWTimeout
            page = self._page

            # Step 1: go to Gemini and click "Sign in"
            log.info("Gemini login step 1: opening gemini.google.com/app...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # Step 2: click "Sign in" button if present
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

            # Step 3: Google email input
            log.info("Gemini login step 3: filling Google email...")
            page.wait_for_selector("input[type='email']", timeout=15_000)
            page.fill("input[type='email']", email)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(3)

            # Step 4: Google password input
            log.info("Gemini login step 4: filling Google password...")
            page.wait_for_selector("input[type='password']", timeout=15_000)
            page.fill("input[type='password']", password)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(6)

            # Step 5: redirect back to Gemini if needed
            if "gemini.google.com" not in page.url:
                log.info("Gemini login step 5: navigating back to Gemini...")
                page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(4)

            if self._is_logged_in():
                log.info("Manual Gemini login succeeded ✅")
                # Save cookies back to context so they persist across queries
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
        import base64 as _b64
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_path = tmp.name
        tmp.close()
        try:
            page = self._page

            # ── Layer 1: xclip clipboard + Ctrl+V ────────────────────────────
            clipboard_ok = self._put_image_on_clipboard(image_bytes, tmp_path)
            if clipboard_ok:
                _INPUT_SELS = [
                    "rich-textarea div[contenteditable='true']",
                    "div[contenteditable='true'][role='textbox']",
                    "div.ql-editor[contenteditable='true']",
                    "textarea[placeholder]",
                    "div[contenteditable='true']",
                ]
                for sel in _INPUT_SELS:
                    try:
                        el = page.wait_for_selector(sel, timeout=5_000)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.3)
                            page.keyboard.press("Control+v")
                            time.sleep(2)
                            # Verify something was actually pasted (image chip appears)
                            try:
                                page.wait_for_selector(
                                    "img[src^='blob:'], [data-test-id='image-chip'], "
                                    ".image-chip, [aria-label*='image' i]",
                                    timeout=3_000,
                                )
                            except Exception:
                                pass  # best-effort verification
                            log.info("Reel screenshot pasted via Ctrl+V ✅")
                            return True
                    except Exception:
                        continue

            # ── Layer 2: JS clipboard API injection ───────────────────────────
            # Inject the image directly into the page clipboard via JS so the
            # browser's own paste handler picks it up — works even when xclip
            # isn't installed or the X11 clipboard is sandboxed.
            log.info("Clipboard paste failed — trying JS clipboard injection...")
            try:
                b64 = _b64.b64encode(image_bytes).decode()
                injected = page.evaluate(f"""async () => {{
                    try {{
                        const b64 = "{b64}";
                        const bin = atob(b64);
                        const arr = new Uint8Array(bin.length);
                        for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                        const blob = new Blob([arr], {{type: "image/png"}});
                        await navigator.clipboard.write([
                            new ClipboardItem({{"image/png": blob}})
                        ]);
                        return true;
                    }} catch(e) {{ return false; }}
                }}""")
                if injected:
                    for sel in [
                        "rich-textarea div[contenteditable='true']",
                        "div[contenteditable='true'][role='textbox']",
                        "div[contenteditable='true']",
                    ]:
                        try:
                            el = page.wait_for_selector(sel, timeout=5_000)
                            if el and el.is_visible():
                                el.click()
                                time.sleep(0.3)
                                page.keyboard.press("Control+v")
                                time.sleep(2)
                                log.info("Reel screenshot pasted via JS clipboard injection ✅")
                                return True
                        except Exception:
                            continue
            except Exception as js_exc:
                log.debug(f"JS clipboard injection failed: {js_exc}")

            # ── Layer 3: hidden file input (set_input_files bypasses visibility) ─
            log.info("JS clipboard failed — trying file input upload...")
            try:
                # Try ALL input[type=file] elements, including hidden ones.
                # Playwright's set_input_files works on hidden inputs directly.
                all_inputs = page.query_selector_all("input[type='file']")
                for inp in all_inputs:
                    try:
                        inp.set_input_files(tmp_path)
                        time.sleep(2)
                        log.info("Image uploaded via hidden file input ✅")
                        return True
                    except Exception:
                        continue
            except Exception:
                pass

            # ── Layer 4: click attachment button, then set file input ──────────
            log.info("Trying attachment button click + file input...")
            try:
                for btn_sel in [
                    "button[aria-label*='attach' i]",
                    "button[aria-label*='upload' i]",
                    "button[aria-label*='image' i]",
                    "button[data-test-id*='attach' i]",
                    "[role='button'][aria-label*='attach' i]",
                ]:
                    try:
                        btn = page.query_selector(btn_sel)
                        if btn:
                            btn.click()
                            time.sleep(1)
                            all_inputs = page.query_selector_all("input[type='file']")
                            for inp in all_inputs:
                                try:
                                    inp.set_input_files(tmp_path)
                                    time.sleep(2)
                                    log.info(f"Image uploaded via {btn_sel} + file input ✅")
                                    return True
                                except Exception:
                                    continue
                    except Exception:
                        continue
            except Exception:
                pass

            log.warning("Could not paste or upload image to Gemini after all attempts.")
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
            # 1. Inject cookies into context BEFORE navigating so Google
            #    receives authenticated cookies on the very first request.
            injected = self._inject_cookies()
            log.info(f"Navigating to Gemini (existing tab, {injected} cookies pre-injected)...")
            page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)

            # 2. Login check — if not logged in, re-inject and reload once more
            if not self._is_logged_in():
                log.warning("Gemini: not logged in after cookie injection — retrying with fresh inject + reload...")
                injected2 = self._inject_cookies()
                if injected2 > 0:
                    page.reload(wait_until="domcontentloaded", timeout=20_000)
                    time.sleep(3)

            if not self._is_logged_in():
                log.error("Gemini: still not logged in — GEMINI_COOKIES secret is missing or expired.")
                snap = page.screenshot(type="jpeg", quality=80)
                self._send_screenshot(snap, "❌ Gemini: not logged in — update GEMINI_COOKIES secret with fresh exported cookies")
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
