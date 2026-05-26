#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# proxy_pool.py — Webshare.io rotating proxy pool
#
# Fetches the proxy list from the Webshare API once at startup, then hands
# out proxies in round-robin order.  Failed proxies are quarantined for
# QUARANTINE_SECONDS before being re-admitted.
#
# Usage:
#   pool = ProxyPool(api_key="YOUR_KEY")
#   proxy = pool.get()           # returns {"server": "...", "username": ..., "password": ...}
#   pool.mark_failed(proxy)      # quarantines this proxy
#   pool.mark_success(proxy)     # resets its failure counter
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

# ── tunables ──────────────────────────────────────────────────────────────────
WEBSHARE_API_BASE   = "https://proxy.webshare.io/api/v2"
PAGE_SIZE           = 100          # proxies per API page (max 100)
QUARANTINE_SECONDS  = 300          # how long a bad proxy sits out
MAX_FAILURES        = 3            # failures before quarantine
CONNECT_TIMEOUT     = 10           # seconds for requests to Webshare API
REFRESH_INTERVAL    = 3600         # re-fetch the list every hour


@dataclass
class _ProxyEntry:
    server:   str            # "http://host:port"
    username: str
    password: str
    failures: int = 0
    quarantine_until: float = 0.0

    @property
    def is_available(self) -> bool:
        return time.monotonic() >= self.quarantine_until

    def playwright_dict(self) -> dict:
        """Return the dict Playwright's new_context(proxy=...) expects."""
        return {
            "server":   self.server,
            "username": self.username,
            "password": self.password,
        }


class ProxyPool:
    """
    Thread-safe rotating proxy pool backed by Webshare.io.

    Parameters
    ----------
    api_key : str
        Webshare REST API key (Token auth).
    mode : str
        "direct" (datacenter, rotating) or "backbone" — must match your plan.
        Defaults to "direct".
    refresh : bool
        Whether to refresh the proxy list periodically in a background thread.
    """

    def __init__(
        self,
        api_key: str,
        mode: str = "direct",
        refresh: bool = True,
    ) -> None:
        self._api_key = api_key
        self._mode    = mode
        self._lock    = threading.Lock()
        self._entries: List[_ProxyEntry] = []
        self._index   = 0

        self._fetch_all()

        if refresh:
            t = threading.Thread(target=self._refresh_loop, daemon=True)
            t.start()

    # ── public API ────────────────────────────────────────────────────────────

    def get(self) -> Optional[dict]:
        """
        Return a Playwright proxy dict for the next available proxy.
        Returns None if the pool is empty or all proxies are quarantined.
        """
        with self._lock:
            available = [e for e in self._entries if e.is_available]
            if not available:
                log.warning("ProxyPool: no available proxies (all quarantined or pool empty)")
                return None
            # round-robin over available entries
            self._index = self._index % len(available)
            entry = available[self._index]
            self._index = (self._index + 1) % len(available)
            log.debug(f"ProxyPool: selected {entry.server}")
            return entry.playwright_dict()

    def get_random(self) -> Optional[dict]:
        """Return a random available proxy (useful for per-session randomness)."""
        with self._lock:
            available = [e for e in self._entries if e.is_available]
            if not available:
                log.warning("ProxyPool: no available proxies")
                return None
            entry = random.choice(available)
            log.debug(f"ProxyPool: random-selected {entry.server}")
            return entry.playwright_dict()

    def mark_failed(self, proxy: dict) -> None:
        """Increment failure counter; quarantine if threshold reached."""
        server = proxy.get("server", "")
        with self._lock:
            entry = self._find(server)
            if entry is None:
                return
            entry.failures += 1
            if entry.failures >= MAX_FAILURES:
                entry.quarantine_until = time.monotonic() + QUARANTINE_SECONDS
                log.warning(
                    f"ProxyPool: quarantined {server} for {QUARANTINE_SECONDS}s "
                    f"(failures={entry.failures})"
                )
            else:
                log.debug(f"ProxyPool: failure #{entry.failures} for {server}")

    def mark_success(self, proxy: dict) -> None:
        """Reset failure counter on success."""
        server = proxy.get("server", "")
        with self._lock:
            entry = self._find(server)
            if entry:
                entry.failures = 0

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def available_count(self) -> int:
        with self._lock:
            return sum(1 for e in self._entries if e.is_available)

    def summary(self) -> str:
        with self._lock:
            total  = len(self._entries)
            avail  = sum(1 for e in self._entries if e.is_available)
            quar   = total - avail
            return (
                f"ProxyPool: {total} proxies total, "
                f"{avail} available, {quar} quarantined"
            )

    # ── internals ─────────────────────────────────────────────────────────────

    def _find(self, server: str) -> Optional[_ProxyEntry]:
        """Caller must hold self._lock."""
        for e in self._entries:
            if e.server == server:
                return e
        return None

    def _fetch_all(self) -> None:
        """Download every proxy page from Webshare and rebuild the pool."""
        entries: List[_ProxyEntry] = []
        page = 1
        while True:
            data = self._fetch_page(page)
            if data is None:
                break
            results = data.get("results", [])
            for p in results:
                host = p.get("proxy_address") or p.get("host", "")
                port = p.get("port", 80)
                user = p.get("username", "")
                pwd  = p.get("password", "")
                if not host:
                    continue
                entries.append(_ProxyEntry(
                    server   = f"http://{host}:{port}",
                    username = user,
                    password = pwd,
                ))
            # pagination
            next_url = data.get("next")
            if not next_url:
                break
            page += 1

        if entries:
            with self._lock:
                # preserve quarantine state for entries that already exist
                old_map: Dict[str, _ProxyEntry] = {e.server: e for e in self._entries}
                for e in entries:
                    if e.server in old_map:
                        old = old_map[e.server]
                        e.failures         = old.failures
                        e.quarantine_until = old.quarantine_until
                self._entries = entries
                self._index   = 0
            log.info(f"ProxyPool: loaded {len(entries)} proxies from Webshare")
        else:
            log.warning("ProxyPool: no proxies returned from Webshare API")

    def _fetch_page(self, page: int) -> Optional[dict]:
        url = f"{WEBSHARE_API_BASE}/proxy/list/"
        params = {"mode": self._mode, "page": page, "page_size": PAGE_SIZE}
        headers = {"Authorization": f"Token {self._api_key}"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=CONNECT_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.error(f"ProxyPool: failed to fetch page {page}: {exc}")
            return None

    def _refresh_loop(self) -> None:
        while True:
            time.sleep(REFRESH_INTERVAL)
            log.info("ProxyPool: refreshing proxy list…")
            self._fetch_all()
