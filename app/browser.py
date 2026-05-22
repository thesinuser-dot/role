#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# browser.py — Playwright browser lifecycle + stealth + human simulation
#
# Stealth improvements over the original:
#   WebGL    — vendor/renderer strings spoofed to a real Intel GPU
#   Canvas   — per-session noise seed injected into toDataURL / getImageData
#   Audio    — AnalyserNode frequency data jittered to defeat fingerprinting
#   Fonts    — font availability list locked to a realistic Windows 10 set
#   Screen   — window.screen dimensions made consistent with the viewport
#   Mouse    — cubic Bezier curves + micro-jitter replace direct jumps
#
# These make the browser profile look like a real Chrome user rather than an
# automated headless session.  Instagram's bot detection is canvas+WebGL based.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import math
import os
import random
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
    Error as PlaywrightError,
)

from config import Config
from proxy_pool import ProxyPool

# ── Module-level proxy pool (initialised once, shared across BrowserManager instances) ──
_proxy_pool: Optional[ProxyPool] = None

def get_proxy_pool() -> Optional[ProxyPool]:
    """Return the module-level ProxyPool, creating it on first call if proxies are enabled."""
    global _proxy_pool
    if not Config.USE_PROXY:
        return None
    if _proxy_pool is None:
        _proxy_pool = ProxyPool(
            api_key=Config.WEBSHARE_API_KEY,
            mode=Config.WEBSHARE_PROXY_MODE,
        )
    return _proxy_pool


# ─────────────────────────────────────────────────────────────────────────────
# Stealth JS — injected into every new page context
# ─────────────────────────────────────────────────────────────────────────────

def _build_stealth_script(canvas_seed: int) -> str:
    """
    Return a JS snippet that masks automation signals.

    canvas_seed is randomised per browser session so each run produces a
    different canvas fingerprint — identical seeds would make all runs
    look like the same device.
    """
    return f"""
(() => {{
  // ── webdriver flag ────────────────────────────────────────────────────────
  const proto = Object.getPrototypeOf(navigator);
  Object.defineProperty(proto, 'webdriver', {{ get: () => undefined, configurable: true }});

  // ── plugins ───────────────────────────────────────────────────────────────
  Object.defineProperty(navigator, 'plugins', {{
    get: () => {{
      const arr = [
        {{ name:'Chrome PDF Plugin',  filename:'internal-pdf-viewer', description:'Portable Document Format', length:1 }},
        {{ name:'Chrome PDF Viewer',  filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', description:'', length:1 }},
        {{ name:'Native Client',      filename:'internal-nacl-plugin', description:'', length:2 }},
      ];
      Object.setPrototypeOf(arr, PluginArray.prototype);
      return arr;
    }},
  }});

  // ── navigator misc ────────────────────────────────────────────────────────
  Object.defineProperty(navigator, 'languages', {{ get: () => ['en-US', 'en'] }});
  Object.defineProperty(navigator, 'platform',  {{ get: () => 'Win32' }});
  Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => 8 }});
  Object.defineProperty(navigator, 'deviceMemory', {{ get: () => 8 }});

  // ── chrome runtime shim ──────────────────────────────────────────────────
  if (!window.chrome) {{
    window.chrome = {{
      app: {{ isInstalled: false }},
      runtime: {{ onConnect: {{}}, onMessage: {{}} }},
      loadTimes: () => ({{}}),
      csi: () => ({{}}),
    }};
  }}

  // ── permissions ───────────────────────────────────────────────────────────
  const origQuery = navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query = (params) => {{
    if (params.name === 'notifications') {{
      return Promise.resolve({{ state: Notification.permission, onchange: null }});
    }}
    return origQuery(params);
  }};

  // ── WebGL fingerprint spoof ───────────────────────────────────────────────
  // Masquerade as a common Intel Iris GPU — the most frequently seen renderer
  // in real browser populations.  Without this the default "SwiftShader" /
  // "Google SwiftShader" string is a near-certain automation signal.
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function(p) {{
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
    return _getParam.call(this, p);
  }};
  const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
  WebGL2RenderingContext.prototype.getParameter = function(p) {{
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel(R) Iris(TM) Graphics 6100';
    return _getParam2.call(this, p);
  }};

  // ── Canvas noise ─────────────────────────────────────────────────────────
  // Injects a deterministic-per-session but unique-per-run noise seed into
  // canvas readback methods.  The noise is imperceptible visually (±1 on
  // one channel per 4 pixels) but defeats fingerprint matching.
  const _seed = {canvas_seed};
  let _noiseCtr = 0;
  function _noise() {{
    // xorshift32
    _noiseCtr ^= _noiseCtr << 13;
    _noiseCtr ^= _noiseCtr >> 17;
    _noiseCtr ^= _noiseCtr << 5;
    return (_noiseCtr >>> 0) % 3 - 1;  // -1, 0, or +1
  }}
  _noiseCtr = _seed;

  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
    if (this.width > 16 && this.height > 16) {{
      const ctx = this.getContext('2d');
      if (ctx) {{
        const d = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < d.data.length; i += 16) d.data[i] = Math.max(0, Math.min(255, d.data[i] + _noise()));
        ctx.putImageData(d, 0, 0);
      }}
    }}
    return _toDataURL.call(this, type, quality);
  }};

  const _toBlob = HTMLCanvasElement.prototype.toBlob;
  HTMLCanvasElement.prototype.toBlob = function(cb, type, quality) {{
    if (this.width > 16 && this.height > 16) {{
      const ctx = this.getContext('2d');
      if (ctx) {{
        const d = ctx.getImageData(0, 0, this.width, this.height);
        for (let i = 0; i < d.data.length; i += 16) d.data[i] = Math.max(0, Math.min(255, d.data[i] + _noise()));
        ctx.putImageData(d, 0, 0);
      }}
    }}
    return _toBlob.call(this, cb, type, quality);
  }};

  // ── Audio fingerprint jitter ──────────────────────────────────────────────
  // AnalyserNode.getFloatFrequencyData is used by audio fingerprinters.
  // Adding a tiny per-session jitter makes each run look like a different device.
  const _audioSeed = (_seed ^ 0xdeadbeef) >>> 0;
  let _audioNoise = _audioSeed;
  function _aNoise() {{
    _audioNoise ^= _audioNoise << 13;
    _audioNoise ^= _audioNoise >> 17;
    _audioNoise ^= _audioNoise << 5;
    return ((_audioNoise >>> 0) / 0xffffffff) * 0.0001;
  }}

  const _AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (_AudioCtx) {{
    const _createAnalyser = _AudioCtx.prototype.createAnalyser;
    _AudioCtx.prototype.createAnalyser = function() {{
      const node = _createAnalyser.call(this);
      const _orig = node.getFloatFrequencyData.bind(node);
      node.getFloatFrequencyData = function(arr) {{
        _orig(arr);
        for (let i = 0; i < arr.length; i++) arr[i] += _aNoise();
      }};
      return node;
    }};
  }}

  // ── Font list spoofing ────────────────────────────────────────────────────
  // Some fingerprinters enumerate fonts via document.fonts.  We lock the
  // reported set to the fonts that ship on a standard Windows 10 install,
  // which is the largest real-user population.
  const _WIN10_FONTS = [
    'Arial','Arial Black','Calibri','Cambria','Comic Sans MS','Consolas',
    'Courier New','Georgia','Impact','Lucida Console','Segoe UI','Tahoma',
    'Times New Roman','Trebuchet MS','Verdana','Wingdings',
  ];
  if (document.fonts) {{
    try {{
      const origCheck = document.fonts.check.bind(document.fonts);
      document.fonts.check = function(font, text) {{
        const name = font.replace(/[0-9]+px\s+/, '').replace(/['"]/g,'').trim();
        return _WIN10_FONTS.some(f => f.toLowerCase() === name.toLowerCase());
      }};
    }} catch(e) {{}}
  }}

  // ── Screen / window consistency ────────────────────────────────────────────
  // If viewport is 430×932 but screen reports 1920×1080, that mismatch is a
  // fingerprinting signal on mobile-emulated sessions.
  Object.defineProperty(screen, 'width',       {{ get: () => {Config.VIEWPORT_W} }});
  Object.defineProperty(screen, 'height',      {{ get: () => {Config.VIEWPORT_H} }});
  Object.defineProperty(screen, 'availWidth',  {{ get: () => {Config.VIEWPORT_W} }});
  Object.defineProperty(screen, 'availHeight', {{ get: () => {Config.VIEWPORT_H} }});
  Object.defineProperty(screen, 'colorDepth',  {{ get: () => 24 }});
  Object.defineProperty(screen, 'pixelDepth',  {{ get: () => 24 }});
}})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# BrowserManager
# ─────────────────────────────────────────────────────────────────────────────

class BrowserManager:
    def __init__(self):
        self.log = logging.getLogger("BrowserManager")
        self._pw:      Optional[Playwright]      = None
        self._browser: Optional[Browser]         = None
        self._ctx:     Optional[BrowserContext]  = None
        self._page:    Optional[Page]            = None
        self._trace_dir: Optional[Path]          = None
        self._canvas_seed: int = random.randint(1, 0x7FFFFFFF)
        self._active_proxy: Optional[dict] = None  # currently-used proxy dict

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("BrowserManager.launch() has not been called")
        return self._page

    # ── Cookie helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_cookies(raw: str) -> List[dict]:
        """
        Accept either a JSON array (from EditThisCookie export) or a
        semicolon-separated "name=value; name=value" string.
        Returns a list of Playwright-compatible cookie dicts.
        All parsing errors are logged, never silently swallowed.
        """
        import json, re
        log = logging.getLogger("BrowserManager")
        cookies: List[dict] = []
        if not raw or not raw.strip():
            return cookies
        base = {
            "domain": ".instagram.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "None",
            "expires": 2147483647,
        }
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for c in parsed:
                    cookie = {**base}
                    cookie["name"]     = c.get("name", "")
                    cookie["value"]    = c.get("value", "")
                    cookie["domain"]   = c.get("domain", ".instagram.com")
                    cookie["path"]     = c.get("path", "/")
                    cookie["secure"]   = c.get("secure", True)
                    cookie["httpOnly"] = c.get("httpOnly", False)
                    exp = c.get("expirationDate", c.get("expires", 2147483647))
                    try:
                        cookie["expires"] = int(float(exp)) if exp else 2147483647
                    except (TypeError, ValueError) as exc:
                        log.debug(f"Cookie expiry parse error for {cookie['name']!r}: {exc}")
                        cookie["expires"] = 2147483647
                    if cookie["name"]:
                        cookies.append(cookie)
                return cookies
        except (json.JSONDecodeError, ValueError):
            pass  # fall through to semicolon parse
        except Exception as exc:
            log.warning(f"Cookie JSON parse unexpected error: {exc}")

        for part in raw.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            name, _, value = part.partition("=")
            name  = name.strip()
            value = value.strip()
            if name:
                cookies.append({**base, "name": name, "value": value})
        return cookies

    def write_netscape_cookies(self, raw: str) -> bool:
        """
        Write a Netscape-format cookies file for yt-dlp.
        Returns True on success, False on failure.
        Failure is non-fatal — yt-dlp will run without authentication.
        """
        cookies = self._parse_cookies(raw)
        if not cookies:
            self.log.warning("No cookies to write for yt-dlp (INSTAGRAM_SESSION_COOKIES empty).")
            return False
        lines = ["# Netscape HTTP Cookie File", "# Generated by ReelsHunter"]
        for c in cookies:
            domain    = c.get("domain", ".instagram.com")
            subdomain = "TRUE" if domain.startswith(".") else "FALSE"
            path      = c.get("path", "/")
            secure    = "TRUE" if c.get("secure") else "FALSE"
            expires   = str(int(c.get("expires", 2147483647)))
            name      = c.get("name", "")
            value     = c.get("value", "")
            lines.append(f"{domain}\t{subdomain}\t{path}\t{secure}\t{expires}\t{name}\t{value}")
        try:
            Config.COOKIES_FILE.write_text("\n".join(lines))
            self.log.info(
                f"Netscape cookies written: {Config.COOKIES_FILE} ({len(cookies)} cookies)"
            )
            return True
        except OSError as exc:
            self.log.error(f"Could not write Netscape cookies file: {exc}")
            return False

    # ── Launch ────────────────────────────────────────────────────────────────

    def launch(self, cookies: List[dict] = None) -> None:
        """
        Launch Chromium with full stealth settings.

        cookies: optional list of Playwright-format cookie dicts.
        If omitted, cookies are parsed from Config.INSTAGRAM_SESSION_COOKIES.
        """
        if cookies is None:
            cookies = self._parse_cookies(Config.INSTAGRAM_SESSION_COOKIES)
        ua = random.choice(Config.USER_AGENTS)

        # ── Proxy selection ────────────────────────────────────────────────────
        pool = get_proxy_pool()
        self._active_proxy = pool.get_random() if pool else None
        if self._active_proxy:
            self.log.info(f"Using proxy: {self._active_proxy['server']}")
        else:
            self.log.info("No proxy in use (pool empty or disabled)")

        self.log.info(f"Launching Chromium (headless={Config.HEADLESS}) UA={ua[:60]}...")
        self._pw = sync_playwright().start()
        # Use system Chromium (with H.264/AAC codecs) when available in container
        _chromium_exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        self._browser = self._pw.chromium.launch(
            executable_path=_chromium_exe or None,
            headless=Config.HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                                "--disable-site-isolation-trials",
                "--disable-web-security",
                "--disable-extensions",
                "--disable-default-apps",
                "--disable-infobars",
                "--disable-notifications",
                "--disable-popup-blocking",
                "--disable-hang-monitor",
                "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-client-side-phishing-detection",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                "--use-mock-keychain",
                # ── Video playback ─────────────────────────────────────────
                # Instagram serves H.264/AAC — ensure software decode is on
                "--autoplay-policy=no-user-gesture-required",
                "--enable-features=MediaFoundationH264Encoding",
                "--disable-features=VizDisplayCompositor,IsolateOrigins,LegacyTLSEnforced",
                "--force-color-profile=srgb",
                "--enable-accelerated-video-decode",
                "--enable-gpu-rasterization",
                # ── Avoid triggering CDN bot-detection ─────────────────────
                # Remove the explicit UA from args — it's set in context below,
                # having it in both places sometimes creates a mismatch header
                f"--window-size={Config.VIEWPORT_W},{Config.VIEWPORT_H}",
            ],
        )
        self._ctx = self._browser.new_context(
            viewport={"width": Config.VIEWPORT_W, "height": Config.VIEWPORT_H},
            user_agent=ua,
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="light",
            device_scale_factor=2.0,
            permissions=["notifications"],
            ignore_https_errors=True,
            **({"proxy": self._active_proxy} if self._active_proxy else {}),
        )
        self._ctx.add_init_script(_build_stealth_script(self._canvas_seed))
        if cookies:
            self._ctx.add_cookies(cookies)
            self.log.info(f"Injected {len(cookies)} session cookies.")
        else:
            self.log.warning("No session cookies injected — may require login.")

        self._page = self._ctx.new_page()
        self._page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "upgrade-insecure-requests": "1",
        })

        # Playwright tracing
        try:
            trace_dir = Config.SCREENSHOT_DIR.parent / "playwright-traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            self._trace_dir = trace_dir
            self._ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
            self.log.info(f"Playwright tracing started → {trace_dir}/trace.zip")
        except Exception as exc:
            self.log.warning(f"Could not start Playwright tracing: {exc}")
            self._trace_dir = None

        self.log.info("Browser context ready.")
        if not Config.HEADLESS:
            self.raise_chromium_window()

    # ── Shutdown ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        if self._ctx and self._trace_dir:
            try:
                trace_path = self._trace_dir / "trace.zip"
                self._ctx.tracing.stop(path=str(trace_path))
                self.log.info(f"Playwright trace saved → {trace_path}")
            except Exception as exc:
                self.log.warning(f"Could not save Playwright trace: {exc}")

        for name, closer in (
            ("page",       lambda: self._page.close()    if self._page    else None),
            ("context",    lambda: self._ctx.close()     if self._ctx     else None),
            ("browser",    lambda: self._browser.close() if self._browser else None),
            ("playwright", lambda: self._pw.stop()       if self._pw      else None),
        ):
            try:
                closer()
                self.log.debug(f"Closed: {name}")
            except Exception as exc:
                self.log.warning(f"Error closing {name}: {exc}")

        # ── Proxy feedback ─────────────────────────────────────────────────────
        # mark_proxy_success() / mark_proxy_failed() should be called by the
        # caller BEFORE close() when they know whether the session worked.
        # close() itself just cleans up resources.

    def mark_proxy_success(self) -> None:
        """Tell the pool the current proxy worked fine."""
        pool = get_proxy_pool()
        if pool and self._active_proxy:
            pool.mark_success(self._active_proxy)

    def mark_proxy_failed(self) -> None:
        """Tell the pool the current proxy failed; it may be quarantined."""
        pool = get_proxy_pool()
        if pool and self._active_proxy:
            pool.mark_failed(self._active_proxy)
            self.log.warning(f"Marked proxy as failed: {self._active_proxy.get('server')}")

    # ── Human-like interaction ────────────────────────────────────────────────

    def delay(self, lo_ms: int = 400, hi_ms: int = 1800) -> None:
        time.sleep(random.randint(lo_ms, hi_ms) / 1000)

    def move_mouse_bezier(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        steps: int = 25,
    ) -> None:
        """
        Move the mouse from (x1, y1) to (x2, y2) along a cubic Bezier curve
        with per-step Gaussian micro-jitter.  This produces the kind of slightly
        wobbly arc that characterises real hand movement rather than the
        perfectly straight jumps that automated tools produce.
        """
        def _b(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
            u = 1 - t
            return u**3*p0 + 3*u**2*t*p1 + 3*u*t**2*p2 + t**3*p3

        # Control points: offset randomly from the straight line
        dx, dy = x2 - x1, y2 - y1
        cx1 = x1 + random.uniform(-50, 50) + dx * 0.3
        cy1 = y1 + random.uniform(-30, 30) + dy * 0.1
        cx2 = x1 + random.uniform(-50, 50) + dx * 0.7
        cy2 = y1 + random.uniform(-30, 30) + dy * 0.9

        for i in range(steps + 1):
            t = i / steps
            x = _b(t, x1, cx1, cx2, x2) + random.gauss(0, 0.4)
            y = _b(t, y1, cy1, cy2, y2) + random.gauss(0, 0.4)
            self._page.mouse.move(x, y)
            time.sleep(random.uniform(0.004, 0.018))

    def human_scroll(self, direction: str = "down", target_px: int = 932) -> None:
        """Scroll with a sine-weighted velocity profile — accelerates then decelerates."""
        sign = 1 if direction == "down" else -1
        steps = random.randint(8, 16)
        weights = [math.sin(math.pi * i / (steps - 1)) for i in range(steps)]
        total_w = sum(weights)
        for w in weights:
            delta = (w / total_w) * target_px
            self._page.mouse.wheel(0, sign * delta)
            time.sleep(random.uniform(0.025, 0.09))
        self.delay(200, 700)

    def type_human_like(self, selector: str, text: str) -> None:
        """Click a field and type with per-character delays like a real typist."""
        self._page.click(selector)
        for char in text:
            self._page.keyboard.type(char)
            time.sleep(random.uniform(0.025, 0.09))
        self.delay(200, 700)

    # ── Window management ─────────────────────────────────────────────────────

    def raise_chromium_window(self) -> bool:
        """
        Attempt to raise the Chromium window to the foreground on the Xpra desktop.
        Returns True if at least one window was successfully raised.
        Four strategies tried in order, each logged at the appropriate level.
        """
        display = os.environ.get("DISPLAY", ":99")
        env = {**os.environ, "DISPLAY": display}
        time.sleep(1.5)

        xdotool_ok = subprocess.run(
            ["which", "xdotool"], capture_output=True, text=True, timeout=5
        ).returncode == 0
        if not xdotool_ok:
            self.log.warning(
                "xdotool not installed — window raise skipped. "
                "Add 'xdotool' to apt-get install in the workflow to fix this."
            )
            return False

        def _xdo(*args, timeout=5):
            return subprocess.run(
                ["xdotool"] + list(args),
                capture_output=True, text=True, timeout=timeout, env=env,
            )

        def _raise_wid(wid: str, label: str) -> bool:
            try:
                subprocess.run(
                    ["xdotool", "windowmove", "--sync", wid, "0", "0"],
                    capture_output=True, timeout=3, env=env,
                )
                subprocess.run(
                    ["xdotool", "windowraise", wid, "windowfocus", "--sync", wid],
                    capture_output=True, timeout=3, env=env,
                )
                self.log.info(f"Window raised: WID={wid} ({label})")
                return True
            except Exception as exc:
                self.log.debug(f"windowraise WID={wid} failed: {exc}")
                return False

        raised = False

        # Strategy 1: search by PID
        try:
            pgrep = subprocess.run(
                ["pgrep", "-f", "chromium|chrome"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in pgrep.stdout.splitlines() if p.strip()]
            self.log.info(f"Chromium PIDs: {pids}")
            for pid in pids:
                for wid in _xdo("search", "--pid", pid).stdout.strip().splitlines():
                    if wid.strip() and _raise_wid(wid.strip(), f"PID={pid}"):
                        raised = True
        except Exception as exc:
            self.log.debug(f"PID strategy failed: {exc}")

        if raised:
            return True

        # Strategy 2: search by class / name patterns
        for args in (
            ("search", "--class", "chromium"),
            ("search", "--class", "Chromium"),
            ("search", "--classname", "chromium"),
            ("search", "--name", "Instagram"),
            ("search", "--name", "Chromium"),
        ):
            try:
                for wid in _xdo(*args).stdout.strip().splitlines():
                    if wid.strip() and _raise_wid(wid.strip(), " ".join(args)):
                        raised = True
            except Exception as exc:
                self.log.debug(f"Class/name strategy {args} failed: {exc}")

        if raised:
            return True

        # Strategy 3: enumerate all windows, match by title
        try:
            all_wids = [
                w.strip()
                for w in _xdo("search", "--name", "").stdout.splitlines()
                if w.strip()
            ]
            self.log.info(f"Total X windows on {display}: {len(all_wids)}")
            keywords = {"chromium", "chrome", "instagram", "facebook", "reels"}
            for wid in all_wids:
                try:
                    name = _xdo("getwindowname", wid, timeout=2).stdout.strip().lower()
                    if any(k in name for k in keywords):
                        self.log.info(f"Matched window by title: '{name}' WID={wid}")
                        if _raise_wid(wid, f"title={name}"):
                            raised = True
                            break
                except Exception as exc:
                    self.log.debug(f"getwindowname WID={wid} failed: {exc}")
        except Exception as exc:
            self.log.debug(f"Full-enum strategy failed: {exc}")

        if raised:
            return True

        # Strategy 4: dump all window names for debugging
        try:
            all_wids = [
                w.strip()
                for w in _xdo("search", "--name", "").stdout.splitlines()
                if w.strip()
            ]
            names = []
            for wid in all_wids[:20]:
                try:
                    n = _xdo("getwindowname", wid, timeout=1).stdout.strip()
                    if n:
                        names.append(f"  WID={wid}  {n}")
                except Exception as exc:
                    self.log.debug(f"getwindowname diagnostic WID={wid} failed: {exc}")
            self.log.warning(
                "Could not find a Chromium window to raise.\n"
                f"All X windows ({len(all_wids)} total):\n" + "\n".join(names or ["  (none)"])
            )
        except Exception as exc:
            self.log.warning(f"Window diagnostic enumeration failed: {exc}")

        return False
