#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# agent.py — Orchestrator
#
# Changes (2026-05):
#   • RunStats — fresh stats dataclass replacing ad-hoc scanned/sent counters
#   • Command priority — _drain_cmd_queue() called inside the hunt loop so
#     /help /status /stats /skip /set* are handled immediately, not blocked
#     until the hunt finishes
#   • USERS_ATTACK — hunt targets specific accounts when the list is populated
#   • Caption filtering — pre-checks reel caption/hashtags against blacklist /
#     whitelist before spending a screenshot + Gemini call on it
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeout,
    Error as PlaywrightError,
)

from config import Config
from database import DatabaseManager
from browser import BrowserManager
from collector import ReelCollector, SelectorRegistry
from vision import VisionEvaluator
from notifier import NotificationService
from downloader import download_reel
from pipeline import WorkQueue, ReelTask, ReelStatus, FailureKind


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _build_logger() -> logging.Logger:
    fmt = "%(asctime)s ***%(levelname)-7s*** %(name)-22s | %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(fmt, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(h)
    for noisy in ("urllib3", "httpcore", "httpx", "google.api_core", "google.auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("ReelsHunter")


log = _build_logger()


# ─────────────────────────────────────────────────────────────────────────────
# RunStats — single source of truth for all statistics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunStats:
    """
    Tracks statistics for ONE hunt run.
    All counters are updated in-place by _process_task; nothing is scattered
    across the agent as bare int fields anymore.
    """
    scanned:         int = 0   # total reels pulled from queue
    sent:            int = 0   # successfully sent to Telegram
    skipped_thresh:  int = 0   # below view/like threshold
    skipped_caption: int = 0   # rejected by caption blacklist
    skipped_vision:  int = 0   # rejected by Gemini / pixel check
    dedup:           int = 0   # already in DB
    download_fail:   int = 0   # yt-dlp / interception failed
    send_fail:       int = 0   # Telegram delivery failed
    errors:          int = 0   # unexpected exceptions

    def record(self, task: ReelTask) -> None:
        """Update counters from a finished task."""
        self.scanned += 1
        if task.status == ReelStatus.DOWNLOADED:
            self.sent += 1
        elif task.status == ReelStatus.FAILED:
            kind = task.failure_kind
            if kind == FailureKind.DOWNLOAD:
                self.download_fail += 1
            elif kind == FailureKind.SEND:
                self.send_fail += 1
            else:
                self.errors += 1
        elif task.status == ReelStatus.SKIPPED:
            reason = (task.failure_reason or "").lower()
            if "vision" in reason or "gemini" in reason or "stage" in reason:
                self.skipped_vision += 1
            elif "caption" in reason or "blacklist" in reason or "whitelist" in reason:
                self.skipped_caption += 1
            else:
                self.skipped_thresh += 1

    def telegram_summary(self, elapsed: float, db_stats: Dict[str, int]) -> str:
        m, s = divmod(int(elapsed), 60)
        lines = [
            "📊 <b>Hunt Summary</b>",
            f"⏱ Duration  : {m}m {s}s",
            "",
            "── This run ──────────────────",
            f"🔍 Scanned        : {self.scanned}",
            f"✅ Sent           : {self.sent}",
            f"⏭ Below threshold: {self.skipped_thresh}",
            f"📝 Caption filter : {self.skipped_caption}",
            f"👁 Vision rejected: {self.skipped_vision}",
            f"⬇ DL failures    : {self.download_fail}",
            f"📤 Send failures  : {self.send_fail}",
            f"💥 Errors         : {self.errors}",
            "",
            "── All-time DB ───────────────",
            f"📦 Total processed: {db_stats.get('total', 0):,}",
            f"✅ Downloaded     : {db_stats.get('downloaded', 0):,}",
            f"⏭ Skipped        : {db_stats.get('skipped', 0):,}",
            f"💥 Errors         : {db_stats.get('errors', 0):,}",
        ]
        return "\n".join(lines)

    def telegram_stats_cmd(self, db_stats: Dict[str, int]) -> str:
        """Used by /stats command — full breakdown."""
        lines = [
            "📊 <b>All-time Statistics</b>\n",
            f"Total processed : {db_stats.get('total', 0):,}",
            f"Downloaded      : {db_stats.get('downloaded', 0):,}",
            f"Skipped         : {db_stats.get('skipped', 0):,}",
            f"Errors          : {db_stats.get('errors', 0):,}",
            "",
            "<b>This session</b>",
            f"Scanned         : {self.scanned}",
            f"Sent            : {self.sent}",
            f"Below threshold : {self.skipped_thresh}",
            f"Caption filter  : {self.skipped_caption}",
            f"Vision rejected : {self.skipped_vision}",
            f"DL failures     : {self.download_fail}",
            f"Send failures   : {self.send_fail}",
        ]
        return "\n".join(lines)

    def log_summary(self) -> str:
        return (
            f"scanned={self.scanned} sent={self.sent} "
            f"thresh_skip={self.skipped_thresh} caption_skip={self.skipped_caption} "
            f"vision_skip={self.skipped_vision} dl_fail={self.download_fail} "
            f"send_fail={self.send_fail} errors={self.errors}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Telegram command poller (daemon thread)
# ─────────────────────────────────────────────────────────────────────────────

class TelegramCommandPoller:
    HELP_TEXT = (
        "<b>🤖 Reels Hunter — Bot Commands</b>\n\n"
        "/help — show this message\n"
        "/start — trigger a new hunt run now\n"
        "/restart — restart the agent process\n"
        "/status — current run state &amp; uptime\n"
        "/stats — all-time DB statistics\n"
        "/test &lt;url&gt; — force-download one reel (skips AI &amp; dedup)\n"
        "/startdisplay — raise Chromium window on Xpra desktop\n"
        "   <i>aliases: /desktop  /resumerdp</i>\n"
        "/setviews &lt;n&gt; — set minimum view count\n"
        "/setscans &lt;n&gt; — set reels-to-scan per run\n"
        "/setsend &lt;n&gt; — set max reels sent per run\n"
        "/skip — stop collecting reels now; proceed to download + Gemini stage\n"
    )

    def __init__(self, bot_token: str, chat_id: str, cmd_queue: queue.Queue):
        self.bot_token = bot_token
        self.chat_id   = str(chat_id).strip()
        self.base_url  = f"https://api.telegram.org/bot{bot_token}"
        self.cmd_queue = cmd_queue
        self.log       = logging.getLogger("CmdPoller")
        self.enabled   = bool(bot_token and chat_id)
        self._stop     = threading.Event()
        self._offset   = 0
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not self.enabled:
            self.log.warning("Command poller disabled (no Telegram credentials).")
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="CmdPoller")
        self._thread.start()
        self.log.info("Telegram command poller started.")

    def stop(self) -> None:
        self._stop.set()

    def reply(self, text: str) -> None:
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            self.log.warning(f"Reply failed: {exc}")

    def _get_updates(self) -> List[Dict]:
        try:
            resp = requests.get(
                f"{self.base_url}/getUpdates",
                params={
                    "offset": self._offset,
                    "timeout": 20,
                    "allowed_updates": ["message"],
                },
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
            self.log.debug(f"getUpdates HTTP {resp.status_code}")
        except requests.Timeout:
            self.log.debug("getUpdates timed out (non-fatal, will retry)")
        except requests.ConnectionError as exc:
            self.log.debug(f"getUpdates connection error (non-fatal): {exc}")
        except requests.RequestException as exc:
            self.log.warning(f"getUpdates unexpected error: {exc}")
        return []

    def _poll_loop(self) -> None:
        self.log.info("Polling loop running...")
        while not self._stop.is_set():
            updates = self._get_updates()
            for upd in updates:
                self._offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = (msg.get("text") or "").strip()
                if not text.startswith("/"):
                    continue
                from_chat = str(msg.get("chat", {}).get("id", ""))
                if from_chat != self.chat_id:
                    self.log.debug(f"Ignoring message from unknown chat {from_chat}")
                    continue
                self.log.info(f"Command received: {text!r}")
                parts = text.split(None, 1)
                cmd   = parts[0].lower().split("@")[0]
                arg   = parts[1].strip() if len(parts) > 1 else ""
                self.cmd_queue.put({"cmd": cmd, "arg": arg})
            self._stop.wait(2)


# ─────────────────────────────────────────────────────────────────────────────
# Screenshot helper
# ─────────────────────────────────────────────────────────────────────────────

def _capture_reel_screenshot(page: Page, reel_id: str) -> Optional[bytes]:
    el = None
    for sel in SelectorRegistry.VIDEO_ELEMENT:
        try:
            candidate = page.query_selector(sel)
            if candidate and candidate.is_visible():
                el = candidate
                break
        except PlaywrightError as exc:
            log.debug(f"[{reel_id}] Screenshot selector {sel!r} error: {exc}")

    if el:
        raw = el.screenshot(type="jpeg", quality=90)
    else:
        log.debug(f"[{reel_id}] No visible <video> — using viewport screenshot.")
        raw = page.screenshot(
            type="jpeg", quality=90,
            clip={"x": 0, "y": 0, "width": Config.VIEWPORT_W, "height": Config.VIEWPORT_H},
        )

    ts   = int(time.time())
    path = Config.SCREENSHOT_DIR / f"{reel_id}_{ts}.jpg"
    try:
        path.write_bytes(raw)
    except OSError as exc:
        log.warning(f"[{reel_id}] Could not save screenshot to disk (non-fatal): {exc}")

    log.debug(f"[{reel_id}] Screenshot captured ({len(raw) // 1024} KB)")
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Instagram Agent
# ─────────────────────────────────────────────────────────────────────────────

class InstagramAgent:
    def __init__(self):
        self.log        = logging.getLogger("InstagramAgent")
        self.start_time = time.monotonic()

        self.db       = DatabaseManager(Config.DB_PATH)
        self.bm       = BrowserManager()
        self.vision   = VisionEvaluator(Config.GEMINI_API_KEY)
        self.notifier = NotificationService(Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID)
        self.wq       = WorkQueue()

        self.collector: Optional[ReelCollector] = None

        self._run_id: Optional[int] = None

        # ── Stats (rebuilt from scratch each session) ──────────────────────
        # session_stats accumulates across ALL hunt runs in one agent process.
        # Each hunt also creates its own local RunStats for per-run summaries.
        self.session_stats = RunStats()

        self._stop    = False
        self._hunting = False
        self._skip_collection = False

        self._cmd_queue: queue.Queue = queue.Queue()
        self._poller = TelegramCommandPoller(
            Config.TELEGRAM_BOT_TOKEN,
            Config.TELEGRAM_CHAT_ID,
            self._cmd_queue,
        )

        Config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        Config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT,  self._on_signal)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _on_signal(self, signum: int, _frame: Any) -> None:
        self.log.warning(f"Signal {signum} received — requesting graceful stop.")
        self._stop = True

    def _elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def _deadline_approaching(self) -> bool:
        hard = Config.MAX_RUNTIME_SECONDS - Config.SHUTDOWN_BUFFER_SECONDS
        elapsed = self._elapsed()
        if elapsed >= hard:
            self.log.warning(f"Deadline approaching: {elapsed:.0f}s / {hard}s — stopping.")
            return True
        return False

    # ── Caption / hashtag pre-filter ──────────────────────────────────────────

    @staticmethod
    def _caption_passes(caption: str) -> tuple[bool, str]:
        """
        Check caption/hashtags against the blacklist and whitelist.
        Returns (passes: bool, reason: str).
        Blacklist wins over whitelist.
        """
        if not caption:
            return True, "no caption"

        lower = caption.lower()

        for word in Config.CAPTION_BLACKLIST:
            if word.lower() in lower:
                return False, f"caption blacklist: '{word}'"

        # Whitelist is advisory (not required) — if populated, reel must match at
        # least one whitelist term.  If whitelist is empty we skip this check.
        if Config.CAPTION_WHITELIST:
            for word in Config.CAPTION_WHITELIST:
                if word.lower() in lower:
                    return True, f"caption whitelist match: '{word}'"
            # No whitelist hit — neutral (don't reject, let vision decide)
            return True, "caption neutral (no whitelist match)"

        return True, "caption ok"

    # ── Pending-upload retry ──────────────────────────────────────────────────

    def _retry_pending_uploads(self) -> int:
        pending = self.db.get_pending_uploads(Config.MAX_UPLOAD_ATTEMPTS)
        if not pending:
            return 0

        self.log.info(f"Retrying {len(pending)} pending upload(s) from previous run(s)...")
        self.notifier.send_message(
            f"♻️ <b>Resuming {len(pending)} pending upload(s)</b> from last run..."
        )
        delivered = 0

        for row in pending:
            video_path = Path(row["video_path"])

            if not video_path.exists():
                self.log.warning(
                    f"Pending upload file missing — removing from queue: "
                    f"reel_id={row['reel_id']}  path={video_path}"
                )
                self.db.remove_pending_upload(row["reel_id"])
                continue

            self.log.info(
                f"Retrying pending upload reel_id={row['reel_id']} "
                f"(attempt {row['attempts'] + 1}/{Config.MAX_UPLOAD_ATTEMPTS})"
            )
            self.db.increment_upload_attempt(row["reel_id"])

            ok = self.notifier.send_qualified_reel(
                video_path, row["url"], row["views"], row["likes"]
            )
            if ok:
                self.db.remove_pending_upload(row["reel_id"])
                self.db.mark_processed(
                    row["reel_id"], row["url"], "downloaded",
                    row["views"], row["likes"]
                )
                delivered += 1
                self.log.info(f"Pending upload delivered: {row['reel_id']}")
            else:
                self.log.warning(
                    f"Pending upload still failing: reel_id={row['reel_id']} "
                    f"(will retry on next run if attempts < {Config.MAX_UPLOAD_ATTEMPTS})"
                )

        if delivered:
            self.log.info(f"Pending retry: {delivered}/{len(pending)} delivered.")
        return delivered

    # ── Setup ─────────────────────────────────────────────────────────────────

    def _switch_to_rdp_display_if_available(self) -> bool:
        import subprocess as _sp
        import glob as _glob

        current = os.environ.get("DISPLAY", "")
        try:
            num = int(current.lstrip(":"))
            if num >= 10:
                self.log.info(f"DISPLAY={current} is already the RDP session — good.")
                return True
        except (ValueError, AttributeError):
            pass

        try:
            sockets = _glob.glob("/tmp/.X11-unix/X*")
            rdp_sockets = sorted(
                [s for s in sockets
                 if s.replace("/tmp/.X11-unix/X", "").isdigit()
                 and int(s.replace("/tmp/.X11-unix/X", "")) >= 10]
            )
            if rdp_sockets:
                num_str = rdp_sockets[0].replace("/tmp/.X11-unix/X", "")
                rdp_display = f":{num_str}"
                os.environ["DISPLAY"] = rdp_display
                self.log.info(
                    f"Switched DISPLAY from {current!r} → {rdp_display} (RDP session)"
                )
                return True
        except Exception as exc:
            self.log.debug(f"RDP socket scan failed: {exc}")

        self.log.info(f"No RDP display found — using DISPLAY={current or ':99'} (Xvfb)")
        return False

    def setup(self) -> bool:
        self.log.info("\n" + Config.summary())

        try:
            self.db.initialize()
        except Exception:
            self.log.critical(f"Database init failed:\n{traceback.format_exc()}")
            return False

        try:
            self.bm.write_netscape_cookies(Config.INSTAGRAM_SESSION_COOKIES)
        except Exception:
            self.log.warning(
                f"Failed to write Netscape cookies file "
                f"(yt-dlp will run without auth):\n{traceback.format_exc()}"
            )

        self._switch_to_rdp_display_if_available()

        try:
            self.bm.launch()
        except Exception:
            tb = traceback.format_exc()
            self.log.critical(f"Browser launch failed:\n{tb}")
            self.notifier.send_crash_alert(f"Browser launch failure:\n{tb}")
            return False

        self.collector = ReelCollector(self.bm)

        try:
            self._retry_pending_uploads()
        except Exception:
            self.log.warning(
                f"Pending upload retry raised an exception (non-fatal):\n"
                f"{traceback.format_exc()}"
            )

        try:
            self._run_id = self.db.start_run()
        except Exception as exc:
            self.log.warning(f"Could not start run record (non-fatal): {exc}")

        return True

    # ── Per-reel state machine ────────────────────────────────────────────────

    def _process_task(
        self,
        task: ReelTask,
        run_stats: RunStats,
        force: bool = False,
        skip_vision: bool = False,
    ) -> None:
        """
        Drive one ReelTask through the full pipeline.
        Updates task.status in-place and records outcome into run_stats.
        No exception is silently swallowed.
        """
        reel_id  = task.reel_id
        reel_url = task.url
        sep      = "-" * 56
        self.log.info(f"\n{sep}\n  Processing: {reel_id}\n  URL: {reel_url}\n{sep}")

        page: Page = self.bm.page

        # ── 1. Navigate ───────────────────────────────────────────────────────
        try:
            page.goto(reel_url, wait_until="domcontentloaded", timeout=20_000)
            self.bm.delay(2000, 4000)
            self.collector.dismiss_popups()
        except PlaywrightTimeout as exc:
            task.mark_retry(f"Navigation timeout: {exc}", FailureKind.TRANSIENT)
            self.log.warning(f"[{reel_id}] Navigation timeout (retryable): {exc}")
            self.db.mark_processed(reel_id, reel_url, "error", 0, 0, str(exc)[:200])
            run_stats.record(task)
            return
        except PlaywrightError as exc:
            task.mark_retry(f"Navigation Playwright error: {exc}", FailureKind.TRANSIENT)
            self.log.error(f"[{reel_id}] Navigation Playwright error: {exc}")
            self.db.mark_processed(reel_id, reel_url, "error", 0, 0, str(exc)[:200])
            run_stats.record(task)
            return

        try:
            page.wait_for_selector("video", timeout=10_000)
        except PlaywrightTimeout:
            self.log.warning(f"[{reel_id}] No <video> element within 10s — continuing.")
        except PlaywrightError as exc:
            self.log.warning(f"[{reel_id}] wait_for_selector error (non-fatal): {exc}")

        self.bm.delay(1000, 1500)

        # ── 2. Metrics ────────────────────────────────────────────────────────
        try:
            metrics = self.collector.extract_metrics()
        except PlaywrightError as exc:
            task.mark_retry(f"Metrics Playwright error: {exc}", FailureKind.TRANSIENT)
            self.log.error(f"[{reel_id}] Metrics extraction Playwright error: {exc}")
            self.db.mark_processed(reel_id, reel_url, "error", 0, 0, str(exc)[:200])
            run_stats.record(task)
            return
        except Exception as exc:
            task.mark_failed(f"Metrics unexpected error: {exc}", FailureKind.UNKNOWN)
            self.log.error(f"[{reel_id}] Metrics unexpected error:\n{traceback.format_exc()}")
            self.db.mark_processed(reel_id, reel_url, "error", 0, 0, str(exc)[:200])
            run_stats.record(task)
            return

        views = metrics["views"]
        likes = metrics["likes"]
        task.views = views
        task.likes = likes

        # ── 3. View / like threshold ──────────────────────────────────────────
        if not force:
            if Config.MIN_LIKES > 0 and likes < Config.MIN_LIKES:
                reason = f"Likes {likes:,} < threshold {Config.MIN_LIKES:,}"
                task.mark_skipped(reason, FailureKind.PERMANENT)
                self.log.info(f"[{reel_id}] SKIP: {reason}")
                self.db.mark_processed(reel_id, reel_url, "skipped", views, likes, reason)
                run_stats.record(task)
                return
            if Config.MIN_VIEWS > 0 and views < Config.MIN_VIEWS:
                reason = f"Views {views:,} < threshold {Config.MIN_VIEWS:,}"
                task.mark_skipped(reason, FailureKind.PERMANENT)
                self.log.info(f"[{reel_id}] SKIP: {reason}")
                self.db.mark_processed(reel_id, reel_url, "skipped", views, likes, reason)
                run_stats.record(task)
                return

        self.log.info(f"[{reel_id}] Metrics passed — views={views:,}  likes={likes:,}")

        # ── 4. Caption filter (before expensive vision call) ──────────────────
        if not force:
            caption = metrics.get("caption", "")
            ok, cap_reason = self._caption_passes(caption)
            if not ok:
                reason = f"Caption filter: {cap_reason}"
                task.mark_skipped(reason, FailureKind.PERMANENT)
                self.log.info(f"[{reel_id}] SKIP ({reason})")
                self.db.mark_processed(reel_id, reel_url, "skipped", views, likes, reason)
                run_stats.record(task)
                return
            self.log.info(f"[{reel_id}] Caption OK: {cap_reason}")

        # ── 5. Vision ─────────────────────────────────────────────────────────
        if not skip_vision:
            screenshot: Optional[bytes] = None
            try:
                screenshot = _capture_reel_screenshot(page, reel_id)
            except PlaywrightError as exc:
                self.log.error(f"[{reel_id}] Screenshot Playwright error: {exc}")
            except Exception as exc:
                self.log.error(f"[{reel_id}] Screenshot unexpected error: {exc}")

            if not screenshot:
                task.mark_failed("Screenshot capture failed", FailureKind.VISION)
                self.db.mark_processed(reel_id, reel_url, "error", views, likes, "screenshot_failed")
                run_stats.record(task)
                return

            try:
                vision_ok, vision_reason = self.vision.evaluate(screenshot)
            except Exception as exc:
                self.log.error(
                    f"[{reel_id}] Vision evaluate raised exception (fail-closed): "
                    f"{exc}\n{traceback.format_exc()}"
                )
                task.mark_failed(f"Vision exception: {exc}", FailureKind.VISION)
                self.db.mark_processed(reel_id, reel_url, "skipped", views, likes, f"vision_exception:{exc}")
                run_stats.record(task)
                return

            if not vision_ok:
                task.mark_skipped(vision_reason, FailureKind.VISION)
                self.log.info(f"[{reel_id}] SKIP (vision): {vision_reason}")
                self.db.mark_processed(reel_id, reel_url, "skipped", views, likes, f"vision:{vision_reason}")
                run_stats.record(task)
                return

            self.log.info(f"[{reel_id}] Vision passed: {vision_reason}")
        else:
            self.log.info(f"[{reel_id}] Vision SKIPPED (test mode)")

        # ── 6. Download ───────────────────────────────────────────────────────
        self.log.info(f"[{reel_id}] All gates cleared — downloading...")
        video_path, strategy = download_reel(reel_url, reel_id, page=page)

        if not video_path:
            task.mark_retry("All download strategies failed", FailureKind.DOWNLOAD)
            self.log.error(f"[{reel_id}] Download failed (all strategies exhausted)")
            self.db.mark_processed(reel_id, reel_url, "download_failed", views, likes, "all_strategies_failed")
            run_stats.record(task)
            return

        self.log.info(f"[{reel_id}] Downloaded via strategy={strategy}")

        # ── 7. Register pending BEFORE Telegram upload (crash-safety) ─────────
        try:
            self.db.add_pending_upload(reel_id, reel_url, video_path, views, likes)
        except Exception as exc:
            self.log.warning(
                f"[{reel_id}] Could not register pending upload "
                f"(send will still be attempted): {exc}"
            )

        # ── 8. Telegram send ──────────────────────────────────────────────────
        self.log.info(f"[{reel_id}] Sending to Telegram...")
        try:
            sent = self.notifier.send_qualified_reel(video_path, reel_url, views, likes)
        except Exception as exc:
            self.log.error(
                f"[{reel_id}] Telegram send raised exception: "
                f"{exc}\n{traceback.format_exc()}"
            )
            task.mark_retry(f"Telegram send exception: {exc}", FailureKind.SEND)
            self.db.mark_processed(reel_id, reel_url, "telegram_failed", views, likes, str(exc)[:200])
            run_stats.record(task)
            return

        if sent:
            try:
                self.db.remove_pending_upload(reel_id)
            except Exception as exc:
                self.log.warning(f"[{reel_id}] Could not clear pending upload entry: {exc}")
            task.mark_downloaded(video_path)
            self.log.info(f"[{reel_id}] Delivered to Telegram (strategy={strategy})")
            self.db.mark_processed(reel_id, reel_url, "downloaded", views, likes)
        else:
            task.mark_retry("Telegram delivery returned False", FailureKind.SEND)
            self.log.error(f"[{reel_id}] Telegram delivery failed — keeping local file for next run")
            self.db.mark_processed(reel_id, reel_url, "telegram_failed", views, likes, "delivery_failed")

        run_stats.record(task)

    # ── Command priority: drain queue without blocking ────────────────────────

    def _drain_cmd_queue(self, defer_hunt_cmds: bool = True) -> None:
        """
        Process ALL commands currently waiting in the queue RIGHT NOW.

        Called:
          • At the top of each reel iteration inside _run_hunt() so that
            /help /status /stats /skip /set* are handled immediately —
            not delayed until the hunt finishes.
          • From the main event loop as usual.

        When defer_hunt_cmds=True (inside a running hunt), __hunt__ and
        __test__ are put back on the queue rather than executed inline, so
        they run after the current hunt completes.
        """
        deferred: List[Dict] = []
        while True:
            try:
                item = self._cmd_queue.get_nowait()
            except queue.Empty:
                break

            cmd = item["cmd"]
            if defer_hunt_cmds and cmd in ("__hunt__", "__test__"):
                deferred.append(item)
                continue

            try:
                self._dispatch_command(cmd, item["arg"])
            except Exception as exc:
                self.log.error(f"Command dispatch error for {cmd!r}: {exc}")

        # Put deferred items back (FIFO order maintained)
        for item in deferred:
            self._cmd_queue.put(item)

    # ── Hunt cycle ────────────────────────────────────────────────────────────

    def _run_hunt(self) -> None:
        """Execute one full URL-collection + processing cycle."""
        if self._hunting:
            self.log.warning("Hunt already in progress — ignoring duplicate request.")
            return

        self._hunting = True
        run_stats = RunStats()          # fresh stats for this run
        run_id: Optional[int] = None

        try:
            run_id = self.db.start_run()
        except Exception as exc:
            self.log.warning(f"Could not start run record: {exc}")

        try:
            self.notifier.send_message("🔍 <b>Hunt started</b> — navigating Reels feed...")

            if not self.collector.navigate_to_reels_feed(self.notifier):
                self.notifier.send_message("❌ Hunt aborted: could not access Reels feed.")
                return

            reel_urls = self.collector.collect_reel_urls(
                self.notifier,
                stop_fn=lambda: self._skip_collection,
            )
            self._skip_collection = False
            if not reel_urls:
                self.log.warning("No Reel URLs collected.")
                self.notifier.send_message("⚠️ No Reels found in this run.")
                return

            self.log.info(f"Processing {len(reel_urls)} URL(s)...")

            for url in reel_urls:
                self.wq.enqueue_url(url)

            while not self.wq.scan.empty():
                # ── Priority: flush commands BEFORE each reel ──────────────
                self._drain_cmd_queue(defer_hunt_cmds=True)

                if self._stop or self._deadline_approaching():
                    break
                if run_stats.sent >= Config.MAX_QUALIFIED_SEND:
                    break

                try:
                    url = self.wq.scan.get_nowait()
                except Exception:
                    break

                reel_id = ReelCollector.extract_reel_id(url)
                if not reel_id:
                    self.log.warning(f"Cannot extract reel ID from: {url}")
                    continue

                if self.db.is_processed(reel_id):
                    self.log.info(f"[{reel_id}] DEDUP hit — skipping.")
                    run_stats.dedup += 1
                    continue

                task = ReelTask(
                    url=url,
                    reel_id=reel_id,
                    max_attempts=Config.MAX_DOWNLOAD_ATTEMPTS,
                )
                self.wq.enqueue_task(task)
                self._process_task(task, run_stats)

                if task.status == ReelStatus.RETRY:
                    self.wq.enqueue_retry(task)

                if task.status not in (ReelStatus.SKIPPED,):
                    self.bm.delay(800, 2500)

            # Flush retries — one pass
            retried = self.wq.flush_retries()
            if retried:
                self.log.info(f"Processing {retried} retry task(s)...")
                while not self.wq.process.empty():
                    self._drain_cmd_queue(defer_hunt_cmds=True)
                    if self._stop or self._deadline_approaching():
                        break
                    if run_stats.sent >= Config.MAX_QUALIFIED_SEND:
                        break
                    try:
                        task = self.wq.process.get_nowait()
                    except Exception:
                        break
                    self._process_task(task, run_stats)

        finally:
            self._hunting = False
            self.log.info(f"Hunt finished: {run_stats.log_summary()}")
            self.log.info(f"Queue stats: {self.wq.stats()}")

            # Merge run stats into session totals
            self.session_stats.scanned         += run_stats.scanned
            self.session_stats.sent            += run_stats.sent
            self.session_stats.skipped_thresh  += run_stats.skipped_thresh
            self.session_stats.skipped_caption += run_stats.skipped_caption
            self.session_stats.skipped_vision  += run_stats.skipped_vision
            self.session_stats.download_fail   += run_stats.download_fail
            self.session_stats.send_fail       += run_stats.send_fail
            self.session_stats.errors          += run_stats.errors
            self.session_stats.dedup           += run_stats.dedup

            try:
                db_stats = self.db.get_aggregate_stats()
                summary  = run_stats.telegram_summary(self._elapsed(), db_stats)
                self.notifier.send_message(summary)
                if run_id:
                    self.db.end_run(run_id, run_stats.scanned, run_stats.sent, "completed")
            except Exception as exc:
                self.log.warning(f"Could not finalise hunt record: {exc}")

    # ── Force-test a single reel ──────────────────────────────────────────────

    def _run_test(self, url: str) -> None:
        self.log.info(f"TEST MODE: {url}")
        self._poller.reply(
            f"⏳ <b>Test reel queued</b> — AI vision skipped, downloading directly...\n"
            f"<code>{url}</code>"
        )
        reel_id = ReelCollector.extract_reel_id(url)
        if not reel_id:
            self._poller.reply("❌ Cannot parse reel ID from that URL.")
            return

        task = ReelTask(url=url, reel_id=reel_id, max_attempts=1)
        dummy_stats = RunStats()
        try:
            self._process_task(task, dummy_stats, force=True, skip_vision=True)
            self._poller.reply(f"✅ Test complete — status: <b>{task.status.value}</b>")
        except KeyboardInterrupt:
            raise
        except Exception:
            tb = traceback.format_exc()
            self.log.error(f"Test reel exception:\n{tb}")
            self._poller.reply(f"❌ Test crashed:\n<pre>{tb[:400]}</pre>")

    # ── Command dispatch ──────────────────────────────────────────────────────

    def _get_status_text(self) -> str:
        m, s = divmod(int(self._elapsed()), 60)
        s_st = self.session_stats
        return (
            "<b>🔍 Reels Hunter — Status</b>\n\n"
            f"<b>Uptime:</b> {m}m {s}s\n"
            f"<b>Hunting:</b> {'🟢 Yes' if self._hunting else '🔴 Idle'}\n\n"
            f"<b>Session stats</b>\n"
            f"  Scanned         : {s_st.scanned}\n"
            f"  Sent            : {s_st.sent}\n"
            f"  Below threshold : {s_st.skipped_thresh}\n"
            f"  Caption filter  : {s_st.skipped_caption}\n"
            f"  Vision rejected : {s_st.skipped_vision}\n"
            f"  Dedup skipped   : {s_st.dedup}\n"
            f"  DL failures     : {s_st.download_fail}\n\n"
            f"<b>Min Views:</b> {Config.MIN_VIEWS:,}\n"
            f"<b>Scan Target:</b> {Config.TARGET_REELS_SCAN}\n"
            f"<b>Max Send:</b> {Config.MAX_QUALIFIED_SEND}\n"
            f"<b>Target accounts:</b> {', '.join(Config.USERS_ATTACK) or '(feed)'}\n"
            f"\n<b>Queue:</b> <code>{self.wq.stats()}</code>"
        )

    def _dispatch_command(self, cmd: str, arg: str) -> None:
        self.log.info(f"Command: {cmd!r}  arg={arg!r}")
        reply = self._poller.reply

        if cmd == "/help":
            reply(TelegramCommandPoller.HELP_TEXT)

        elif cmd == "/status":
            reply(self._get_status_text())

        elif cmd == "/stats":
            try:
                db_stats = self.db.get_aggregate_stats()
                reply(self.session_stats.telegram_stats_cmd(db_stats))
            except Exception as exc:
                reply(f"❌ Stats error: {exc}")

        elif cmd == "/start":
            if self._hunting:
                reply("⚠️ A hunt is already running. Use /status to check progress.")
            else:
                reply("🚀 Starting a new hunt run...")
                self._cmd_queue.put({"cmd": "__hunt__", "arg": ""})

        elif cmd == "/restart":
            reply("🔄 Restarting agent process...")
            self._stop = True
            time.sleep(1)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        elif cmd == "/test":
            url = arg.strip()
            if not url:
                reply("❌ Usage: <code>/test https://www.instagram.com/reel/XXXXX/</code>")
                return
            if "/reel" not in url and "/p/" not in url:
                reply(
                    "❌ URL doesn't look like an Instagram reel.\n"
                    "Accepted: <code>/reel/XXXXX/</code>  or  <code>/p/XXXXX/</code>"
                )
                return
            if self._hunting:
                reply("⚠️ A hunt is running. /test will run after it finishes.")
            reply(f"🧪 Test queued:\n<code>{url}</code>")
            self._cmd_queue.put({"cmd": "__test__", "arg": url})

        elif cmd == "/setviews":
            try:
                n = int(arg.replace(",", "").lower().replace("k", "000"))
                Config.MIN_VIEWS = n
                reply(f"✅ MIN_VIEWS set to <b>{n:,}</b>")
            except ValueError:
                reply("❌ Usage: <code>/setviews 50000</code>")

        elif cmd == "/setscans":
            try:
                Config.TARGET_REELS_SCAN = int(arg)
                reply(f"✅ TARGET_REELS_SCAN set to <b>{arg}</b>")
            except ValueError:
                reply("❌ Usage: <code>/setscans 35</code>")

        elif cmd == "/setsend":
            try:
                Config.MAX_QUALIFIED_SEND = int(arg)
                reply(f"✅ MAX_QUALIFIED_SEND set to <b>{arg}</b>")
            except ValueError:
                reply("❌ Usage: <code>/setsend 5</code>")

        elif cmd in ("/startdisplay", "/desktop", "/resumerdp"):
            reply("🖥️ Detecting RDP display and relaunching browser on it...")
            try:
                import subprocess as _sp
                sockets = _sp.run(
                    ["ls", "/tmp/.X11-unix/"],
                    capture_output=True, text=True, timeout=5,
                ).stdout.splitlines()
                rdp_display = None
                for entry in sorted(sockets):
                    entry = entry.strip()
                    if entry.startswith("X"):
                        num_str = entry[1:]
                        if num_str.isdigit() and 10 <= int(num_str) <= 50:
                            rdp_display = f":{num_str}"
                            break

                if rdp_display:
                    self.log.info(f"/startdisplay: found RDP display {rdp_display}")
                    reply(f"🖥️ Found RDP display <code>{rdp_display}</code> — relaunching browser on it…")
                    try:
                        self.bm.close()
                    except Exception as close_exc:
                        self.log.warning(f"Browser close before relaunch: {close_exc}")
                    os.environ["DISPLAY"] = rdp_display
                    self.bm.launch()
                    try:
                        self.bm.page.goto(
                            "https://www.instagram.com/reels/",
                            wait_until="domcontentloaded", timeout=20_000,
                        )
                    except Exception:
                        pass
                    reply("✅ Browser relaunched on your RDP screen — you should see it now.")
                    try:
                        snap = self.bm.page.screenshot(type="jpeg", quality=75)
                        self.notifier.send_photo(snap, caption="🖥️ Browser is live on your RDP screen")
                    except PlaywrightError as exc:
                        self.log.debug(f"Post-relaunch screenshot failed: {exc}")
                else:
                    current_disp = os.environ.get('DISPLAY', '?')
                    reply(
                        f"⚠️ No RDP session found (checked :10–:50 range).\n\n"
                        f"Current display is <code>{current_disp}</code> "
                        f"({'Xvfb — invisible' if current_disp in (':99', ':0') else 'unknown'}).\n\n"
                        "Connect via RDP first, then send /startdisplay again."
                    )
                    try:
                        snap = self.bm.page.screenshot(type="jpeg", quality=75)
                        self.notifier.send_photo(
                            snap,
                            caption=f"👁️ Playwright view on <code>{current_disp}</code> (Xvfb — not your RDP screen)\n<code>{self.bm.page.url}</code>",
                        )
                    except PlaywrightError as exc:
                        reply(f"Screenshot failed: {exc}")
            except Exception as exc:
                self.log.exception("/startdisplay handler error")
                reply(f"❌ /startdisplay error: {exc}")

        elif cmd == "/skip":
            if not self._hunting:
                reply("⚠️ No hunt is running right now — nothing to skip.")
            elif self._skip_collection:
                reply("⏩ Skip already requested — collection will stop on the next step.")
            else:
                self._skip_collection = True
                reply(
                    "⏩ <b>Skip requested</b> — URL collection will stop after the current "
                    "step and the download + Gemini stage will begin immediately with the "
                    "reels collected so far."
                )

        elif cmd == "/resume":
            # /resume re-enables hunting if it was paused / stopped
            if self._hunting:
                reply("🟢 Hunt is already running.")
            else:
                reply("▶️ Resuming — starting a new hunt run...")
                self._cmd_queue.put({"cmd": "__hunt__", "arg": ""})

        else:
            reply(f"❓ Unknown command: <code>{cmd}</code>  Try /help")

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self) -> None:
        if not self.setup():
            return

        self._poller.start()
        self.notifier.send_message(
            "🤖 <b>Reels Hunter online</b>\n"
            "Type /help for available commands.\n"
            "Starting initial hunt..."
        )

        crash_screenshot: Optional[bytes] = None
        final_status = "completed"

        try:
            self._run_hunt()

            self.log.info("Entering command loop — waiting for Telegram commands...")
            while not self._stop:
                if self._deadline_approaching():
                    self.log.warning("Deadline reached in command loop — shutting down.")
                    break
                try:
                    item = self._cmd_queue.get(timeout=5)
                except queue.Empty:
                    continue

                cmd = item["cmd"]
                arg = item["arg"]

                if cmd == "__hunt__":
                    try:
                        self._run_hunt()
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        tb = traceback.format_exc()
                        self.log.error(f"Hunt exception:\n{tb}")
                        self.notifier.send_crash_alert(tb)

                elif cmd == "__test__":
                    try:
                        self._run_test(arg)
                    except KeyboardInterrupt:
                        raise
                    except Exception:
                        tb = traceback.format_exc()
                        self.log.error(f"Test exception:\n{tb}")
                        self._poller.reply(f"❌ Test crashed:\n<pre>{tb[:400]}</pre>")

                else:
                    try:
                        self._dispatch_command(cmd, arg)
                    except Exception as exc:
                        self.log.error(f"Command dispatch error for {cmd!r}: {exc}")

        except KeyboardInterrupt:
            self.log.warning("KeyboardInterrupt — graceful shutdown.")
            final_status = "interrupted"

        except Exception:
            tb = traceback.format_exc()
            self.log.critical(f"FATAL UNHANDLED EXCEPTION:\n{tb}")
            final_status = "crashed"
            try:
                crash_screenshot = self.bm.page.screenshot(type="jpeg", quality=80)
            except Exception as exc:
                self.log.debug(f"Crash screenshot failed (non-fatal): {exc}")
            self.notifier.send_crash_alert(tb, crash_screenshot)

        finally:
            self._poller.stop()
            self._send_session_summary()
            self.shutdown(final_status)

    def _send_session_summary(self) -> None:
        try:
            db_stats = self.db.get_aggregate_stats()
            summary  = self.session_stats.telegram_summary(self._elapsed(), db_stats)
            self.notifier.send_message(f"🏁 <b>Session ended</b>\n\n{summary}")
        except Exception as exc:
            self.log.warning(f"Could not send session summary: {exc}")

    def shutdown(self, status: str = "completed") -> None:
        self.log.info(f"Shutdown initiated (status={status})...")

        try:
            if getattr(self.bm, "_ctx", None) and getattr(self.bm, "_trace_dir", None):
                trace_path = self.bm._trace_dir / "trace.zip"
                self.bm._ctx.tracing.stop(path=str(trace_path))
                self.log.info(f"Playwright trace saved: {trace_path}")
        except Exception as exc:
            self.log.warning(f"Could not save Playwright trace: {exc}")

        try:
            self.bm.close()
        except Exception as exc:
            self.log.warning(f"Error closing browser: {exc}")

        try:
            if self._run_id and self.db.conn:
                self.db.end_run(
                    self._run_id,
                    self.session_stats.scanned,
                    self.session_stats.sent,
                    status,
                )
        except Exception as exc:
            self.log.warning(f"Could not finalise run record: {exc}")

        self.db.close()
        self.log.info(
            f"Shutdown complete in {self._elapsed():.1f}s | "
            f"session: {self.session_stats.log_summary()}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("=" * 70)
    log.info("  ULTIMATE REELS AI HUNTER  —  Production Agent")
    log.info(f"  Started: {datetime.utcnow().isoformat()} UTC")
    log.info("=" * 70)
    agent = InstagramAgent()
    agent.run()
PYEOF
echo "agent.py written"
