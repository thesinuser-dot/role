#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# b2_uploader.py — Backblaze B2 video storage
#
# Uploads downloaded reels to B2 so they persist beyond the GitHub Actions run.
# Uses the b2sdk library (pip install b2sdk).
#
# Required env vars / GitHub secrets:
#   B2_APPLICATION_KEY_ID  — the key ID (starts with "005...")
#   B2_APPLICATION_KEY     — the full application key secret
#   B2_BUCKET_NAME         — name of your B2 bucket
#
# Usage:
#   uploader = B2Uploader()
#   url = uploader.upload(video_path, reel_id, views, likes)
#   # url is the friendly download URL, or None on failure
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("B2Uploader")


class B2Uploader:
    def __init__(self) -> None:
        self.key_id     = os.environ.get("B2_APPLICATION_KEY_ID", "").strip()
        self.app_key    = os.environ.get("B2_APPLICATION_KEY", "").strip()
        self.bucket_name = os.environ.get("B2_BUCKET_NAME", "reels-hunter").strip()
        self.enabled    = bool(self.key_id and self.app_key)
        self._api       = None
        self._bucket    = None

        if not self.enabled:
            log.info("B2 upload disabled — set B2_APPLICATION_KEY_ID + B2_APPLICATION_KEY to enable.")
            return

        try:
            from b2sdk.v2 import InMemoryAccountInfo, B2Api
            info = InMemoryAccountInfo()
            self._api = B2Api(info)
            self._api.authorize_account("production", self.key_id, self.app_key)
            self._bucket = self._api.get_bucket_by_name(self.bucket_name)
            log.info(f"B2 connected — bucket: {self.bucket_name} ✅")
        except ImportError:
            log.error("b2sdk not installed — run: pip install b2sdk. B2 upload disabled.")
            self.enabled = False
        except Exception as exc:
            log.error(f"B2 init failed: {exc} — upload disabled.")
            self.enabled = False

    def upload(
        self,
        video_path: Path,
        reel_id: str,
        views: int = 0,
        likes: int = 0,
    ) -> Optional[str]:
        """
        Upload video_path to B2.
        Returns the public download URL on success, None on failure.
        Never raises — B2 failure must never kill a successful Telegram send.
        """
        if not self.enabled:
            return None
        if not video_path or not video_path.exists():
            log.error(f"B2 upload skipped — file not found: {video_path}")
            return None

        # Store as  reels/<reel_id>/<timestamp>_<filename>
        remote_name = f"reels/{reel_id}/{int(time.time())}_{video_path.name}"
        file_info = {
            "reel_id": reel_id,
            "views":   str(views),
            "likes":   str(likes),
        }

        for attempt in range(1, 4):
            try:
                log.info(f"[B2] Uploading {video_path.name} → {remote_name} (attempt {attempt}/3)...")
                uploaded = self._bucket.upload_local_file(
                    local_file=str(video_path),
                    file_name=remote_name,
                    file_infos=file_info,
                )
                url = self._api.get_download_url_for_fileid(uploaded.id_)
                log.info(f"[B2] ✅ Upload succeeded: {url}")
                return url
            except Exception as exc:
                log.warning(f"[B2] Upload attempt {attempt}/3 failed: {exc}")
                if attempt < 3:
                    time.sleep(2 ** attempt)

        log.error(f"[B2] ❌ All upload attempts failed for {video_path.name}")
        return None
