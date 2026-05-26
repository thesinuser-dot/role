#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# collector.py — Instagram Reel URL collection and metrics extraction
#
# Key fix (2026-05): Instagram changed "X views" → "X plays" for Reels.
#   All extraction strategies (JS, aria-label, text-node walk, CSS selectors)
#   now handle BOTH "views" and "plays" so view counts are never 0.
#
# Key improvement: SelectorRegistry
#   Instagram's DOM structure changes frequently.  Hard-coded single selectors
#   break silently and the agent collects nothing.  SelectorRegistry holds a
#   priority-ordered list for each DOM target; the first selector that returns
#   a non-empty result wins.  When all selectors fail the failure is logged at
#   WARNING level with the full selector list so it's easy to update.
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

    NOTE 2026-05: Instagram Reels now shows "X plays" instead of "X views".
    All VIEW_COUNT selectors check both "plays" and "views".
    """

    VIEW_COUNT: List[str] = [
        # ── "plays" selectors (Instagram Reels current label) ──
        "[aria-label*='plays' i]",
        "[aria-label*='play' i]",
        "span[class*='play' i]",
        "div[class*='play' i]",
        # ── legacy "views" selectors ──
        "[aria-label*='views' i]",
        "[aria-label*='view' i]",
        "span[class*='view' i]",
        "div[class*='view' i]",
        "span[class*='View']",
        # ── broad fallbacks ──
        "section span[class]",
        "main span[class]",
        "article span[class]",
        "span",              # filtered by text content in caller
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
        """
        Extract view/play + like counts from the current reel page.

        IMPORTANT: Instagram Reels now displays "X plays" not "X views".
        Both labels are handled throughout this method.
        """
        # Step 1: wait for network to settle so lazy JS has run
        try:
            self._page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass  # non-fatal — proceed with whatever is loaded

        # Step 2: scroll the like/play section into view to trigger lazy render
        try:
            self._page.evaluate("""
                () => {
                    const candidates = [
                        ...document.querySelectorAll('section, [role="main"]'),
                    ];
                    const last = candidates[candidates.length - 1];
                    if (last) last.scrollIntoView({behavior: 'instant', block: 'center'});
                }
            """)
        except Exception:
            pass

        self._bm.delay(2000, 3000)

        # Step 3: try extraction — retry once with longer wait if we get 0
        for attempt in range(2):
            views = self._extract_views_js()
            if views == 0:
                self.log.debug(f"JS view extraction got 0 (attempt {attempt+1}) — trying CSS fallback")
                views = self._extract_count(
                    SelectorRegistry.VIEW_COUNT,
                    keywords=["view", "play"],
                    exclude=None,
                    label="views",
                )

            likes = self._extract_count(
                SelectorRegistry.LIKE_COUNT,
                keywords=["like"],
                exclude="unlike",
                label="likes",
            )

            if views > 0 or likes > 0:
                break

            if attempt == 0:
                self.log.debug("Stats still 0 — waiting 4s and retrying extraction")
                self._bm.delay(3500, 4500)
                try:
                    self._page.mouse.wheel(0, 300)
                    self._bm.delay(500, 800)
                    self._page.mouse.wheel(0, -300)
                except Exception:
                    pass

        self.log.info(f"Metrics: views={views:,}  likes={likes:,}")
        return {"views": views, "likes": likes}

    def _extract_views_js(self) -> int:
        """
        Multi-strategy JS extraction for view/play count.

        FIX 2026-05: Instagram Reels changed "views" label to "plays".
        All text-based strategies now check BOTH "views" and "plays".

        Strategies (most-reliable first):
          1. window.__additionalData / window._sharedData (API response in page)
          2. Inline <script> JSON blobs — play_count / video_view_count
          3. aria-label attributes containing "X plays" OR "X views"
          4. Text-node walk: single node "1.2M plays" or "1.2M views"
          5. Text-node walk: number node followed by "plays"/"views" node nearby
          6. window.__bbox (newer IG data container)
        """
        try:
            raw = self._page.evaluate("""
                () => {
                    // ── Strategy 1: Instagram's in-page data objects ──────────────────
                    try {
                        if (window.__additionalData) {
                            const vals = Object.values(window.__additionalData);
                            for (const v of vals) {
                                const media = v?.data?.xdt_api__v1__media__shortcode__web_info?.data?.items?.[0]
                                    || v?.data?.shortcode_media;
                                if (media) {
                                    const vc = media.video_view_count ?? media.play_count
                                        ?? media.view_count ?? null;
                                    if (vc != null && vc > 0) return String(vc);
                                }
                            }
                        }
                    } catch(e) {}

                    try {
                        if (window._sharedData) {
                            const media = window._sharedData?.entry_data?.PostPage?.[0]
                                ?.graphql?.shortcode_media;
                            if (media) {
                                const vc = media.video_view_count ?? media.video_play_count;
                                if (vc > 0) return String(vc);
                            }
                        }
                    } catch(e) {}

                    // ── Strategy 2: inline <script> JSON blobs ────────────────────────
                    try {
                        const scripts = document.querySelectorAll('script[type="application/json"], script:not([src])');
                        for (const s of scripts) {
                            const txt = s.textContent || '';
                            const m = /\"(?:video_view_count|play_count|video_play_count|view_count)\"\\s*:\\s*(\\d+)/.exec(txt);
                            if (m && parseInt(m[1]) > 0) return m[1];
                        }
                    } catch(e) {}

                    // ── Strategy 3: aria-label "X plays" OR "X views" ─────────────────
                    try {
                        for (const el of document.querySelectorAll('[aria-label]')) {
                            const label = el.getAttribute('aria-label') || '';
                            // Match "X plays" or "X views" (Instagram uses "plays" for Reels)
                            const m = /([\\d,]+\\.?\\d*\\s*[KMBkmb]?)\\s+(?:plays?|views?)/i.exec(label);
                            if (m) return m[1];
                        }
                    } catch(e) {}

                    // ── Strategy 4 & 5: text-node walk (plays OR views) ───────────────
                    try {
                        const walker = document.createTreeWalker(
                            document.body, NodeFilter.SHOW_TEXT, null, false
                        );
                        const texts = [];
                        let node;
                        while ((node = walker.nextNode())) {
                            const t = node.textContent.trim();
                            if (t) texts.push(t);
                        }

                        // Single node: "1.2M plays" or "1.2M views"
                        for (const t of texts) {
                            const m = /^([\\d,]+\\.?\\d*\\s*[KMBkmb]?)\\s+(?:plays?|views?)$/i.exec(t);
                            if (m) return m[1];
                        }

                        // Separate nodes: number then "plays" or "views" within 4 positions
                        for (let i = 0; i < texts.length - 1; i++) {
                            const nearby = [texts[i+1], texts[i+2], texts[i+3]].filter(Boolean);
                            if (nearby.some(t => /^(?:plays?|views?)$/i.test(t))) {
                                const m = /^([\\d,]+\\.?\\d*\\s*[KMBkmb]?)$/.exec(texts[i]);
                                if (m) return m[1];
                            }
                        }
                    } catch(e) {}

                    // ── Strategy 6: window.__bbox (newer IG data container) ───────────
                    try {
                        if (window.__bbox && window.__bbox.define) {
                            const str = JSON.stringify(window.__bbox);
                            const m = /\"(?:video_view_count|play_count)\":(\\d+)/.exec(str);
                            if (m && parseInt(m[1]) > 0) return m[1];
                        }
                    } catch(e) {}

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
        keywords: List[str],
        exclude: Optional[str],
        label: str,
    ) -> int:
        """
        Extract a numeric count from DOM elements matching any of the given keywords.

        `keywords` is a list — element text/aria-label must contain at least one.
        This allows checking for both "view" and "play" in a single call.
        """
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
                        # Must match at least one keyword
                        if not any(kw in combined for kw in keywords):
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

    def collect_reel_urls(self, notifier=None, stop_fn=None) -> List[str]:
        """
        Collect reel URLs via ArrowDown keyboard navigation.
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
            if stop_fn is not None and stop_fn():
                self.log.info(
                    f"/skip received — stopping collection early "
                    f"({len(collected)} URL(s) collected so far)."
                )
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
