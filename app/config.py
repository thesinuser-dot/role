#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# config.py — Central configuration
# All values are read from environment variables; hard defaults make the agent
# runnable without any env vars for local testing.
# ─────────────────────────────────────────────────────────────────────────────

import os
from pathlib import Path
from typing import List


class Config:
    # ── Instagram ──────────────────────────────────────────────────────────────
    INSTAGRAM_SESSION_COOKIES: str = os.environ.get("INSTAGRAM_SESSION_COOKIES", "")
    INSTAGRAM_REELS_URL: str = "https://www.instagram.com/reels/"

    # ── Viral thresholds ───────────────────────────────────────────────────────
    MIN_VIEWS: int = int(os.environ.get("MIN_VIEWS", "50000"))
    MIN_LIKES: int = int(os.environ.get("MIN_LIKES", "0"))

    # ── Gemini vision ──────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    GEMINI_MAX_DIM: int = int(os.environ.get("GEMINI_MAX_DIM", "720"))
    # How many times to retry a transient Gemini error before failing closed
    GEMINI_RETRIES: int = int(os.environ.get("GEMINI_RETRIES", "2"))

    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    TELEGRAM_API_BASE: str = "https://api.telegram.org/bot"
    TELEGRAM_MAX_VIDEO_MB: int = int(os.environ.get("TELEGRAM_MAX_VIDEO_MB", "49"))

    # ── Database ───────────────────────────────────────────────────────────────
    DB_PATH: str = os.environ.get("DB_PATH", "history.db")

    # ── Runtime limits ─────────────────────────────────────────────────────────
    MAX_RUNTIME_SECONDS: int = int(os.environ.get("MAX_RUNTIME_SECONDS", "480"))
    SHUTDOWN_BUFFER_SECONDS: int = int(os.environ.get("SHUTDOWN_BUFFER_SECONDS", "45"))
    TARGET_REELS_SCAN: int = int(os.environ.get("TARGET_REELS_SCAN", "35"))
    MAX_QUALIFIED_SEND: int = int(os.environ.get("MAX_QUALIFIED_SEND", "5"))

    # ── Retry / queue ──────────────────────────────────────────────────────────
    # How many times to retry a failed Telegram send (persisted across runs)
    MAX_UPLOAD_ATTEMPTS: int = int(os.environ.get("MAX_UPLOAD_ATTEMPTS", "3"))
    # How many times to retry a failed reel download within a single run
    MAX_DOWNLOAD_ATTEMPTS: int = int(os.environ.get("MAX_DOWNLOAD_ATTEMPTS", "2"))

    # ── Paths ──────────────────────────────────────────────────────────────────
    DOWNLOAD_DIR: Path = Path(os.environ.get("DOWNLOAD_DIR", "/tmp/reels_downloads"))
    SCREENSHOT_DIR: Path = Path(os.environ.get("SCREENSHOT_DIR", "/tmp/reels_screenshots"))
    COOKIES_FILE: Path = Path("/tmp/ig_cookies.txt")

    # ── Browser ────────────────────────────────────────────────────────────────
    HEADLESS: bool = os.environ.get("PLAYWRIGHT_HEADLESS", "false").strip().lower() == "true"
    VIEWPORT_W: int = 430
    VIEWPORT_H: int = 932

    USER_AGENTS: List[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    # ── Vision / black-bar detection ───────────────────────────────────────────
    BLACK_THRESHOLD: int = int(os.environ.get("BLACK_THRESHOLD", "28"))
    BLACK_BAR_RATIO: float = float(os.environ.get("BLACK_BAR_RATIO", "0.82"))
    BORDER_SAMPLE_PCT: float = float(os.environ.get("BORDER_SAMPLE_PCT", "0.05"))

    @classmethod
    def summary(cls) -> str:
        lines = [
            "+- Config ---------------------------------------------------",
            f"|  Max runtime        : {cls.MAX_RUNTIME_SECONDS}s (buffer {cls.SHUTDOWN_BUFFER_SECONDS}s)",
            f"|  Min views          : {cls.MIN_VIEWS:,}",
            f"|  Min likes          : {cls.MIN_LIKES:,}",
            f"|  Target scan count  : {cls.TARGET_REELS_SCAN}",
            f"|  Max send count     : {cls.MAX_QUALIFIED_SEND}",
            f"|  Max upload retry   : {cls.MAX_UPLOAD_ATTEMPTS}",
            f"|  Headless           : {cls.HEADLESS}",
            f"|  DB path            : {cls.DB_PATH}",
            f"|  Gemini model       : {cls.GEMINI_MODEL}",
            f"|  Gemini enabled     : {bool(cls.GEMINI_API_KEY)}",
            f"|  Telegram enabled   : {bool(cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_ID)}",
            f"|  Cookies set        : {bool(cls.INSTAGRAM_SESSION_COOKIES)}",
            "+------------------------------------------------------------",
        ]
        return "\n".join(lines)
