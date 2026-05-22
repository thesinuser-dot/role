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

    def send_video(
        self, video_path: Path, caption: str = "", parse_mode: str = "HTML"
    ) -> Optional[Dict]:
        if not video_path.exists():
            self.log.error(f"[Telegram] Video not found: {video_path}")
            return None
        size_mb = video_path.stat().st_size / (1024 * 1024)
        self.log.info(f"[Telegram] Sending video: {video_path.name} ({size_mb:.1f} MB)")
        if size_mb > Config.TELEGRAM_MAX_VIDEO_MB:
            self.log.warning(f"[Telegram] File {size_mb:.1f} MB exceeds limit — sending link only.")
            return self.send_message(f"Video too large ({size_mb:.1f} MB) to attach.\n{caption}")
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
        self, video_path: Path, reel_url: str, views: int, likes: int
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
        # Only delete the local file after Telegram confirms receipt.
        # Deleting first risks losing the only copy if the upload times out
        # mid-transfer or the API returns an error after a partial upload.
        if result is not None:
            try:
                video_path.unlink(missing_ok=True)
                self.log.info(f"Local video deleted after confirmed send: {video_path.name}")
            except OSError as exc:
                self.log.warning(f"Could not delete {video_path.name}: {exc}")
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
