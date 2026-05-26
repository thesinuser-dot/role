#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# ai_router.py — Best-effort LLM routing for text + hashtag generation
#
# Priority:
#   1) Gemini (vision-capable; preferred when an image is available)
#   2) Groq    (OpenAI-compatible text fallback)
#   3) OpenRouter (OpenAI-compatible text fallback; can use openrouter/free)
#
# The router is intentionally conservative:
#   - It never tries to "remove" watermarks, handles, or personal tags.
#   - It only generates classification / metadata / hashtags for authorized use.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import re
from typing import Optional

import requests

from config import Config

try:
    import google.generativeai as genai
except Exception:  # pragma: no cover - optional dependency
    genai = None


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

    @property
    def gemini_ready(self) -> bool:
        return self._gemini_model is not None

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
        if not self._gemini_model:
            return None

        contents = [prompt]
        if image_bytes is not None:
            contents.append({"mime_type": mime_type, "data": image_bytes})

        response = self._gemini_model.generate_content(
            contents=contents,
            generation_config=genai.types.GenerationConfig(  # type: ignore[attr-defined]
                temperature=0.0,
                max_output_tokens=max_output_tokens,
            ),
            request_options={"timeout": 45},
        )
        return self._response_text(response)

    def _try_groq(self, prompt: str, *, max_output_tokens: int = 256) -> Optional[str]:
        if not Config.GROQ_API_KEY:
            return None
        return self._chat_completion(
            base_url="https://api.groq.com/openai/v1/chat/completions",
            api_key=Config.GROQ_API_KEY,
            model=Config.GROQ_MODEL,
            prompt=prompt,
            system_prompt=(
                "You are a concise assistant that returns only the requested text. "
                "Do not add commentary unless explicitly asked."
            ),
            max_tokens=max_output_tokens,
        )

    def _try_openrouter(self, prompt: str, *, max_output_tokens: int = 256) -> Optional[str]:
        if not Config.OPENROUTER_API_KEY:
            return None
        return self._chat_completion(
            base_url="https://openrouter.ai/api/v1/chat/completions",
            api_key=Config.OPENROUTER_API_KEY,
            model=Config.OPENROUTER_MODEL,
            prompt=prompt,
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
        Gemini is tried first for image inputs. If it is unavailable or fails,
        text-only fallbacks are tried in the configured provider order.
        """
        tried = []

        # 1) Gemini gets first crack, especially when we have an image.
        if "gemini" in self._providers and self._gemini_model is not None:
            tried.append("gemini")
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
                self.log.warning("Gemini request failed: %s", exc)

        # 2) Text-only fallbacks.
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
