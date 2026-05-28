#!/usr/bin/env python3
"""
ollama_vision.py — Remote LLaVA vision client for the HF Space API.

Plugs into vision.py as a drop-in fallback after Gemini Web.
The HF Space exposes an OpenAI-compatible endpoint so this client
uses the same _chat_completion pattern already in ai_router.py.

Config (env vars / GitHub secrets):
    OLLAMA_BASE_URL   — full URL of your HF Space, e.g.
                        https://YOUR-USERNAME-llava-vision-api.hf.space
    OLLAMA_MODEL      — model name to pass (default: "llava")
    OLLAMA_TIMEOUT    — seconds to wait per request (default: 120)
    HF_TOKEN          — optional Bearer token if Space is private
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Optional, Tuple

import requests

log = logging.getLogger("OllamaVision")

_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "").rstrip("/")
_MODEL    = os.environ.get("OLLAMA_MODEL", "llava")
_TIMEOUT  = int(os.environ.get("OLLAMA_TIMEOUT", "120"))
_HF_TOKEN = os.environ.get("HF_TOKEN", "")


def is_configured() -> bool:
    return bool(_BASE_URL)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if _HF_TOKEN:
        h["Authorization"] = f"Bearer {_HF_TOKEN}"
    return h


def health_check() -> bool:
    """Return True if the HF Space is up and responding."""
    if not _BASE_URL:
        return False
    try:
        r = requests.get(f"{_BASE_URL}/health", timeout=15, headers=_headers())
        return r.status_code == 200
    except Exception as e:
        log.warning(f"HF Space health check failed: {e}")
        return False


def ask_vision(
    prompt: str,
    image_bytes: Optional[bytes] = None,
    max_tokens: int = 16,
) -> Tuple[Optional[str], bool]:
    """
    Send a vision request to the HF Space LLaVA API.

    Returns:
        (response_text, success)
        response_text is None on failure.
    """
    if not _BASE_URL:
        log.debug("OLLAMA_BASE_URL not set — skipping HF vision.")
        return None, False

    # Build OpenAI-compatible message content
    content: list = [{"type": "text", "text": prompt}]
    if image_bytes:
        b64 = base64.b64encode(image_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })

    payload = {
        "model": _MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
    }

    t0 = time.time()
    try:
        resp = requests.post(
            f"{_BASE_URL}/v1/chat/completions",
            json=payload,
            headers=_headers(),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        text = (
            data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
        )
        elapsed = time.time() - t0
        log.info(f"HF LLaVA response in {elapsed:.1f}s: {text!r}")
        return text, bool(text)

    except requests.exceptions.Timeout:
        log.warning(f"HF Space timed out after {_TIMEOUT}s")
        return None, False
    except Exception as e:
        log.warning(f"HF Space request failed: {e}")
        return None, False
