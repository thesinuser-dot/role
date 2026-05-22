#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# pipeline.py — Task queue and state machine
#
# Replaces the original linear pipeline with four explicit queues:
#
#   scan     — raw URLs waiting for dedup check
#   process  — deduped tasks ready for metrics → vision → download
#   retry    — tasks that failed transiently and should be attempted again
#   failed   — terminal failures (max attempts exceeded or permanent error)
#
# ReelTask is a dataclass that carries all state for one reel through the
# pipeline.  The status field is the single source of truth.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class ReelStatus(Enum):
    PENDING     = "pending"      # in scan queue, not yet deduped
    PROCESSING  = "processing"   # actively being worked on
    DOWNLOADED  = "downloaded"   # sent to Telegram successfully
    SKIPPED     = "skipped"      # filtered out (views, vision, dedup)
    FAILED      = "failed"       # terminal failure after max retries
    RETRY       = "retry"        # transient failure, eligible for retry


class FailureKind(Enum):
    """Categorises failures so the agent can decide retry vs terminal."""
    TRANSIENT    = "transient"   # network, timeout, rate-limit — worth retrying
    PERMANENT    = "permanent"   # dedup hit, below threshold — skip forever
    VISION       = "vision"      # failed quality check — skip
    DOWNLOAD     = "download"    # yt-dlp / interception failed — may retry
    SEND         = "send"        # Telegram delivery failed — persist + retry
    UNKNOWN      = "unknown"     # catch-all


@dataclass
class ReelTask:
    url:          str
    reel_id:      str
    status:       ReelStatus = ReelStatus.PENDING
    failure_kind: Optional[FailureKind] = None
    failure_reason: Optional[str] = None

    views:  int = 0
    likes:  int = 0

    # Set by the downloader once the file is on disk
    video_path: Optional[Path] = None

    attempt:      int = 0
    max_attempts: int = 2         # overridden per-task type by the agent

    # Timing — monotonic so it survives clock changes
    created_at:    float = field(default_factory=time.monotonic)
    last_attempt:  float = 0.0

    @property
    def is_retryable(self) -> bool:
        return (
            self.failure_kind in (FailureKind.TRANSIENT, FailureKind.DOWNLOAD, FailureKind.SEND)
            and self.attempt < self.max_attempts
        )

    def mark_retry(self, reason: str, kind: FailureKind = FailureKind.TRANSIENT) -> None:
        self.attempt += 1
        self.last_attempt = time.monotonic()
        self.status = ReelStatus.RETRY if self.is_retryable else ReelStatus.FAILED
        self.failure_reason = reason
        self.failure_kind = kind

    def mark_failed(self, reason: str, kind: FailureKind = FailureKind.UNKNOWN) -> None:
        self.status = ReelStatus.FAILED
        self.failure_reason = reason
        self.failure_kind = kind

    def mark_skipped(self, reason: str, kind: FailureKind = FailureKind.PERMANENT) -> None:
        self.status = ReelStatus.SKIPPED
        self.failure_reason = reason
        self.failure_kind = kind

    def mark_downloaded(self, video_path: Path) -> None:
        self.status = ReelStatus.DOWNLOADED
        self.video_path = video_path

    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at


class WorkQueue:
    """
    Thread-safe four-queue pipeline.

    scan     — raw URL strings (strings, not ReelTasks)
    process  — ReelTasks ready for full processing
    retry    — ReelTasks that failed transiently, will be re-enqueued to process
    failed   — terminal ReelTasks; never re-enqueued (for metrics only)

    The agent feeds URLs into scan, the pipeline worker deduplicates them
    and moves qualified tasks to process.  After processing, tasks flow to
    either retry or failed.  On retry they go back to process.
    """

    def __init__(self):
        self.scan:    queue.Queue[str]       = queue.Queue()
        self.process: queue.Queue[ReelTask]  = queue.Queue()
        self.retry:   queue.Queue[ReelTask]  = queue.Queue()
        self._failed: List[ReelTask]         = []
        self._lock = threading.Lock()
        self.log = logging.getLogger("WorkQueue")

    # ── Enqueue helpers ───────────────────────────────────────────────────────

    def enqueue_url(self, url: str) -> None:
        self.scan.put(url)

    def enqueue_task(self, task: ReelTask) -> None:
        """Send a task to the process queue (first attempt or after dedup)."""
        task.status = ReelStatus.PROCESSING
        self.process.put(task)

    def enqueue_retry(self, task: ReelTask) -> None:
        if task.is_retryable:
            self.log.info(
                f"Queuing retry for {task.reel_id} "
                f"(attempt {task.attempt}/{task.max_attempts})"
            )
            self.retry.put(task)
        else:
            self.log.warning(
                f"Task {task.reel_id} exceeded max attempts — moved to failed "
                f"(reason={task.failure_reason})"
            )
            self.mark_failed(task)

    def mark_failed(self, task: ReelTask) -> None:
        task.status = ReelStatus.FAILED
        with self._lock:
            self._failed.append(task)

    # ── Drain retry → process ─────────────────────────────────────────────────

    def flush_retries(self) -> int:
        """Move all pending retries into the process queue. Returns count moved."""
        moved = 0
        while True:
            try:
                task = self.retry.get_nowait()
                task.attempt += 1
                task.last_attempt = time.monotonic()
                self.enqueue_task(task)
                moved += 1
            except queue.Empty:
                break
        if moved:
            self.log.info(f"Flushed {moved} retry task(s) to process queue.")
        return moved

    # ── Stats ─────────────────────────────────────────────────────────────────

    @property
    def failed(self) -> List[ReelTask]:
        with self._lock:
            return list(self._failed)

    def stats(self) -> str:
        with self._lock:
            return (
                f"scan={self.scan.qsize()} "
                f"process={self.process.qsize()} "
                f"retry={self.retry.qsize()} "
                f"failed={len(self._failed)}"
            )
