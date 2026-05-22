#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# downloader.py — Three-strategy video acquisition layer
#
# Strategy 1: yt-dlp (preferred)
#   Downloads the raw CDN MP4 — watermark-free, highest quality.
#   Authenticated via Netscape cookies file.
#   Retries internally (--retries 3, --fragment-retries 5).
#
# Strategy 2: Browser network interception (fallback)
#   Navigates to the reel page and listens for the CDN video URL via
#   Playwright's response event.  Once captured, downloads it with requests.
#   Works when yt-dlp's extractor is broken by an Instagram API change.
#
# Strategy 3: Video src attribute extraction (last resort)
#   Reads the <video src="..."> attribute directly from the DOM.
#   Quality is lower (usually the preview stream) but better than nothing.
#
# All strategies write a .mp4 to DOWNLOAD_DIR and return its Path.
# On total failure all strategies return None and log structured reasons.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import requests

from config import Config


log = logging.getLogger("Downloader")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_output(reel_id: str) -> Optional[Path]:
    """Return the largest file matching reel_id.* if it exceeds the minimum size."""
    candidates = sorted(
        Config.DOWNLOAD_DIR.glob(f"{reel_id}.*"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if candidates and candidates[0].stat().st_size > 10_000:
        return candidates[0]
    return None


def _is_cdninstagram_url(url: str) -> bool:
    return any(
        tok in url
        for tok in ("cdninstagram.com", "fbcdn.net", ".mp4", "video/mp4")
    )


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 1 — yt-dlp
# ─────────────────────────────────────────────────────────────────────────────

def download_ytdlp(reel_url: str, reel_id: str) -> Optional[Path]:
    """
    Download via yt-dlp.  Returns the output Path on success, None on failure.
    Failure reasons are logged at ERROR level with structured context.
    """
    out_template = str(Config.DOWNLOAD_DIR / f"{reel_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--format",              "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--output",              out_template,
        "--no-playlist",
        "--no-warnings",
        "--no-part",
        "--socket-timeout",      "30",
        "--retries",             "3",
        "--fragment-retries",    "5",
        "--file-access-retries", "3",
        "--concurrent-fragments","4",
        "--postprocessor-args",  "ffmpeg:-c:v copy -c:a aac",
        "--add-header",
            "User-Agent:Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "--add-header",          "Referer:https://www.instagram.com/",
        "--add-header",          "Accept-Language:en-US,en;q=0.9",
    ]
    if Config.COOKIES_FILE.exists():
        cmd += ["--cookies", str(Config.COOKIES_FILE)]
    cmd.append(reel_url)

    log.info(f"[yt-dlp] Starting download: {reel_id}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Config.DOWNLOAD_DIR),
        )
    except subprocess.TimeoutExpired:
        log.error(
            f"[yt-dlp] Timeout after 180s for reel_id={reel_id}",
            extra={"reel_id": reel_id, "strategy": "ytdlp", "reason": "timeout"},
        )
        return None
    except OSError as exc:
        # yt-dlp binary missing or not executable
        log.error(
            f"[yt-dlp] OS error launching process (is yt-dlp installed?): {exc}",
            extra={"reel_id": reel_id, "strategy": "ytdlp", "reason": "os_error"},
        )
        return None

    if result.returncode != 0:
        stderr_tail = result.stderr[-800:].strip()
        stdout_tail = result.stdout[-400:].strip()
        log.error(
            f"[yt-dlp] Non-zero exit {result.returncode} for reel_id={reel_id}\n"
            f"  STDERR: {stderr_tail}\n"
            f"  STDOUT: {stdout_tail}",
            extra={
                "reel_id": reel_id,
                "strategy": "ytdlp",
                "reason": "nonzero_exit",
                "returncode": result.returncode,
            },
        )
        return None

    path = _find_output(reel_id)
    if path:
        log.info(f"[yt-dlp] Success: {path.name} ({path.stat().st_size / 1024**2:.1f} MB)")
        return path

    log.error(
        f"[yt-dlp] Exit 0 but no valid output file found for reel_id={reel_id}",
        extra={"reel_id": reel_id, "strategy": "ytdlp", "reason": "no_output_file"},
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 2 — Browser network interception
# ─────────────────────────────────────────────────────────────────────────────

def download_via_interception(page, reel_url: str, reel_id: str) -> Optional[Path]:
    """
    Navigate to the reel page and capture the CDN video URL from network
    traffic, then download it directly with requests.

    page: a live Playwright Page object (must already be authenticated).

    The captured URL is a direct CDN link — no yt-dlp dependency required.
    Works as long as the browser can load the reel (i.e., session is valid).
    """
    captured: list[str] = []

    def _on_response(response):
        url = response.url
        try:
            status = response.status
        except Exception:
            return
        if status == 200 and _is_cdninstagram_url(url) and url not in captured:
            log.debug(f"[intercept] Captured candidate: {url[:120]}")
            captured.append(url)

    log.info(f"[intercept] Attaching response listener for {reel_id}")
    page.on("response", _on_response)
    try:
        try:
            page.goto(reel_url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            log.warning(f"[intercept] goto raised (non-fatal, may still capture): {exc}")

        # Wait up to 10 s for a video response to arrive
        deadline = time.time() + 10
        while time.time() < deadline:
            if captured:
                break
            time.sleep(0.25)
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception as exc:
            log.debug(f"[intercept] remove_listener failed (non-fatal): {exc}")

    if not captured:
        log.warning(
            f"[intercept] No CDN video URL captured for reel_id={reel_id}",
            extra={"reel_id": reel_id, "strategy": "intercept", "reason": "no_url_captured"},
        )
        return None

    video_url = captured[0]
    log.info(f"[intercept] Downloading from captured URL: {video_url[:100]}…")
    out_path = Config.DOWNLOAD_DIR / f"{reel_id}.mp4"

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.instagram.com/",
            "Accept": "video/mp4,video/*;q=0.9,*/*;q=0.8",
        }
        with requests.get(video_url, headers=headers, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
    except requests.HTTPError as exc:
        log.error(
            f"[intercept] HTTP error downloading captured URL: {exc}",
            extra={"reel_id": reel_id, "strategy": "intercept", "reason": "http_error"},
        )
        return None
    except requests.ConnectionError as exc:
        log.error(
            f"[intercept] Connection error: {exc}",
            extra={"reel_id": reel_id, "strategy": "intercept", "reason": "connection_error"},
        )
        return None
    except requests.Timeout:
        log.error(
            f"[intercept] Download timeout for reel_id={reel_id}",
            extra={"reel_id": reel_id, "strategy": "intercept", "reason": "timeout"},
        )
        return None
    except OSError as exc:
        log.error(
            f"[intercept] File write error: {exc}",
            extra={"reel_id": reel_id, "strategy": "intercept", "reason": "write_error"},
        )
        return None

    if out_path.exists() and out_path.stat().st_size > 10_000:
        size_mb = out_path.stat().st_size / 1024 ** 2
        log.info(f"[intercept] Success: {out_path.name} ({size_mb:.1f} MB)")
        return out_path

    log.error(
        f"[intercept] Output file missing or too small for reel_id={reel_id}",
        extra={"reel_id": reel_id, "strategy": "intercept", "reason": "output_too_small"},
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Strategy 3 — DOM video src extraction
# ─────────────────────────────────────────────────────────────────────────────

def download_via_src(page, reel_url: str, reel_id: str) -> Optional[Path]:
    """
    Read the <video src="..."> attribute directly from the Playwright page
    and download it via requests.  This is the lowest-quality fallback
    (typically a preview-quality stream) but is the most robust against
    yt-dlp extractor breakage.

    page: a live Playwright Page object, ideally already on reel_url.
    """
    log.info(f"[src-extract] Attempting video src extraction for {reel_id}")

    src: Optional[str] = None

    # Try several selectors in order; first non-empty src wins
    src_selectors = [
        "video[src]",
        "video[playsinline][src]",
        "main video",
        "article video",
        "video",
    ]
    for sel in src_selectors:
        try:
            el = page.query_selector(sel)
            if el:
                candidate = el.get_attribute("src") or ""
                if candidate.startswith("http"):
                    src = candidate
                    log.debug(f"[src-extract] Found src via selector {sel!r}")
                    break
        except Exception as exc:
            log.debug(f"[src-extract] Selector {sel!r} error: {exc}")

    if not src:
        # Last resort: evaluate JS to find any video src
        try:
            src = page.evaluate(
                "() => { "
                "  const v = document.querySelector('video'); "
                "  return (v && v.src && v.src.startsWith('http')) ? v.src : null; "
                "}"
            )
        except Exception as exc:
            log.debug(f"[src-extract] JS evaluation error: {exc}")

    if not src:
        log.warning(
            f"[src-extract] No video src found for reel_id={reel_id}",
            extra={"reel_id": reel_id, "strategy": "src_extract", "reason": "no_src"},
        )
        return None

    log.info(f"[src-extract] Downloading from src: {src[:100]}…")
    out_path = Config.DOWNLOAD_DIR / f"{reel_id}.mp4"

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.instagram.com/",
        }
        with requests.get(src, headers=headers, stream=True, timeout=120) as resp:
            resp.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
    except requests.RequestException as exc:
        log.error(
            f"[src-extract] Download error: {exc}",
            extra={"reel_id": reel_id, "strategy": "src_extract", "reason": "request_error"},
        )
        return None
    except OSError as exc:
        log.error(
            f"[src-extract] File write error: {exc}",
            extra={"reel_id": reel_id, "strategy": "src_extract", "reason": "write_error"},
        )
        return None

    if out_path.exists() and out_path.stat().st_size > 10_000:
        size_mb = out_path.stat().st_size / 1024 ** 2
        log.info(f"[src-extract] Success (preview quality): {out_path.name} ({size_mb:.1f} MB)")
        return out_path

    log.error(
        f"[src-extract] Output file missing or too small for reel_id={reel_id}",
        extra={"reel_id": reel_id, "strategy": "src_extract", "reason": "output_too_small"},
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def download_reel(reel_url: str, reel_id: str, page=None) -> tuple[Optional[Path], str]:
    """
    Attempt all three download strategies in priority order.

    Parameters
    ----------
    reel_url : Instagram reel URL
    reel_id  : short ID extracted from the URL
    page     : Playwright Page object (required for strategies 2 & 3)

    Returns
    -------
    (Path, strategy_name) on success, or (None, "all_failed") on total failure.
    """
    # Strategy 1 — yt-dlp (no page required)
    path = download_ytdlp(reel_url, reel_id)
    if path:
        return path, "ytdlp"

    if page is None:
        log.warning(
            f"[downloader] yt-dlp failed and no page object provided — "
            f"cannot attempt interception/src fallbacks for {reel_id}"
        )
        return None, "all_failed"

    log.info(f"[downloader] yt-dlp failed — trying browser interception for {reel_id}")

    # Strategy 2 — network interception
    path = download_via_interception(page, reel_url, reel_id)
    if path:
        return path, "interception"

    log.info(f"[downloader] Interception failed — trying DOM src extraction for {reel_id}")

    # Strategy 3 — DOM src attribute
    path = download_via_src(page, reel_url, reel_id)
    if path:
        return path, "src_extract"

    log.error(
        f"[downloader] All three strategies failed for reel_id={reel_id}",
        extra={"reel_id": reel_id, "strategy": "all_failed"},
    )
    return None, "all_failed"
