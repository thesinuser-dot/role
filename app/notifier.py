#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# notifier.py — Telegram delivery layer
#
# Key correctness guarantee: send_qualified_reel() only deletes the local video
# file AFTER Telegram has confirmed receipt.  Deleting before confirmation would
# permanently lose the file on a mid-upload timeout or API error.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import subprocess
import tempfile
import requests

from config import Config


class NotificationService:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = str(chat_id).strip()
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.log = logging.getLogger("NotificationService")
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            self.log.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — notifications disabled."
            )

    # ── Core HTTP helper ──────────────────────────────────────────────────────

    def _request(
        self, method: str, endpoint: str, max_retries: int = 3, **kwargs: Any
    ) -> Optional[Dict]:
        if not self.enabled:
            return None
        url = f"{self.base_url}/{endpoint}"
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.request(method, url, timeout=90, **kwargs)
                if resp.status_code == 200:
                    return resp.json()
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    self.log.warning(f"Telegram rate-limited — sleeping {retry_after}s")
                    time.sleep(retry_after)
                    continue
                self.log.warning(
                    f"Telegram {endpoint} attempt {attempt}/{max_retries} "
                    f"— HTTP {resp.status_code}: {resp.text[:200]}"
                )
            except requests.RequestException as exc:
                self.log.warning(
                    f"Telegram request error (attempt {attempt}/{max_retries}): {exc}"
                )
            if attempt < max_retries:
                time.sleep(2 ** attempt)
        self.log.error(f"Telegram {endpoint} failed after {max_retries} attempts.")
        return None

    # ── Connection test ───────────────────────────────────────────────────────

    def test_connection(self) -> bool:
        """Verify Telegram credentials by calling getMe. Logs result and returns success bool."""
        if not self.enabled:
            self.log.warning("[Telegram] test_connection skipped — notifications disabled.")
            return False
        result = self._request("GET", "getMe")
        if result and result.get("ok"):
            bot_name = result.get("result", {}).get("username", "unknown")
            self.log.info(f"[Telegram] Connection OK — bot: @{bot_name}")
            return True
        self.log.error("[Telegram] test_connection failed — check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    # ── Public send methods ───────────────────────────────────────────────────

    def send_message(self, text: str, parse_mode: str = "HTML") -> Optional[Dict]:
        self.log.info(f"[Telegram] Sending message ({len(text)} chars)")
        return self._request(
            "POST", "sendMessage",
            data={
                "chat_id": self.chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )

    def _compress_video(self, video_path: Path) -> Optional[Path]:
        """
        Re-encode with ffmpeg (H.264 CRF 28, 720p max) into a temp file.
        Returns the compressed Path on success, None if ffmpeg fails.
        Called only when the file exceeds TELEGRAM_MAX_VIDEO_MB.
        """
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".mp4", prefix="compressed_", delete=False,
                dir=Config.DOWNLOAD_DIR,
            )
            tmp.close()
            out_path = Path(tmp.name)

            cmd = [
                "ffmpeg", "-y", "-i", str(video_path),
                "-c:v", "libx264",
                "-crf", "28",
                "-preset", "fast",
                "-vf", "scale='min(iw,720)':'min(ih,1280)':force_original_aspect_ratio=decrease",
                "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart",
                "-loglevel", "error",
                str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                self.log.warning(
                    f"[ffmpeg] Compression failed (rc={result.returncode}): "
                    f"{result.stderr.decode()[:300]}"
                )
                out_path.unlink(missing_ok=True)
                return None

            compressed_mb = out_path.stat().st_size / (1024 * 1024)
            original_mb = video_path.stat().st_size / (1024 * 1024)
            self.log.info(
                f"[ffmpeg] Compressed {original_mb:.1f} MB → {compressed_mb:.1f} MB"
            )
            return out_path
        except Exception as exc:
            self.log.warning(f"[ffmpeg] Compression exception: {exc}")
            return None

    def _upload_video(self, video_path: Path, caption: str, parse_mode: str) -> Optional[Dict]:
        """Send a single video file via Telegram sendVideo."""
        with open(video_path, "rb") as fh:
            return self._request(
                "POST", "sendVideo",
                data={
                    "chat_id": self.chat_id,
                    "caption": caption[:1024],
                    "parse_mode": parse_mode,
                    "supports_streaming": True,
                },
                files={"video": (video_path.name, fh, "video/mp4")},
            )

    def send_video(
        self, video_path: Path, caption: str = "", parse_mode: str = "HTML"
    ) -> Optional[Dict]:
        if not video_path.exists():
            self.log.error(f"[Telegram] Video not found: {video_path}")
            return None

        size_mb = video_path.stat().st_size / (1024 * 1024)
        self.log.info(f"[Telegram] Sending video: {video_path.name} ({size_mb:.1f} MB)")

        # ── File fits within Telegram limit — send directly ───────────────
        if size_mb <= Config.TELEGRAM_MAX_VIDEO_MB:
            return self._upload_video(video_path, caption, parse_mode)

        # ── File too large — try ffmpeg compression ────────────────────────
        self.log.warning(
            f"[Telegram] {size_mb:.1f} MB > {Config.TELEGRAM_MAX_VIDEO_MB} MB limit "
            f"— compressing with ffmpeg..."
        )
        compressed_path = self._compress_video(video_path)

        if compressed_path is None:
            # Compression failed entirely — send link as last resort
            self.log.error("[Telegram] Compression failed — falling back to link.")
            return self.send_message(
                f"⚠️ Video too large to compress ({size_mb:.1f} MB).\n{caption}"
            )

        compressed_mb = compressed_path.stat().st_size / (1024 * 1024)
        if compressed_mb > Config.TELEGRAM_MAX_VIDEO_MB:
            # Still too big after compression
            compressed_path.unlink(missing_ok=True)
            self.log.error(
                f"[Telegram] Compressed file still {compressed_mb:.1f} MB — sending link."
            )
            return self.send_message(
                f"⚠️ Video still too large after compression ({compressed_mb:.1f} MB).\n{caption}"
            )

        # Send the compressed file, then clean it up regardless of outcome
        try:
            result = self._upload_video(compressed_path, caption, parse_mode)
        finally:
            compressed_path.unlink(missing_ok=True)

        return result

    def send_photo(
        self, photo_data: bytes, caption: str = "", parse_mode: str = "HTML"
    ) -> Optional[Dict]:
        self.log.info("[Telegram] Sending photo...")
        return self._request(
            "POST", "sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": parse_mode},
            files={"photo": ("screenshot.jpg", photo_data, "image/jpeg")},
        )

    def send_document(self, data: bytes, filename: str, caption: str = "") -> Optional[Dict]:
        return self._request(
            "POST", "sendDocument",
            data={"chat_id": self.chat_id, "caption": caption[:1024]},
            files={"document": (filename, data, "application/octet-stream")},
        )

    def send_qualified_reel(
        self, video_path: Path, reel_url: str, views: int, likes: int,
        preserve_file: bool = False,
    ) -> bool:
        caption = (
            "<b>Viral Reel Captured!</b>\n\n"
            f"<b>Source:</b> {reel_url}\n"
            f"<b>Views:</b> {views:,}\n"
            f"<b>Likes:</b> {likes:,}\n"
            f"<b>Captured:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "#ReelsHunter #Viral #ContentHarvest"
        )
        result = self.send_video(video_path, caption)
        # Only delete the local file after Telegram confirms receipt — unless
        # preserve_file=True, which means the caller (agent) needs the file
        # for a subsequent TikTok upload and will handle deletion itself.
        if result is not None and not preserve_file:
            try:
                video_path.unlink(missing_ok=True)
                self.log.info(f"Local video deleted after confirmed send: {video_path.name}")
            except OSError as exc:
                self.log.warning(f"Could not delete {video_path.name}: {exc}")
        elif result is not None and preserve_file:
            self.log.info(
                f"Telegram send confirmed — preserving local file for TikTok: {video_path.name}"
            )
        else:
            self.log.warning(
                f"Telegram send failed — keeping local file for retry: {video_path}"
            )
        return result is not None

    def send_crash_alert(
        self, error_text: str, screenshot_bytes: Optional[bytes] = None
    ) -> None:
        self.log.error("[Telegram] Dispatching crash alert...")
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        short_caption = (
            f"<b>CRASH ALERT</b> — {timestamp}\n\n"
            f"<pre>{error_text[:900]}</pre>"
        )
        if screenshot_bytes:
            self.send_photo(screenshot_bytes, caption=short_caption)
        else:
            self.send_message(short_caption)
        if len(error_text) > 200:
            self.send_document(
                error_text.encode(),
                filename=f"crash_{int(time.time())}.txt",
                caption="Full traceback attached",
            )

    def send_run_summary(
        self, elapsed_s: float, scanned: int, sent: int, db_stats: Dict
    ) -> None:
        text = (
            "<b>Reels Hunter — Run Complete</b>\n\n"
            f"<b>Duration:</b> {elapsed_s:.0f}s\n"
            f"<b>Scanned this run:</b> {scanned}\n"
            f"<b>Sent this run:</b> {sent}\n\n"
            "<b>All-time DB stats:</b>\n"
            f"  Total processed : {int(db_stats.get('total', 0)):,}\n"
            f"  Downloaded      : {int(db_stats.get('downloaded', 0)):,}\n"
            f"  Skipped         : {int(db_stats.get('skipped', 0)):,}\n"
            f"  Errors          : {int(db_stats.get('errors', 0)):,}\n\n"
            f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.send_message(text)

    def send_debug(
        self, text: str, screenshot_bytes: Optional[bytes] = None
    ) -> None:
        """Non-critical debug message — errors are swallowed so they never block the agent."""
        try:
            prefix = "🐛 <b>DEBUG</b>\n"
            if screenshot_bytes:
                self.send_photo(screenshot_bytes, caption=(prefix + text)[:1024])
            else:
                self.send_message(prefix + text[:3900])
        except Exception as exc:
            self.log.debug(f"send_debug swallowed error: {exc}")
