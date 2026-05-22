#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# vision.py — Two-stage visual quality filter
#
# Stage 1: Pillow border-pixel analysis (free — no API call)
#   Detects letterbox / pillarbox black bars.
#   Fails closed on exception — a corrupt screenshot is not a pass.
#
# Stage 2: Gemini 1.5 Flash multimodal evaluation (API call)
#   Checks for watermarks, handles, platform branding, low quality.
#   Transient errors (network, quota) are retried up to GEMINI_RETRIES times.
#   After retries exhausted: fail closed — a broken Gemini key or quota
#   exhaustion must NOT silently pass every reel for the rest of the run.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import time
from io import BytesIO
from typing import List, Tuple

from PIL import Image
import google.generativeai as genai

from config import Config


class VisionEvaluator:
    _GEMINI_PROMPT = (
        "You are a zero-tolerance content quality filter for viral social media clips.\n"
        "Inspect this video frame screenshot carefully.\n\n"
        "Output exactly 'FAILED' if ANY of the following are present:\n"
        "  - An @username, display name, or profile handle overlaid anywhere on the frame\n"
        "  - TikTok watermark: spinning record icon, TikTok logo, username with music note\n"
        "  - Instagram or Facebook watermark embedded INSIDE the video content\n"
        "  - Any other social media platform logo or icon burned into the video\n"
        "  - Horizontal black bars at both top AND bottom (letterboxed 16:9 in 9:16 frame)\n"
        "  - Vertical black bars on both left AND right (pillarboxed 4:3 or wider)\n"
        "  - Blurry, heavily compressed, or clearly low-resolution video quality\n"
        "  - Hard-burned subtitle text overlaying the video content\n\n"
        "Output exactly 'PASSED' if ALL of the following are true:\n"
        "  - Native vertical 9:16 format — no black bars on any edge\n"
        "  - No watermarks, handles, or platform branding anywhere in the frame\n"
        "  - Clean, sharp, high-quality video content\n\n"
        "Respond with exactly ONE word — either PASSED or FAILED. No other text."
    )

    # Exceptions that indicate a transient Gemini error worth retrying
    _RETRYABLE_MESSAGES = (
        "quota", "rate", "503", "502", "timeout", "deadline", "unavailable",
        "resource_exhausted", "internal",
    )

    def __init__(self, gemini_api_key: str):
        self.log = logging.getLogger("VisionEvaluator")
        self.gemini_enabled = False
        if gemini_api_key:
            try:
                genai.configure(api_key=gemini_api_key)
                self._model = genai.GenerativeModel(Config.GEMINI_MODEL)
                self.gemini_enabled = True
                self.log.info(f"Gemini Vision ready — model={Config.GEMINI_MODEL}")
            except Exception as exc:
                # Init failure is permanent (bad key, missing package, etc.)
                self.log.error(f"Gemini init failed: {exc}")
        else:
            self.log.warning("GEMINI_API_KEY not set — Stage 2 vision disabled.")

    # ── Stage 1: local Pillow pixel analysis ──────────────────────────────────

    def _sample_strip(
        self, img: Image.Image, box: Tuple[int, int, int, int], step: int = 4
    ) -> List[Tuple[int, int, int]]:
        x0, y0, x1, y1 = box
        pixels: List[Tuple[int, int, int]] = []
        for y in range(y0, y1, max(1, step)):
            for x in range(x0, x1, max(1, step)):
                px = img.getpixel((x, y))
                pixels.append((px[0], px[1], px[2]))
        return pixels

    def _is_black_strip(self, pixels: List[Tuple[int, int, int]]) -> bool:
        if not pixels:
            return False
        thr = Config.BLACK_THRESHOLD
        ratio = Config.BLACK_BAR_RATIO
        black = sum(1 for r, g, b in pixels if r < thr and g < thr and b < thr)
        return (black / len(pixels)) >= ratio

    def check_aspect_ratio_local(self, screenshot_bytes: bytes) -> Tuple[bool, str]:
        try:
            img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
            w, h = img.size
            bh = max(2, int(h * Config.BORDER_SAMPLE_PCT))
            bw = max(2, int(w * Config.BORDER_SAMPLE_PCT))
            top_black    = self._is_black_strip(self._sample_strip(img, (0, 0, w, bh)))
            bottom_black = self._is_black_strip(self._sample_strip(img, (0, h - bh, w, h)))
            left_black   = self._is_black_strip(self._sample_strip(img, (0, 0, bw, h)))
            right_black  = self._is_black_strip(self._sample_strip(img, (w - bw, 0, w, h)))
            self.log.debug(
                f"Border: top={top_black} bottom={bottom_black} "
                f"left={left_black} right={right_black} ({w}x{h})"
            )
            if top_black and bottom_black:
                return False, "Letterbox bars (top+bottom black)"
            if left_black and right_black:
                return False, "Pillarbox bars (left+right black)"
            return True, "No black bars"
        except Exception as exc:
            # Fail closed: a corrupt screenshot is not a reason to allow through.
            self.log.error(f"Stage-1 pixel check failed (fail-closed): {exc}")
            return False, f"Stage-1 error (fail-closed): {exc}"

    # ── Stage 2: Gemini multimodal ────────────────────────────────────────────

    def _compress_for_gemini(self, screenshot_bytes: bytes) -> bytes:
        img = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
        max_dim = Config.GEMINI_MAX_DIM
        if max(img.width, img.height) > max_dim:
            scale = max_dim / max(img.width, img.height)
            img = img.resize(
                (int(img.width * scale), int(img.height * scale)), Image.LANCZOS
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()

    def _is_retryable(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(tok in msg for tok in self._RETRYABLE_MESSAGES)

    def check_with_gemini(self, screenshot_bytes: bytes) -> Tuple[bool, str]:
        if not self.gemini_enabled:
            return True, "Gemini disabled (no API key) — skipped"

        compressed = self._compress_for_gemini(screenshot_bytes)
        last_exc: Optional[Exception] = None

        for attempt in range(1, Config.GEMINI_RETRIES + 2):  # +2 = first try + N retries
            try:
                response = self._model.generate_content(
                    [self._GEMINI_PROMPT, {"mime_type": "image/jpeg", "data": compressed}],
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.0, max_output_tokens=8
                    ),
                    request_options={"timeout": 20},
                )
                raw_text = (response.text or "").strip().upper()
                self.log.info(f"Gemini raw response (attempt {attempt}): '{raw_text}'")
                if "PASSED" in raw_text:
                    return True, "Gemini Vision: PASSED"
                if "FAILED" in raw_text:
                    return False, "Gemini Vision: FAILED"
                # Ambiguous response — treat as FAILED, no retry needed
                self.log.warning(f"Ambiguous Gemini response: '{raw_text}' — treating as FAILED")
                return False, f"Gemini ambiguous response: '{raw_text}'"

            except Exception as exc:
                last_exc = exc
                if self._is_retryable(exc) and attempt <= Config.GEMINI_RETRIES:
                    wait = 2 ** attempt
                    self.log.warning(
                        f"Gemini transient error (attempt {attempt}/{Config.GEMINI_RETRIES + 1}): "
                        f"{exc} — retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    break

        # All attempts exhausted — fail closed.
        # A broken API key or quota exhaustion must NOT silently pass every reel.
        self.log.error(
            f"Gemini failed after {Config.GEMINI_RETRIES + 1} attempt(s) (fail-closed): {last_exc}"
        )
        return False, f"Gemini API error (fail-closed): {last_exc}"

    # ── Combined evaluate ─────────────────────────────────────────────────────

    def evaluate(self, screenshot_bytes: bytes) -> Tuple[bool, str]:
        self.log.info("Vision Stage 1: local border pixel analysis")
        ok, reason = self.check_aspect_ratio_local(screenshot_bytes)
        if not ok:
            self.log.warning(f"Stage 1 FAILED: {reason}")
            return False, reason
        self.log.info(f"Stage 1 PASSED: {reason}")

        self.log.info("Vision Stage 2: Gemini multimodal evaluation")
        ok, reason = self.check_with_gemini(screenshot_bytes)
        if not ok:
            self.log.warning(f"Stage 2 FAILED: {reason}")
            return False, reason
        self.log.info(f"Stage 2 PASSED: {reason}")
        return True, "All vision checks passed"
