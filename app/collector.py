#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# collector.py — Instagram Reel URL collection and metrics extraction
#
# Key improvement: SelectorRegistry
#   Instagram's DOM structure changes frequently.  Hard-coded single selectors
#   break silently and the agent collects nothing.  SelectorRegistry holds a
#   priority-ordered list for each DOM target; the first selector that returns
#   a non-empty result wins.  When all selectors fail the failure is logged at
#   WARNING level with the full selector list so it's easy to update.
#
# All exception paths log at least at DEBUG level — no more silent swallows.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

from browser import BrowserManager
from config import Config


# ─────────────────────────────────────────────────────────────────────────────
# Selector Registry
# ─────────────────────────────────────────────────────────────────────────────

class SelectorRegistry:
    """
    Priority-ordered CSS / text selector lists for every DOM target we query.

    Each list is tried in order; the first selector that returns ≥1 element
    wins.  If all selectors fail, the caller receives an empty list and logs
    a WARNING with the full tried list so the fix is obvious.

    Strategy: most-specific selectors (aria-label, data-* attributes) first;
    broad tag-only selectors last.  Broad selectors are included as a last
    resort so the agent degrades gracefully rather than silently collecting
    nothing.
    """

    VIEW_COUNT: List[str] = [
        "[aria-label*='views' i]",
        "[aria-label*='view' i]",
        "span[class*='view' i]",
        "div[class*='view' i]",
        "span[class*='View']",
        "section span[class]",
        "main span[class]",
        "article span[class]",
        "span",              # broad fallback — filtered by text content in caller
    ]

    LIKE_COUNT: List[str] = [
        "button[aria-label*='like' i]:not([aria-label*='unlike' i]) span",
        "[aria-label*='like' i]:not([aria-label*='unlike' i])",
        "button[aria-label*='like' i] span",
        "span[class*='like' i]",
        "div[class*='like' i]",
        "section span[class]",
        "span",              # broad fallback
    ]

    VIDEO_ELEMENT: List[str] = [
        "video[src]",
        "video[class*='reel' i]",
        "video[playsinline]",
        "main video",
        "article video",
        "video",
    ]

    POPUP_DISMISS: List[str] = [
        "button:has-text('Not Now')",
        "button:has-text('Not now')",
        "button:has-text('Allow essential and optional cookies')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('OK')",
        "button:has-text('Continue')",
        "button:has-text('I Agree')",
        "[aria-label='Close']",
        "[aria-label='Dismiss']",
        "[aria-label='close' i]",
    ]

    REEL_LINKS: List[str] = [
        "a[href*='/reel/']",
        "a[href*='/reels/']",
        "a[href*='/p/']",
        "a[href*='instagram.com/reel']",
        "a[href*='instagram.com/p/']",
    ]

    @classmethod
    def query_all(cls, page: Page, selector_list: List[str], label: str) -> list:
        """
        Try each selector in order; return elements from the first that matches.
        Logs WARNING if all selectors fail.
        """
        log = logging.getLogger("SelectorRegistry")
        tried = []
        for sel in selector_list:
            tried.append(sel)
            try:
                els = page.query_selector_all(sel)
                if els:
                    if tried[:-1]:  # needed a fallback
                        log.debug(
                            f"[{label}] Primary selectors failed; matched on: {sel!r}"
                        )
                    return els
            except PlaywrightError as exc:
                log.debug(f"[{label}] Selector {sel!r} raised PlaywrightError: {exc}")
            except Exception as exc:
                log.debug(f"[{label}] Selector {sel!r} raised unexpected error: {exc}")
        log.warning(
            f"[{label}] All {len(tried)} selectors failed — DOM may have changed. "
            f"Tried: {tried}"
        )
        return []


# ─────────────────────────────────────────────────────────────────────────────
# ReelCollector
# ─────────────────────────────────────────────────────────────────────────────

class ReelCollector:
    def __init__(self, browser: BrowserManager):
        self.log = logging.getLogger("ReelCollector")
        self._bm = browser

    @property
    def _page(self) -> Page:
        return self._bm.page

    # ── Utilities ─────────────────────────────────────────────────────────────

    # Known non-ID path segments Instagram uses under /reels/
    _GARBAGE_SEGMENTS = frozenset({"audio", "trending", "explore", "saved", "liked"})
    # Valid reel IDs are Base64url — at least 8 chars
    _REEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,}$")

    @classmethod
    def extract_reel_id(cls, url: str) -> Optional[str]:
        for pattern in (r"/reels?/([A-Za-z0-9_-]+)", r"/p/([A-Za-z0-9_-]+)"):
            m = re.search(pattern, url)
            if m:
                candidate = m.group(1)
                if (
                    candidate not in cls._GARBAGE_SEGMENTS
                    and cls._REEL_ID_RE.match(candidate)
                ):
                    return candidate
        return None

    @staticmethod
    def _is_valid_reel_url(url: str) -> bool:
        """Accept only canonical /reels/{id}/ or /p/{id}/ URLs."""
        return bool(re.search(r"instagram\.com/(?:reels?|p)/[A-Za-z0-9_-]{8,}", url))

    @staticmethod
    def _parse_count(text: str) -> int:
        if not text:
            return 0
        text = text.strip().upper().replace(",", "").replace(" ", "")
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        for suffix, mult in multipliers.items():
            if text.endswith(suffix):
                num_str = re.sub(r"[^\d.]", "", text[:-1])
                try:
                    return int(float(num_str) * mult)
                except ValueError:
                    return 0
        num_str = re.sub(r"[^\d]", "", text)
        return int(num_str) if num_str else 0

    # ── Popup dismissal ───────────────────────────────────────────────────────

    def dismiss_popups(self) -> None:
        for sel in SelectorRegistry.POPUP_DISMISS:
            try:
                btn = self._page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click(timeout=2000)
                    self.log.debug(f"Dismissed popup: {sel}")
                    self._bm.delay(200, 500)
            except PlaywrightTimeout as exc:
                self.log.debug(f"Popup dismiss timeout for {sel!r}: {exc}")
            except PlaywrightError as exc:
                self.log.debug(f"Popup dismiss Playwright error for {sel!r}: {exc}")
            except Exception as exc:
                self.log.debug(f"Popup dismiss unexpected error for {sel!r}: {exc}")

    # ── Metrics extraction ────────────────────────────────────────────────────

    def extract_metrics(self) -> Dict[str, int]:
        # Give Instagram's lazy-loaded engagement section time to render
        self._bm.delay(1500, 2500)

        views = self._extract_views_js()
        if views == 0:
            # JS traversal failed — fall back to CSS selector approach
            self.log.debug("JS view extraction got 0 — trying CSS fallback")
            views = self._extract_count(
                SelectorRegistry.VIEW_COUNT,
                keyword="view",
                exclude=None,
                label="views",
            )

        likes = self._extract_count(
            SelectorRegistry.LIKE_COUNT,
            keyword="like",
            exclude="unlike",
            label="likes",
        )
        self.log.info(f"Metrics: views={views:,}  likes={likes:,}")
        return {"views": views, "likes": likes}

    def _extract_views_js(self) -> int:
        """
        Walk the DOM text nodes via JavaScript to find the view count.

        Instagram renders the number and the 'views' label in separate sibling
        spans, so CSS-selector-based approaches that require both to be in the
        same element always return 0.  Walking text nodes and looking for a
        numeric value adjacent to a 'views' label is far more robust.
        """
        try:
            raw = self._page.evaluate("""
                () => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    const texts = [];
                    let node;
                    while ((node = walker.nextNode())) {
                        const t = node.textContent.trim();
                        if (t) texts.push(t);
                    }

                    // Pattern 1: single text node like "1.2M views" or "12,345 views"
                    for (const t of texts) {
                        const m = /^([\d,]+\.?\d*\s*[KMBkmb]?)\s+views?$/i.exec(t);
                        if (m) return m[1];
                    }

                    // Pattern 2: number in one text node, "views" in the next 1-3
                    for (let i = 0; i < texts.length - 1; i++) {
                        if (/^views?$/i.test(texts[i + 1]) ||
                            (texts[i + 2] && /^views?$/i.test(texts[i + 2])) ||
                            (texts[i + 3] && /^views?$/i.test(texts[i + 3]))) {
                            const m = /^([\d,]+\.?\d*\s*[KMBkmb]?)$/.exec(texts[i]);
                            if (m) return m[1];
                        }
                    }

                    // Pattern 3: aria-label on any element containing "X views"
                    const ariaEls = document.querySelectorAll('[aria-label]');
                    for (const el of ariaEls) {
                        const label = el.getAttribute('aria-label') || '';
                        const m = /([\d,]+\.?\d*\s*[KMBkmb]?)\s+views?/i.exec(label);
                        if (m) return m[1];
                    }

                    return null;
                }
            """)
            if raw:
                val = self._parse_count(str(raw))
                self.log.debug(f"JS view extraction found: {raw!r} → {val:,}")
                return val
        except Exception as exc:
            self.log.debug(f"JS view extraction error: {exc}")
        return 0

    def _extract_count(
        self,
        selector_list: List[str],
        keyword: str,
        exclude: Optional[str],
        label: str,
    ) -> int:
        best = 0
        tried_selectors = 0
        for sel in selector_list:
            tried_selectors += 1
            try:
                elements = self._page.query_selector_all(sel)
                if not elements:
                    continue
                for el in elements[:80]:
                    try:
                        aria  = el.get_attribute("aria-label") or ""
                        inner = el.inner_text() or ""
                        combined = (aria + " " + inner).lower()
                        if keyword not in combined:
                            continue
                        if exclude and exclude in combined:
                            continue
                        match = re.search(r"([\d,]+\.?\d*\s*[kmb]?)", combined, re.IGNORECASE)
                        if match:
                            val = self._parse_count(match.group(1))
                            if 0 < val < 2_000_000_000 and val > best:
                                best = val
                    except PlaywrightError as exc:
                        self.log.debug(f"[{label}] Element read PlaywrightError: {exc}")
                    except Exception as exc:
                        self.log.debug(f"[{label}] Element read unexpected error: {exc}")
                if best:
                    break  # found a value — stop trying selectors
            except PlaywrightError as exc:
                self.log.debug(f"[{label}] Selector {sel!r} PlaywrightError: {exc}")
            except Exception as exc:
                self.log.debug(f"[{label}] Selector {sel!r} unexpected error: {exc}")

        if best == 0:
            self.log.debug(
                f"[{label}] Count not found after {tried_selectors} selector strategies"
            )
        return best

    # ── Screenshot ────────────────────────────────────────────────────────────

    def capture_reel_screenshot(self, reel_id: str) -> Optional[bytes]:
        try:
            video_el = self._page.query_selector("video")
            if video_el and video_el.is_visible():
                raw = video_el.screenshot(type="jpeg", quality=90)
            else:
                self.log.debug(f"[{reel_id}] No visible <video> — falling back to viewport screenshot")
                raw = self._page.screenshot(
                    type="jpeg", quality=90,
                    clip={"x": 0, "y": 0, "width": Config.VIEWPORT_W, "height": Config.VIEWPORT_H},
                )
            path = Config.SCREENSHOT_DIR / f"{reel_id}_{int(time.time())}.jpg"
            path.write_bytes(raw)
            self.log.debug(f"Screenshot saved: {path} ({len(raw)//1024} KB)")
            return raw
        except PlaywrightError as exc:
            self.log.error(f"[{reel_id}] Screenshot PlaywrightError: {exc}")
            return None
        except OSError as exc:
            self.log.error(f"[{reel_id}] Screenshot write failed: {exc}")
            return None
        except Exception as exc:
            self.log.error(f"[{reel_id}] Screenshot unexpected error: {exc}")
            return None

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate_to_reels_feed(self, notifier=None) -> bool:
        self.log.info(f"Navigating to {Config.INSTAGRAM_REELS_URL}")
        try:
            self._page.goto(
                Config.INSTAGRAM_REELS_URL, wait_until="domcontentloaded", timeout=30_000
            )
            self._bm.delay(2500, 4500)
            url = self._page.url
            self.log.info(f"Landed on: {url}")

            # Debug snapshot
            if notifier:
                try:
                    snap = self._page.screenshot(type="jpeg", quality=70)
                    page_title = self._page.title()
                    notifier.send_debug(
                        f"🌐 <b>Landed on:</b> <code>{url}</code>\n"
                        f"📄 <b>Page title:</b> {page_title}",
                        snap,
                    )
                except Exception as exc:
                    self.log.debug(f"Landing snapshot failed: {exc}")

            if "accounts/login" in url or "/login" in url:
                self.log.error("Redirected to login page — session cookies are invalid/expired!")
                if notifier:
                    notifier.send_message(
                        "<b>Reels Hunter — Session Expired</b>\n\n"
                        "Instagram redirected to login.\n"
                        "Please update <code>INSTAGRAM_SESSION_COOKIES</code>."
                    )
                return False

            self.dismiss_popups()

            try:
                self._page.wait_for_selector("video", timeout=12_000)
                self.log.info("Video elements detected — Reels feed is live.")
            except PlaywrightTimeout:
                self.log.warning("No <video> found within 12s — proceeding anyway.")

            return True

        except PlaywrightError as exc:
            self.log.error(f"Navigation PlaywrightError: {exc}")
            if notifier:
                notifier.send_debug(f"❌ Navigation error:\n<pre>{str(exc)[:400]}</pre>")
            return False
        except Exception as exc:
            self.log.error(f"Navigation unexpected error: {exc}")
            return False

    # ── Reel URL collection ───────────────────────────────────────────────────

    def collect_reel_urls(self, notifier=None) -> List[str]:
        """
        Collect reel URLs via ArrowDown keyboard navigation.

        Instagram's SPA does not update the DOM with new reel URLs on scroll;
        ArrowDown is the only reliable mechanism to advance the active reel
        and get the URL bar to update.
        """
        seen: set = set()
        collected: List[str] = []

        self.log.info(
            f"Collecting up to {Config.TARGET_REELS_SCAN} reel URLs "
            f"via ArrowDown navigation…"
        )

        def _scrape_links() -> None:
            for sel in SelectorRegistry.REEL_LINKS:
                try:
                    for link in self._page.query_selector_all(sel):
                        try:
                            href = link.get_attribute("href") or ""
                            if not href:
                                continue
                            full = (
                                f"https://www.instagram.com{href}"
                                if href.startswith("/") else href
                            )
                            full = full.split("?")[0].rstrip("/") + "/"
                            if full not in seen and self._is_valid_reel_url(full):
                                seen.add(full)
                                collected.append(full)
                                self.log.debug(f"DOM link: {self.extract_reel_id(full)}")
                        except PlaywrightError as exc:
                            self.log.debug(f"Link attribute read error: {exc}")
                        except Exception as exc:
                            self.log.debug(f"Link attribute unexpected error: {exc}")
                except PlaywrightError as exc:
                    self.log.debug(f"query_selector_all({sel!r}) PlaywrightError: {exc}")
                except Exception as exc:
                    self.log.debug(f"query_selector_all({sel!r}) unexpected error: {exc}")

        def _capture_url_bar() -> None:
            current = self._page.url
            clean = current.split("?")[0].rstrip("/") + "/"
            if self._is_valid_reel_url(clean) and clean not in seen:
                seen.add(clean)
                collected.append(clean)
                self.log.info(
                    f"[{len(collected)}/{Config.TARGET_REELS_SCAN}] "
                    f"Captured: {self.extract_reel_id(clean)}"
                )

        def _focus_player() -> bool:
            try:
                self._page.evaluate("() => { document.body && document.body.focus(); }")
                self._bm.delay(100, 200)
                safe_x = max(10, Config.VIEWPORT_W // 6)
                self._page.mouse.click(safe_x, 60)
                self._bm.delay(200, 400)
                self._page.evaluate("() => { document.body && document.body.focus(); }")
                self._page.keyboard.press("Escape")
                self._bm.delay(150, 300)
                return True
            except PlaywrightError as exc:
                self.log.debug(f"Focus player PlaywrightError: {exc}")
                return False
            except Exception as exc:
                self.log.debug(f"Focus player unexpected error: {exc}")
                return False

        def _wait_url_change(old_url: str, max_wait: float = 3.5) -> bool:
            deadline = time.time() + max_wait
            while time.time() < deadline:
                if self._page.url != old_url:
                    return True
                time.sleep(0.15)
            return False

        _focus_player()
        _capture_url_bar()
        _scrape_links()

        # Initial state debug snapshot
        if notifier:
            try:
                snap = self._page.screenshot(type="jpeg", quality=65)
                active_el = self._page.evaluate(
                    "() => document.activeElement ? "
                    "document.activeElement.tagName + '#' + "
                    "(document.activeElement.id || '') : 'none'"
                )
                notifier.send_debug(
                    f"📸 <b>Feed initial state</b>\n"
                    f"URL: <code>{self._page.url}</code>\n"
                    f"Active element: <code>{active_el}</code>\n"
                    f"URLs so far: {len(collected)}",
                    snap,
                )
            except Exception as exc:
                self.log.debug(f"Initial state snapshot failed: {exc}")

        consecutive_stuck = 0
        stuck_refocus_count = 0
        max_steps = Config.TARGET_REELS_SCAN * 4
        last_progress_report = 0

        for step in range(max_steps):
            if len(collected) >= Config.TARGET_REELS_SCAN:
                break

            prev_url = self._page.url
            try:
                self._page.keyboard.press("ArrowDown")
            except PlaywrightError as exc:
                self.log.warning(f"ArrowDown step {step} PlaywrightError: {exc}")
                consecutive_stuck += 1
            except Exception as exc:
                self.log.warning(f"ArrowDown step {step} unexpected error: {exc}")
                consecutive_stuck += 1
            else:
                url_changed = _wait_url_change(prev_url)
                if url_changed:
                    consecutive_stuck = 0
                    stuck_refocus_count = 0
                    _capture_url_bar()
                    _scrape_links()
                    self._bm.delay(600, 1200)

                    # Progress snapshot every 5 URLs collected
                    if (
                        len(collected) > 0
                        and len(collected) % 5 == 0
                        and len(collected) != last_progress_report
                        and notifier
                    ):
                        last_progress_report = len(collected)
                        try:
                            snap = self._page.screenshot(type="jpeg", quality=60)
                            notifier.send_debug(
                                f"📊 <b>Collection progress</b>\n"
                                f"  Collected: {len(collected)}/{Config.TARGET_REELS_SCAN}\n"
                                f"  Step: {step+1}/{max_steps}",
                                snap,
                            )
                        except Exception as exc:
                            self.log.debug(f"Progress snapshot failed: {exc}")
                else:
                    consecutive_stuck += 1
                    self.log.debug(
                        f"Step {step}: URL unchanged (stuck={consecutive_stuck})"
                    )

            # Stuck recovery
            if consecutive_stuck > 0 and consecutive_stuck % 3 == 0:
                stuck_refocus_count += 1
                self.log.info(
                    f"Re-focusing player (stuck={consecutive_stuck}, "
                    f"refocus #{stuck_refocus_count})"
                )
                _focus_player()

                if stuck_refocus_count % 2 == 0 and notifier:
                    try:
                        snap = self._page.screenshot(type="jpeg", quality=65)
                        active_el = self._page.evaluate(
                            "() => document.activeElement ? "
                            "document.activeElement.tagName : 'unknown'"
                        )
                        notifier.send_debug(
                            f"🔁 <b>Stuck — refocus #{stuck_refocus_count}</b>\n"
                            f"  Step: {step}  Stuck: {consecutive_stuck}\n"
                            f"  Collected: {len(collected)}\n"
                            f"  Active: <code>{active_el}</code>",
                            snap,
                        )
                    except Exception as exc:
                        self.log.debug(f"Stuck refocus snapshot failed: {exc}")

            if consecutive_stuck >= 8:
                self.log.warning(
                    "ArrowDown stuck 8× — feed exhausted or player lost focus. Stopping."
                )
                if notifier:
                    try:
                        snap = self._page.screenshot(type="jpeg", quality=65)
                        notifier.send_debug(
                            f"⛔ <b>ArrowDown stuck 8× — stopping</b>\n"
                            f"  Collected: {len(collected)}",
                            snap,
                        )
                    except Exception as exc:
                        self.log.debug(f"Stuck-stop snapshot failed: {exc}")
                break

            self._bm.delay(800, 1400)

        self.log.info(
            f"Collection complete: {len(collected)} unique URLs in {step+1} steps."
        )
        return collected
