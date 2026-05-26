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
    MIN_VIEWS: int = int(os.environ.get("MIN_VIEWS", "0"))
    MIN_LIKES: int = int(os.environ.get("MIN_LIKES", "150000"))

    # ── Gemini vision ──────────────────────────────────────────────────────────
    GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
    GEMINI_MAX_DIM: int = int(os.environ.get("GEMINI_MAX_DIM", "720"))
    # How many times to retry a transient Gemini error before failing closed
    GEMINI_RETRIES: int = int(os.environ.get("GEMINI_RETRIES", "2"))

    # ── Gemini Web fallback (browser-based, no API key needed) ────────────────
    # Paste Google account cookies (JSON array or semicolon-separated) so the
    # agent can query gemini.google.com directly when the API key is absent or
    # quota-exhausted.
    GEMINI_COOKIES: str = os.environ.get("GEMINI_COOKIES", "")
    # Enable Gemini Web as a vision provider when API key is unavailable
    GEMINI_WEB_ENABLED: bool = os.environ.get("GEMINI_WEB_ENABLED", "true").strip().lower() == "true"
    # Manual login fallback — used when cookies are absent/expired
    GEMINI_EMAIL: str = os.environ.get("GEMINI_EMAIL", "")
    GEMINI_PASSWORD: str = os.environ.get("GEMINI_PASSWORD", "")

    # ── Human approval (Telegram inline buttons) ──────────────────────────────
    # When True, each reel that passes AI vision is sent as a screenshot to
    # Telegram with ✅ Approve and ⏭ Skip buttons before downloading.
    # When False (default), reels are downloaded and sent automatically.
    HUMAN_APPROVAL_ENABLED: bool = os.environ.get("HUMAN_APPROVAL_ENABLED", "false").strip().lower() == "true"
    # Seconds to wait for a human response before auto-approving or auto-skipping
    HUMAN_APPROVAL_TIMEOUT_S: int = int(os.environ.get("HUMAN_APPROVAL_TIMEOUT_S", "120"))
    # What to do when the timeout expires with no response: "approve" or "skip"
    HUMAN_APPROVAL_TIMEOUT_ACTION: str = os.environ.get("HUMAN_APPROVAL_TIMEOUT_ACTION", "approve").strip().lower()

    # ── Text / hashtag providers ───────────────────────────────────────────────
    # Provider order is automatic by default: first configured provider wins.
    AI_PROVIDER_ORDER: List[str] = [
        p.strip().lower()
        for p in os.environ.get("AI_PROVIDER_ORDER", "gemini,groq,openrouter")
        .split(",")
        if p.strip()
    ]
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    # Auto-cascade through best free Groq/LLaMA models — fastest available wins.
    # Override with GROQ_MODEL env var if you want a specific model.
    GROQ_MODEL: str = os.environ.get("GROQ_MODEL", "auto")
    # Ordered list of best free Groq models (tried in order, first success wins).
    GROQ_MODEL_CASCADE: List[str] = [
        m.strip()
        for m in os.environ.get(
            "GROQ_MODEL_CASCADE",
            "llama-3.3-70b-versatile,"
            "llama-3.1-70b-versatile,"
            "llama3-70b-8192,"
            "llama-3.1-8b-instant,"
            "llama3-8b-8192,"
            "gemma2-9b-it",
        ).split(",")
        if m.strip()
    ]
    OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
    # "auto" = use OpenRouter's free model router (always picks something available).
    # Override with OPENROUTER_MODEL env var for a specific model.
    OPENROUTER_MODEL: str = os.environ.get("OPENROUTER_MODEL", "auto")
    # Best free OpenRouter models tried in order (fallback cascade).
    OPENROUTER_MODEL_CASCADE: List[str] = [
        m.strip()
        for m in os.environ.get(
            "OPENROUTER_MODEL_CASCADE",
            "meta-llama/llama-3.3-70b-instruct:free,"
            "meta-llama/llama-3.1-8b-instruct:free,"
            "mistralai/mistral-7b-instruct:free,"
            "google/gemma-2-9b-it:free,"
            "qwen/qwen2.5-72b-instruct:free",
        ).split(",")
        if m.strip()
    ]
    OPENROUTER_APP_NAME: str = os.environ.get("OPENROUTER_APP_NAME", "Reels Hunter")
    
    # ── Gemini Fallback Mode (when API quota/limit is hit) ────────────────────
    # When True, if Gemini API fails due to quota/limits, fall back to using 
    # views/likes metrics to determine quality instead of rejecting the reel
    ENABLE_GEMINI_FALLBACK: bool = os.environ.get("ENABLE_GEMINI_FALLBACK", "true").strip().lower() == "true"
    # Minimum views required when falling back (if Gemini unavailable)
    FALLBACK_MIN_VIEWS: int = int(os.environ.get("FALLBACK_MIN_VIEWS", "500000"))
    # Minimum likes required when falling back (if Gemini unavailable)
    FALLBACK_MIN_LIKES: int = int(os.environ.get("FALLBACK_MIN_LIKES", "250000"))

    # ── Telegram ───────────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    TELEGRAM_API_BASE: str = "https://api.telegram.org/bot"
    TELEGRAM_MAX_VIDEO_MB: int = int(os.environ.get("TELEGRAM_MAX_VIDEO_MB", "49"))

    # ── TikTok ────────────────────────────────────────────────────────────────
    # Set TIKTOK_ENABLED=true to activate auto-posting after each Telegram send.
    TIKTOK_ENABLED: bool = os.environ.get("TIKTOK_ENABLED", "false").strip().lower() == "true"

    # Auth mode: "cookies" (default) or "session"
    #   cookies  — point TIKTOK_COOKIES_FILE at a Netscape cookies.txt you
    #              exported from a logged-in browser session.
    #   session  — paste the raw `sessionid` cookie value (or full cookie
    #              string) into TIKTOK_SESSION_COOKIES.
    TIKTOK_AUTH_MODE: str = os.environ.get("TIKTOK_AUTH_MODE", "cookies").strip().lower()
    TIKTOK_COOKIES_FILE: str = os.path.expandvars(os.path.expanduser(
        os.environ.get("TIKTOK_COOKIES_FILE", "~/.secrets/tiktok_cookies.txt")
    ))
    # Accepts either a bare sessionid value ("abc123") or a full cookie string
    # ("sessionid=abc123; tt_csrf_token=xyz; ...") — both are handled.
    TIKTOK_SESSION_COOKIES: str = os.environ.get("TIKTOK_SESSION_COOKIES", "")
    # Backwards-compatible alias for older docs/scripts.
    TIKTOK_SESSION_ID: str = os.environ.get("TIKTOK_SESSION_ID", "")
    # Manual login fallback — used when cookies are absent/expired
    TIKTOK_EMAIL: str = os.environ.get("TIKTOK_EMAIL", "")
    TIKTOK_PASSWORD: str = os.environ.get("TIKTOK_PASSWORD", "")

    # TikTok browser headless mode — defaults False (visible) for local use;
    # set TIKTOK_HEADLESS=true in CI/GitHub Actions to avoid display errors.
    TIKTOK_HEADLESS: bool = os.environ.get("TIKTOK_HEADLESS", "false").strip().lower() == "true"

    # Gemini Web browser is ALWAYS visible (headless=False) so you can see it.
    GEMINI_WEB_HEADLESS: bool = False

    # Retry budget for failed uploads within a single run
    TIKTOK_MAX_RETRIES: int = int(os.environ.get("TIKTOK_MAX_RETRIES", "2"))
    # Hard guard for uploads that hang on the page automation layer
    TIKTOK_UPLOAD_TIMEOUT_SECONDS: int = int(os.environ.get("TIKTOK_UPLOAD_TIMEOUT_SECONDS", "240"))

    # Caption template — use {url}, {views}, {likes}, {tags} placeholders.
    # Leave empty for the default short-caption mode.
    TIKTOK_CAPTION_TEMPLATE: str = os.environ.get("TIKTOK_CAPTION_TEMPLATE", "")

    # Hashtags appended to every TikTok post (comma-separated)
    TIKTOK_HASHTAGS: List[str] = [
        h.strip().lstrip("#")
        for h in os.environ.get(
            "TIKTOK_HASHTAGS",
            "fyp,viral,edit,trending,reels",
        ).split(",")
        if h.strip()
    ]

    # ── Backblaze B2 storage ───────────────────────────────────────────────────
    # Set these to persist downloaded reels beyond the GitHub Actions run.
    B2_APPLICATION_KEY_ID: str = os.environ.get("B2_APPLICATION_KEY_ID", "")
    B2_APPLICATION_KEY:    str = os.environ.get("B2_APPLICATION_KEY", "")
    B2_BUCKET_NAME:        str = os.environ.get("B2_BUCKET_NAME", "reels-hunter")

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
    VIEWPORT_W: int = int(os.environ.get("VIEWPORT_W", "430"))
    VIEWPORT_H: int = int(os.environ.get("VIEWPORT_H", "932"))

    USER_AGENTS: List[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    # ── Target accounts whitelist (Faceless / Edit accounts) ──────────────────
    # Add Instagram usernames you want the bot to scrape — one per line, no @.
    # Set via the USERS_ATTACK env-var (newline- or comma-separated) or add
    # them directly in the list below.  Leave empty to use the normal feed.
    USERS_ATTACK: List[str] = [
        u.strip().lstrip("@")
        for u in os.environ.get(
            "USERS_ATTACK",
            # ── Default seed accounts ──────────────────────────────────────
            # Add or remove usernames here:
            "\n".join([
                "thebl00dz",
                # "username2",
                # "username3",
            ])
        ).replace(",", "\n").splitlines()
        if u.strip()
    ]

    # ── Caption / hashtag content filter ──────────────────────────────────────
    # Words found in captions or hashtags that immediately disqualify a reel.
    CAPTION_BLACKLIST: List[str] = [
        w.strip()
        for w in os.environ.get(
            "CAPTION_BLACKLIST",
            "POV,Vlog,Day in my life,OOTD,Outfit of the day,GRWM,"
            "Get ready with me,Selfie,My girlfriend,My boyfriend,Travel vlog,"
            "storytime,come with me,day with me,morning routine,night routine",
        ).split(",")
        if w.strip()
    ]

    # Words found in captions or hashtags that mark a reel as a desirable Edit.
    CAPTION_WHITELIST: List[str] = [
        w.strip()
        for w in os.environ.get(
            "CAPTION_WHITELIST",
            "Movie edit,Scene pack,Anime edit,Car community,M5 f10,"
            "Sigma edit,Quote of the day,Relatable quotes,edit,cinematic,"
            "aesthetic,motivation,fyp edit,car edit",
        ).split(",")
        if w.strip()
    ]

    # ── Vision / black-bar detection ───────────────────────────────────────────
    BLACK_THRESHOLD: int = int(os.environ.get("BLACK_THRESHOLD", "28"))
    BLACK_BAR_RATIO: float = float(os.environ.get("BLACK_BAR_RATIO", "0.82"))
    BORDER_SAMPLE_PCT: float = float(os.environ.get("BORDER_SAMPLE_PCT", "0.05"))

    @classmethod
    def summary(cls) -> str:
        users_str = ", ".join(cls.USERS_ATTACK) if cls.USERS_ATTACK else "(feed mode)"
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
            f"|  Gemini Web enabled : {cls.GEMINI_WEB_ENABLED and bool(cls.GEMINI_COOKIES)}",
            f"|  Human approval     : {cls.HUMAN_APPROVAL_ENABLED}",
            f"|  Groq model         : {cls.GROQ_MODEL}",
            f"|  Groq enabled       : {bool(cls.GROQ_API_KEY)}",
            f"|  OpenRouter model   : {cls.OPENROUTER_MODEL}",
            f"|  OpenRouter enabled : {bool(cls.OPENROUTER_API_KEY)}",
            f"|  Provider order     : {', '.join(cls.AI_PROVIDER_ORDER)}",
            f"|  Gemini fallback    : {cls.ENABLE_GEMINI_FALLBACK}",
            f"|  Fallback min views : {cls.FALLBACK_MIN_VIEWS:,}",
            f"|  Fallback min likes : {cls.FALLBACK_MIN_LIKES:,}",
            f"|  Telegram enabled   : {bool(cls.TELEGRAM_BOT_TOKEN and cls.TELEGRAM_CHAT_ID)}",
            f"|  TikTok enabled     : {cls.TIKTOK_ENABLED}",
            f"|  TikTok auth mode   : {cls.TIKTOK_AUTH_MODE if cls.TIKTOK_ENABLED else chr(110)+chr(47)+chr(97)}",
            f"|  Cookies set        : {bool(cls.INSTAGRAM_SESSION_COOKIES)}",
            f"|  Target accounts    : {users_str}",
            f"|  Caption blacklist  : {len(cls.CAPTION_BLACKLIST)} words",
            f"|  Caption whitelist  : {len(cls.CAPTION_WHITELIST)} words",
            "+------------------------------------------------------------",
        ]
        return "\n".join(lines)
