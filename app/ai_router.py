#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# ai_router.py — Best-effort LLM routing for text + hashtag generation
#
# Priority:
#   1) Gemini (vision-capable; preferred when an image is available)
#   2) Groq    (OpenAI-compatible text fallback)
#   3) OpenRouter (OpenAI-compatible text fallback; can use openrouter/free)
#
# Fixes applied (2026-05):
#   • Instant quota failover — 429/RESOURCE_EXHAUSTED never retries Gemini;
#     switches to Groq/OpenRouter immediately
#   • Provider health cache — quota-failed provider is disabled for 5 minutes
#     so subsequent requests skip it entirely instead of hammering dead quota
#   • Gemini concurrency semaphore — prevents burst storms that spike RPM
#   • Image compression before Gemini Vision — reduces TPM consumption
#   • Context/prompt truncation guard — prevents token explosion
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Optional

import requests

from config import Config

try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    from gemini_web import GeminiWebClient as _GeminiWebClient
except Exception:
    _GeminiWebClient = None


# ── Quota / rate-limit error detection ───────────────────────────────────────

_QUOTA_SIGNALS = (
    "429",
    "quota",
    "resource_exhausted",
    "rate limit",
    "rate_limit",
    "too many requests",
    "resourceexhausted",
)

def _is_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(sig in msg for sig in _QUOTA_SIGNALS)


# ── Per-provider health cache (thread-safe) ───────────────────────────────────
# When a provider hits a quota error it is marked unavailable for
# PROVIDER_COOLDOWN_SECONDS. All calls during that window skip it instantly.

_PROVIDER_COOLDOWN_SECONDS = 300  # 5 minutes

class _ProviderHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self._disabled_until: dict[str, float] = {}

    def mark_quota_failed(self, provider: str) -> None:
        with self._lock:
            until = time.time() + _PROVIDER_COOLDOWN_SECONDS
            self._disabled_until[provider] = until
            logging.getLogger("AIProviderRouter").warning(
                f"[quota] {provider} quota hit — disabling for {_PROVIDER_COOLDOWN_SECONDS}s "
                f"(until {time.strftime('%H:%M:%S', time.localtime(until))})"
            )

    def is_available(self, provider: str) -> bool:
        with self._lock:
            until = self._disabled_until.get(provider, 0)
            if time.time() >= until:
                return True
            remaining = int(until - time.time())
            logging.getLogger("AIProviderRouter").debug(
                f"[quota] {provider} still cooling down ({remaining}s remaining)"
            )
            return False

    def reset(self, provider: str) -> None:
        with self._lock:
            self._disabled_until.pop(provider, None)


_health = _ProviderHealth()

# Semaphore: max 2 concurrent Gemini API calls to stay within RPM limits
_gemini_semaphore = threading.Semaphore(2)


# ── Image compression helper ──────────────────────────────────────────────────

def _compress_image(image_bytes: bytes, max_dim: int = 720) -> bytes:
    """
    Resize image to max_dim on longest side before sending to Gemini Vision.
    Reduces TPM consumption significantly for large screenshots.
    Returns original bytes if PIL is unavailable or resize fails.
    """
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size
        if max(w, h) <= max_dim:
            return image_bytes
        scale = max_dim / max(w, h)
        new_size = (int(w * scale), int(h * scale))
        img = img.resize(new_size, Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        compressed = buf.getvalue()
        logging.getLogger("AIProviderRouter").debug(
            f"Image compressed: {len(image_bytes)//1024}KB → {len(compressed)//1024}KB"
        )
        return compressed
    except Exception:
        return image_bytes


class AIProviderRouter:
    def __init__(self) -> None:
        self.log = logging.getLogger("AIProviderRouter")
        self._gemini_model = None
        self._providers = [p for p in Config.AI_PROVIDER_ORDER if p in {"gemini", "groq", "openrouter"}]

        if Config.GEMINI_API_KEY and genai is not None:
            try:
                genai.configure(api_key=Config.GEMINI_API_KEY)
                self._gemini_model = genai.GenerativeModel(Config.GEMINI_MODEL)
                self.log.info("Gemini text/vision provider ready: %s", Config.GEMINI_MODEL)
            except Exception as exc:
                self.log.warning("Gemini init failed: %s", exc)
                self._gemini_model = None

        self._gemini_web: Optional["_GeminiWebClient"] = None
        if (
            Config.GEMINI_WEB_ENABLED
            and Config.GEMINI_COOKIES
            and _GeminiWebClient is not None
        ):
            try:
                self._gemini_web = _GeminiWebClient(Config.GEMINI_COOKIES)
                if self._gemini_web.enabled:
                    self.log.info("Gemini Web (browser) provider ready.")
                else:
                    self._gemini_web = None
            except Exception as exc:
                self.log.warning("Gemini Web init failed: %s", exc)
                self._gemini_web = None

    @property
    def gemini_ready(self) -> bool:
        return self._gemini_model is not None

    @property
    def gemini_web_ready(self) -> bool:
        return self._gemini_web is not None and self._gemini_web.enabled

    def _response_text(self, response) -> Optional[str]:
        if response is None:
            return None
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                ptext = getattr(part, "text", None)
                if isinstance(ptext, str) and ptext.strip():
                    return ptext.strip()
        return None

    def _chat_completion(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        system_prompt: str,
        timeout: int = 45,
        max_tokens: int = 256,
        extra_headers: Optional[dict] = None,
    ) -> Optional[str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }

        resp = requests.post(base_url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    txt = item.get("text")
                    if isinstance(txt, str):
                        parts.append(txt)
            combined = "".join(parts).strip()
            if combined:
                return combined
        return None

    def _try_gemini(
        self,
        prompt: str,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
        max_output_tokens: int = 256,
    ) -> Optional[str]:
        """
        Call Gemini API with quota-aware error handling.

        KEY FIX: On quota/429 errors, mark provider as unavailable and raise
        immediately — never let the caller retry Gemini on a quota failure.
        """
        if not self._gemini_model:
            return None

        if not _health.is_available("gemini"):
            self.log.debug("Gemini skipped — quota cooldown active.")
            return None

        # Compress image to reduce TPM cost before sending
        if image_bytes is not None:
            max_dim = getattr(Config, "GEMINI_MAX_DIM", 720)
            image_bytes = _compress_image(image_bytes, max_dim=max_dim)

        # Truncate prompt to avoid accidental context explosion
        prompt = prompt[:8000]

        contents = [prompt]
        if image_bytes is not None:
            contents.append({"mime_type": mime_type, "data": image_bytes})

        # Semaphore limits concurrent Gemini calls to prevent RPM spikes
        with _gemini_semaphore:
            try:
                response = self._gemini_model.generate_content(
                    contents=contents,
                    generation_config=genai.types.GenerationConfig(  # type: ignore[attr-defined]
                        temperature=0.0,
                        max_output_tokens=max_output_tokens,
                    ),
                    request_options={"timeout": 45},
                )
                _health.reset("gemini")  # successful call — clear any old cooldown
                return self._response_text(response)

            except Exception as exc:
                if _is_quota_error(exc):
                    # KEY FIX: quota errors are NOT transient — mark and bail out instantly
                    _health.mark_quota_failed("gemini")
                    raise  # let complete() catch this and skip to next provider
                raise  # non-quota errors propagate normally

    def _try_groq(self, prompt: str, *, max_output_tokens: int = 256) -> Optional[str]:
        if not Config.GROQ_API_KEY:
            return None
        if not _health.is_available("groq"):
            self.log.debug("Groq skipped — quota cooldown active.")
            return None

        models = (
            [Config.GROQ_MODEL]
            if Config.GROQ_MODEL != "auto"
            else Config.GROQ_MODEL_CASCADE
        )
        for model in models:
            try:
                result = self._chat_completion(
                    base_url="https://api.groq.com/openai/v1/chat/completions",
                    api_key=Config.GROQ_API_KEY,
                    model=model,
                    prompt=prompt[:8000],
                    system_prompt=(
                        "You are a concise assistant that returns only the requested text. "
                        "Do not add commentary unless explicitly asked."
                    ),
                    max_tokens=max_output_tokens,
                )
                if result:
                    _health.reset("groq")
                    self.log.info("Groq model succeeded: %s", model)
                    return result
            except Exception as exc:
                if _is_quota_error(exc):
                    _health.mark_quota_failed("groq")
                    break  # stop trying other Groq models too
                self.log.warning("Groq model %s failed: %s — trying next", model, exc)
        return None

    def _try_openrouter(self, prompt: str, *, max_output_tokens: int = 256) -> Optional[str]:
        if not Config.OPENROUTER_API_KEY:
            return None
        if not _health.is_available("openrouter"):
            self.log.debug("OpenRouter skipped — quota cooldown active.")
            return None

        models = (
            [Config.OPENROUTER_MODEL]
            if Config.OPENROUTER_MODEL != "auto"
            else Config.OPENROUTER_MODEL_CASCADE
        )
        for model in models:
            try:
                result = self._chat_completion(
                    base_url="https://openrouter.ai/api/v1/chat/completions",
                    api_key=Config.OPENROUTER_API_KEY,
                    model=model,
                    prompt=prompt[:8000],
                    system_prompt=(
                        "You are a concise assistant that returns only the requested text. "
                        "Do not add commentary unless explicitly asked."
                    ),
                    max_tokens=max_output_tokens,
                    extra_headers={
                        "HTTP-Referer": "https://chat.openai.com",
                        "X-Title": Config.OPENROUTER_APP_NAME,
                    },
                )
                if result:
                    _health.reset("openrouter")
                    self.log.info("OpenRouter model succeeded: %s", model)
                    return result
            except Exception as exc:
                if _is_quota_error(exc):
                    _health.mark_quota_failed("openrouter")
                    break
                self.log.warning("OpenRouter model %s failed: %s — trying next", model, exc)
        return None

    def complete(
        self,
        prompt: str,
        *,
        image_bytes: Optional[bytes] = None,
        mime_type: str = "image/jpeg",
        max_output_tokens: int = 256,
    ) -> Optional[str]:
        """
        Best-effort completion with provider fallback.

        KEY FIX — Quota failover:
          If Gemini returns a 429/RESOURCE_EXHAUSTED, it is immediately marked
          unavailable for 5 minutes and the request falls through to Groq or
          OpenRouter. No retries against a dead quota.

        When image_bytes is provided (vision task):
          - Gemini API is tried first (supports images).
          - Groq and OpenRouter are skipped — text-only APIs.

        When image_bytes is None (text-only task):
          - All providers tried in configured order.
        """
        tried = []
        has_image = image_bytes is not None

        # 1) Gemini API
        if "gemini" in self._providers and self._gemini_model is not None:
            tried.append("gemini")
            if _health.is_available("gemini"):
                try:
                    result = self._try_gemini(
                        prompt,
                        image_bytes=image_bytes,
                        mime_type=mime_type,
                        max_output_tokens=max_output_tokens,
                    )
                    if result:
                        return result
                except Exception as exc:
                    if _is_quota_error(exc):
                        self.log.warning(
                            "Gemini quota exhausted — instantly failing over to next provider. "
                            "Will not retry Gemini for %ds.", _PROVIDER_COOLDOWN_SECONDS
                        )
                    else:
                        self.log.warning("Gemini request failed: %s", exc)
            else:
                self.log.info("Gemini skipped (quota cooldown active) — trying next provider.")

        # 2) Gemini Web — browser-based vision fallback
        if has_image and self._gemini_web is not None:
            tried.append("gemini_web")
            try:
                result = self._gemini_web.complete(
                    prompt,
                    image_bytes=image_bytes,
                    max_output_tokens=max_output_tokens,
                )
                if result:
                    self.log.info("Gemini Web vision succeeded.")
                    return result
            except Exception as exc:
                self.log.warning("Gemini Web request failed: %s", exc)

        # 3) Text-only fallbacks — skip for vision tasks
        if has_image:
            self.log.warning(
                "Image task: skipping Groq and OpenRouter — "
                "they are text-only and cannot evaluate screenshots."
            )
            return None

        for provider in self._providers:
            if provider in tried:
                continue
            try:
                if provider == "groq":
                    result = self._try_groq(prompt, max_output_tokens=max_output_tokens)
                elif provider == "openrouter":
                    result = self._try_openrouter(prompt, max_output_tokens=max_output_tokens)
                else:
                    continue
                if result:
                    return result
            except Exception as exc:
                self.log.warning("%s request failed: %s", provider, exc)

        self.log.error("All providers exhausted — no result returned.")
        return None


_HASHTAG_RE = re.compile(r"#[A-Za-z0-9_]{2,}")


def parse_hashtags(raw_text: str, limit: int = 10) -> list[str]:
    """Extract and normalize hashtags from model output."""
    if not raw_text:
        return []

    seen: set[str] = set()
    tags: list[str] = []
    for tag in _HASHTAG_RE.findall(raw_text):
        clean = "#" + tag.lstrip("#").lower()
        if clean not in seen:
            seen.add(clean)
            tags.append(clean)
        if len(tags) >= limit:
            break
    return tags
