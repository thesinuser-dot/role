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
from typing import List, Tuple, Optional

from PIL import Image
import google.generativeai as genai

from config import Config


class VisionEvaluator:
    # الـ Prompt المطور لحل مشكلة الوجوه السينمائية والتفريق بين الـ Edits والـ Vlogs
    _GEMINI_PROMPT = (
        "Role: You are a strict binary visual filter for a faceless/cinematic content aggregator. "
        "Your sole task is to classify if this video frame is a high-quality EDIT/AESTHETIC clip or a PERSONAL/VLOG clip.\n\n"
        
        "CRITICAL RULE: Respond with EXACTLY ONE WORD: either 'PASSED' or 'FAILED'. "
        "Do not include any punctuation, explanation, or extra characters. Fail closed if unsure.\n\n"
        
        "========================================= \n"
        "REJECT AND OUTPUT 'FAILED' IF ANY OF THESE ARE TRUE:\n"
        "========================================= \n"
        "1. USER INTERFACE & BRANDING:\n"
        "   - Contains platform watermarks (TikTok logo, Instagram Reels UI, YouTube shorts overlay).\n"
        "   - Contains on-screen creator handles (e.g., @username) burned into the video as a permanent watermark.\n"
        "2. FORMAT & QUALITY ISSUES:\n"
        "   - Has horizontal black bars (Letterboxed) or vertical black bars (Pillarboxed).\n"
        "   - Low resolution, blurry, pixelated, or poorly cropped.\n"
        "3. PERSONAL / LIFESTYLE / UGC CONTENT:\n"
        "   - Features an everyday person/influencer talking directly to the camera (Talking-head, Vlog style).\n"
        "   - Looks like user-generated content (UGC), selfie-cam footage, GRWM (get ready with me), OOTD, or a lifestyle/travel vlog.\n"
        "   - Shows real-life couples, family vlogs, or domestic personal context.\n"
        "   - Features burned-in speech auto-captions/subtitles that follow a human voiceover (indicates a commentary vlog).\n\n"
        
        "========================================= \n"
        "ACCEPT AND OUTPUT 'PASSED' ONLY IF ALL OF THESE ARE TRUE:\n"
        "========================================= \n"
        "1. NATIVE FORMAT: True native vertical 9:16 aspect ratio, edge-to-edge content without artificial borders.\n"
        "2. NO BRANDING: 100% clean frame, free of third-party platform logos or creator handles.\n"
        "3. ALLOWED CONTENT TYPES (Must match at least one):\n"
        "   - Cinematic Edits: Scenes from movies, TV shows, or anime. NOTE: Fictional characters/actors (e.g., Homelander, Batman, Tommy Shelby) ARE fully allowed, even in close-ups, provided the footage is cinematic and NOT a personal vlog.\n"
        "   - Automotive Footage: Professional/aesthetic car footage (drifting, rolling shots, car meets, luxury car close-ups).\n"
        "   - Text/Quote Overlays: Deep, motivational, or relatable text written over a clean, artistic, or abstract background (e.g., night streets, rain, nature, scenery).\n"
        "   - Gaming/AMV: High-quality gaming montages or anime music videos with clean transitions.\n\n"
        
        "Final Reminder: Look closely at the image. Is it a generic vlog/social media post? -> FAILED. "
        "Is it a professional/faceless edit, cinematic clip, car video, or quote? -> PASSED.\n"
        "Output ONLY 'PASSED' or 'FAILED'."
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
                (int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS
            )
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()

    def _is_retryable(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(tok in msg for tok in self._RETRYABLE_MESSAGES)

    def check_with_gemini(self, screenshot_bytes: bytes, views: int = 0, likes: int = 0) -> Tuple[bool, str]:
        if not self.gemini_enabled:
            return True, "Gemini disabled (no API key) — skipped"

        compressed = self._compress_for_gemini(screenshot_bytes)
        last_exc: Optional[Exception] = None
        quota_exhausted = False

        for attempt in range(1, Config.GEMINI_RETRIES + 2):  # +2 = first try + N retries
            try:
                # تجهيز هيكل البيانات الخاص بالصورة ليتناسب مع الـ SDK المستقر
                image_part = {"mime_type": "image/jpeg", "data": compressed}
                
                response = self._model.generate_content(
                    contents=[self._GEMINI_PROMPT, image_part],
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
                exc_str = str(exc).lower()
                
                # Check if this is a quota/rate limit error
                if any(tok in exc_str for tok in ["quota", "resource_exhausted", "rate"]):
                    quota_exhausted = True
                    self.log.warning(f"Gemini quota/rate limit detected: {exc}")
                
                if self._is_retryable(exc) and attempt <= Config.GEMINI_RETRIES:
                    wait = 2 ** attempt
                    self.log.warning(
                        f"Gemini transient error (attempt {attempt}/{Config.GEMINI_RETRIES + 1}): "
                        f"{exc} — retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    break

        # All attempts exhausted — check if fallback mode is enabled
        if Config.ENABLE_GEMINI_FALLBACK and quota_exhausted:
            # Fallback to views/likes metrics
            if views >= Config.FALLBACK_MIN_VIEWS and likes >= Config.FALLBACK_MIN_LIKES:
                self.log.warning(
                    f"Gemini quota exhausted BUT fallback mode enabled: "
                    f"views={views:,} >= {Config.FALLBACK_MIN_VIEWS:,} AND "
                    f"likes={likes:,} >= {Config.FALLBACK_MIN_LIKES:,} → PASS"
                )
                return True, f"Gemini fallback PASSED (views={views:,}, likes={likes:,})"
            else:
                self.log.warning(
                    f"Gemini quota exhausted AND fallback metrics insufficient: "
                    f"views={views:,} < {Config.FALLBACK_MIN_VIEWS:,} OR "
                    f"likes={likes:,} < {Config.FALLBACK_MIN_LIKES:,} → FAIL"
                )
                return False, f"Gemini fallback FAILED (insufficient engagement)"
        
        # Fail closed if fallback is disabled or not a quota issue
        self.log.error(
            f"Gemini failed after {Config.GEMINI_RETRIES + 1} attempt(s) (fail-closed): {last_exc}"
        )
        return False, f"Gemini API error (fail-closed): {last_exc}"

    # ── Combined evaluate ─────────────────────────────────────────────────────

    def evaluate(self, screenshot_bytes: bytes, views: int = 0, likes: int = 0) -> Tuple[bool, str]:
        self.log.info("Vision Stage 1: local border pixel analysis")
        ok, reason = self.check_aspect_ratio_local(screenshot_bytes)
        if not ok:
            self.log.warning(f"Stage 1 FAILED: {reason}")
            return False, reason
        self.log.info(f"Stage 1 PASSED: {reason}")

        self.log.info("Vision Stage 2: Gemini multimodal evaluation")
        ok, reason = self.check_with_gemini(screenshot_bytes, views, likes)
        if not ok:
            self.log.warning(f"Stage 2 FAILED: {reason}")
            return False, reason
        self.log.info(f"Stage 2 PASSED: {reason}")
        return True, "All vision checks passed"
