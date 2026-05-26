#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# database.py — SQLite persistence layer
#
# Tables
#   processed_reels  — dedup index + audit trail (one row per reel ever seen)
#   run_history      — one row per agent run
#   pending_uploads  — reels downloaded but not yet confirmed sent to Telegram;
#                      persisted across runs so a crash or Actions timeout can't
#                      permanently lose a downloaded video
# ─────────────────────────────────────────────────────────────────────────────

import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import Config


class DatabaseManager:
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS processed_reels (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        reel_id      TEXT    NOT NULL UNIQUE,
        url          TEXT    NOT NULL,
        status       TEXT    NOT NULL,
        views        INTEGER DEFAULT 0,
        likes        INTEGER DEFAULT 0,
        skip_reason  TEXT,
        processed_at TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_reel_id ON processed_reels(reel_id);
    CREATE INDEX        IF NOT EXISTS idx_status   ON processed_reels(status);

    CREATE TABLE IF NOT EXISTS run_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        run_start      TEXT NOT NULL,
        run_end        TEXT,
        reels_scanned  INTEGER DEFAULT 0,
        reels_sent     INTEGER DEFAULT 0,
        status         TEXT    DEFAULT 'running'
    );

    -- Reels that were downloaded but whose Telegram send has not been confirmed.
    -- On startup the agent retries every row here before starting a new hunt.
    -- A row is deleted only after a successful send (not on first attempt).
    CREATE TABLE IF NOT EXISTS pending_uploads (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        reel_id      TEXT    NOT NULL UNIQUE,
        url          TEXT    NOT NULL,
        video_path   TEXT    NOT NULL,
        views        INTEGER DEFAULT 0,
        likes        INTEGER DEFAULT 0,
        attempts     INTEGER DEFAULT 0,
        created_at   TEXT    DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
        last_attempt TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pending_attempts ON pending_uploads(attempts);
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.log = logging.getLogger("DatabaseManager")

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.log.info(f"Opening database: {self.db_path}")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.executescript(self._SCHEMA)
        self.conn.commit()
        count = self.conn.execute("SELECT COUNT(*) FROM processed_reels").fetchone()[0]
        pending = self.conn.execute("SELECT COUNT(*) FROM pending_uploads").fetchone()[0]
        self.log.info(f"Database ready — records: {count:,}  pending uploads: {pending}")

    def close(self) -> None:
        if self.conn:
            self.conn.commit()
            # Flush WAL back into the main file so a SIGTERM mid-Playwright-call
            # doesn't leave the artifact uploaded to GitHub Actions incomplete.
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except sqlite3.Error as exc:
                self.log.warning(f"WAL checkpoint failed (non-fatal): {exc}")
            self.conn.close()
            self.conn = None
            self.log.info("Database closed, committed, WAL checkpointed.")

    # ── Dedup ──────────────────────────────────────────────────────────────────

    def is_processed(self, reel_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM processed_reels WHERE reel_id = ? LIMIT 1", (reel_id,)
        ).fetchone()
        return row is not None

    def mark_processed(
        self,
        reel_id: str,
        url: str,
        status: str,
        views: int = 0,
        likes: int = 0,
        skip_reason: Optional[str] = None,
    ) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO processed_reels (reel_id, url, status, views, likes, skip_reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(reel_id) DO UPDATE SET
                    status       = excluded.status,
                    views        = excluded.views,
                    likes        = excluded.likes,
                    skip_reason  = excluded.skip_reason,
                    processed_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                """,
                (reel_id, url, status, views, likes, skip_reason),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            self.log.error(f"DB write error for reel_id={reel_id}: {exc}")

    # ── Run history ────────────────────────────────────────────────────────────

    def start_run(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO run_history (run_start) VALUES (?)",
            (datetime.utcnow().isoformat(),),
        )
        self.conn.commit()
        run_id = cur.lastrowid
        self.log.info(f"Run started: run_id={run_id}")
        return run_id

    def end_run(self, run_id: int, scanned: int, sent: int, status: str = "completed") -> None:
        self.conn.execute(
            """
            UPDATE run_history
            SET run_end = ?, reels_scanned = ?, reels_sent = ?, status = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(), scanned, sent, status, run_id),
        )
        self.conn.commit()
        self.log.info(f"Run {run_id} ended: status={status} scanned={scanned} sent={sent}")

    # ── Aggregate stats ────────────────────────────────────────────────────────

    def get_aggregate_stats(self) -> Dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status='downloaded'  THEN 1 ELSE 0 END), 0) AS downloaded,
                COALESCE(SUM(CASE WHEN status='skipped'     THEN 1 ELSE 0 END), 0) AS skipped,
                COALESCE(SUM(CASE WHEN status='error'       THEN 1 ELSE 0 END), 0) AS errors
            FROM processed_reels
            """
        ).fetchone()
        if row is None:
            return {"total": 0, "downloaded": 0, "skipped": 0, "errors": 0}
        return {k: int(row[k] or 0) for k in ("total", "downloaded", "skipped", "errors")}

    # ── Pending uploads ────────────────────────────────────────────────────────

    def add_pending_upload(
        self, reel_id: str, url: str, video_path: Path, views: int, likes: int
    ) -> None:
        """Register a downloaded video that has not been confirmed sent yet."""
        try:
            self.conn.execute(
                """
                INSERT INTO pending_uploads (reel_id, url, video_path, views, likes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(reel_id) DO UPDATE SET
                    video_path   = excluded.video_path,
                    views        = excluded.views,
                    likes        = excluded.likes
                """,
                (reel_id, url, str(video_path), views, likes),
            )
            self.conn.commit()
            self.log.debug(f"Pending upload registered: {reel_id}")
        except sqlite3.Error as exc:
            self.log.error(f"Failed to register pending upload for {reel_id}: {exc}")

    def get_pending_uploads(self, max_attempts: int) -> List[Dict]:
        """Return all pending uploads that have not exceeded max_attempts."""
        rows = self.conn.execute(
            "SELECT * FROM pending_uploads WHERE attempts < ? ORDER BY created_at ASC",
            (max_attempts,),
        ).fetchall()
        return [dict(r) for r in rows]

    def increment_upload_attempt(self, reel_id: str) -> None:
        try:
            self.conn.execute(
                """
                UPDATE pending_uploads
                SET attempts = attempts + 1,
                    last_attempt = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE reel_id = ?
                """,
                (reel_id,),
            )
            self.conn.commit()
        except sqlite3.Error as exc:
            self.log.warning(f"Could not increment attempt count for {reel_id}: {exc}")

    def remove_pending_upload(self, reel_id: str) -> None:
        """Remove a row after a confirmed successful send."""
        try:
            self.conn.execute("DELETE FROM pending_uploads WHERE reel_id = ?", (reel_id,))
            self.conn.commit()
            self.log.debug(f"Pending upload cleared: {reel_id}")
        except sqlite3.Error as exc:
            self.log.warning(f"Could not remove pending upload for {reel_id}: {exc}")
