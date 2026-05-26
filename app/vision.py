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

from ai_router import AIProviderRouter, parse_hashtags
from config import Config
from gemini_web_browser import GeminiWebBrowser


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
        self._ai = AIProviderRouter()
        self._model = self._ai._gemini_model
        self.gemini_enabled = self._ai.gemini_ready
        if self.gemini_enabled:
            self.log.info(f"Gemini Vision ready — model={Config.GEMINI_MODEL}")
        else:
            self.log.warning("Gemini API key not set — will use Gemini Web Browser (visible) as fallback.")

        # Gemini Web Browser fallback — visible Chromium window when API key absent
        self._gemini_web_browser: Optional[GeminiWebBrowser] = None
        if not self.gemini_enabled:
            self._gemini_web_browser = GeminiWebBrowser(Config.GEMINI_COOKIES)
            self.log.info("GeminiWebBrowser (visible) fallback initialised.")

        # Will be set by agent after notifier is available
        self._notifier = None

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

    def _get_response_text(self, response) -> Optional[str]:
        """
        Safely extract text from a Gemini response.

        response.text raises ValueError when the response is blocked by safety
        filters or has no text parts — this catches that and falls back to
        manually reading the candidates so we can log the actual block reason.
        """
        try:
            text = response.text
            if text:
                return text.strip()
        except ValueError:
            pass  # blocked or empty — try candidates manually

        try:
            for candidate in response.candidates or []:
                # Log finish reason so it shows up in the run log
                finish = getattr(candidate, "finish_reason", None)
                if finish and str(finish) not in ("1", "STOP"):
                    self.log.warning(f"Gemini candidate finish_reason={finish}")
                for part in getattr(candidate.content, "parts", []) or []:
                    t = getattr(part, "text", None)
                    if t and t.strip():
                        return t.strip()
        except Exception as exc:
            self.log.debug(f"Candidate text extraction error: {exc}")

        # Log prompt_feedback if available (explains safety blocks)
        try:
            fb = response.prompt_feedback
            if fb:
                self.log.warning(f"Gemini prompt_feedback: {fb}")
        except Exception:
            pass

        return None

    def check_with_gemini(self, screenshot_bytes: bytes, views: int = 0, likes: int = 0) -> Tuple[bool, str]:
        # ── Gemini Web Browser fallback (no API key) ──────────────────────────
        if not self.gemini_enabled:
            if self._gemini_web_browser is not None:
                self.log.info("No Gemini API key — using GeminiWebBrowser (visible browser)...")
                try:
                    response_text, snap = self._gemini_web_browser.ask(
                        self._GEMINI_PROMPT,
                        image_bytes=self._compress_for_gemini(screenshot_bytes),
                    )
                    if snap and self._notifier:
                        try:
                            self._notifier.send_photo(
                                snap,
                                caption="🌐 <b>Gemini Web Vision Check</b> — screenshot of AI response",
                            )
                        except Exception as exc:
                            self.log.warning(f"Could not send Gemini Web screenshot: {exc}")
                    if response_text:
                        upper = response_text.upper()
                        if "PASSED" in upper:
                            return True, "Gemini Web Vision: PASSED"
                        if "FAILED" in upper:
                            return False, "Gemini Web Vision: FAILED"
                    self.log.warning("Gemini Web returned no usable response — fail-closed")
                    return False, "Gemini Web: no usable response (fail-closed)"
                except Exception as exc:
                    self.log.error(f"GeminiWebBrowser check failed: {exc}")
                    return False, f"Gemini Web error (fail-closed): {exc}"
            return True, "Gemini disabled (no API key) — skipped"

        compressed = self._compress_for_gemini(screenshot_bytes)
        last_exc: Optional[Exception] = None
        quota_exhausted = False

        for attempt in range(1, Config.GEMINI_RETRIES + 2):  # +2 = first try + N retries
            try:
                image_part = {"mime_type": "image/jpeg", "data": compressed}

                response = self._model.generate_content(
                    contents=[self._GEMINI_PROMPT, image_part],
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.0,
                        max_output_tokens=32,   # was 8 — too tight, model needs room
                    ),
                    request_options={"timeout": 45},  # was 20 — too short for cold starts
                )

                raw_text = self._get_response_text(response)
                self.log.info(f"Gemini raw response (attempt {attempt}): {raw_text!r}")

                if raw_text is None:
                    # Blocked or empty — not a network error, don't retry
                    self.log.warning("Gemini returned empty/blocked response — treating as FAILED")
                    return False, "Gemini blocked/empty response (FAILED)"

                upper = raw_text.upper()
                if "PASSED" in upper:
                    return True, "Gemini Vision: PASSED"
                if "FAILED" in upper:
                    return False, "Gemini Vision: FAILED"

                # Ambiguous — log and fail, don't retry
                self.log.warning(f"Ambiguous Gemini response: {raw_text!r} — treating as FAILED")
                return False, f"Gemini ambiguous response: {raw_text!r}"

            except Exception as exc:
                last_exc = exc
                exc_str = str(exc).lower()

                if any(tok in exc_str for tok in ["quota", "resource_exhausted", "rate"]):
                    quota_exhausted = True
                    self.log.warning(f"Gemini quota/rate limit (attempt {attempt}): {exc}")
                else:
                    self.log.warning(f"Gemini error (attempt {attempt}): {type(exc).__name__}: {exc}")

                if self._is_retryable(exc) and attempt <= Config.GEMINI_RETRIES:
                    wait = 2 ** attempt
                    self.log.warning(f"Retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    break

        # ── All attempts exhausted ────────────────────────────────────────────
        if Config.ENABLE_GEMINI_FALLBACK and quota_exhausted:
            if views >= Config.FALLBACK_MIN_VIEWS and likes >= Config.FALLBACK_MIN_LIKES:
                self.log.warning(
                    f"Gemini quota exhausted — fallback PASSED "
                    f"(views={views:,}, likes={likes:,})"
                )
                return True, f"Gemini fallback PASSED (views={views:,}, likes={likes:,})"
            else:
                self.log.warning(
                    f"Gemini quota exhausted — fallback FAILED "
                    f"(views={views:,}, likes={likes:,})"
                )
                return False, "Gemini fallback FAILED (insufficient engagement)"

        self.log.error(
            f"Gemini failed after {Config.GEMINI_RETRIES + 1} attempt(s) "
            f"(fail-closed): {type(last_exc).__name__}: {last_exc}"
        )
        return False, f"Gemini API error (fail-closed): {last_exc}"

    # ── Startup self-test ────────────────────────────────────────────────────

    def test_gemini(self) -> tuple:
        """
        Send a tiny synthetic image (solid grey JPEG) to Gemini and verify
        we get any text response back (not an exception).

        Returns (ok: bool, message: str).
        Called once at agent startup so the user knows immediately if the
        key is wrong / quota is zero / the model name is invalid.
        """
        if not self.gemini_enabled:
            return False, "GEMINI_API_KEY is not set — Gemini is disabled"

        try:
            # 64x64 neutral grey — safe content, no chance of a safety block
            img = Image.new("RGB", (64, 64), color=(128, 128, 128))
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=85)
            test_bytes = buf.getvalue()

            image_part = {"mime_type": "image/jpeg", "data": test_bytes}
            response = self._model.generate_content(
                contents=["Reply with exactly one word: READY", image_part],
                generation_config=genai.types.GenerationConfig(
                    temperature=0.0, max_output_tokens=16,
                ),
                request_options={"timeout": 45},
            )
            text = self._get_response_text(response)
            if text is not None:
                return True, f"Gemini OK — model={Config.GEMINI_MODEL!r}, response={text!r}"
            return False, f"Gemini returned empty/blocked response on test call"

        except Exception as exc:
            return False, f"Gemini test failed: {type(exc).__name__}: {exc}"

    # ── Combined evaluate ─────────────────────────────────────────────────────

    # ── Best-in-class hashtag prompt ─────────────────────────────────────────
    _HASHTAG_SYSTEM_PROMPT = (
        "You are a TikTok hashtag strategist for a faceless/cinematic content page. "
        "Your job is to generate high-reach, trend-aligned hashtags for reposted edits. "
        "You MUST follow these rules STRICTLY:\n\n"
        "BANNED — never include:\n"
        "  * Any @username, creator handle, or personal tag\n"
        "  * Platform names as hashtags: #instagram #reels #ig\n"
        "  * Watermark-related or source-credit tags\n"
        "  * Overly generic junk: #love #follow #like #share\n"
        "  * Any tag that identifies the original creator or source account\n\n"
        "REQUIRED — always include a mix of:\n"
        "  * 1-2 ultra-broad reach tags (#fyp #viral)\n"
        "  * 3-5 niche tags matching the visual theme\n"
        "    (e.g. #cinedit #movieedit #animeedit #caredits #editaudio #aestheticedit)\n"
        "  * 1-2 trending format tags (#trending #2025edit #satisfying)\n\n"
        "OUTPUT FORMAT: Return ONLY hashtags separated by spaces. "
        "No numbers, no explanation, no punctuation other than # signs. "
        "Minimum 8, maximum 12 hashtags total."
    )

    _HASHTAG_USER_PROMPT_TMPL = (
        "Analyze this reel and generate the best TikTok hashtags.\n\n"
        "Context:\n"
        "  Views: {views}\n"
        "  Likes: {likes}\n"
        "  Caption: {caption}\n\n"
        "Identify the content type from the screenshot:\n"
        "  - Movie/TV/anime edit -> #cinedit #moviescene #sceneedit etc.\n"
        "  - Car/automotive edit -> #carsedit #caredits #automotivelife etc.\n"
        "  - Motivational/quote  -> #motivation #quotestoliveby etc.\n"
        "  - Gaming/AMV          -> gaming or anime tags.\n\n"
        "Generate 8-12 TikTok hashtags (space-separated, each starting with #). "
        "NO @handles, NO platform names, NO personal credits."
    )

    def suggest_hashtags(
        self,
        screenshot_bytes: bytes,
        views: int = 0,
        likes: int = 0,
        caption: str = "",
        limit: int = 12,
    ) -> list[str]:
        """
        Ask the configured AI stack (Gemini -> Groq -> OpenRouter) for hashtags.
        Uses a strict prompt that strips watermarks, handles, and personal tags.
        Automatically falls back across all 3 providers — always returns useful tags.
        """
        compressed = self._compress_for_gemini(screenshot_bytes)
        user_prompt = self._HASHTAG_USER_PROMPT_TMPL.format(
            views=f"{views:,}",
            likes=f"{likes:,}",
            caption=caption[:300] if caption else "(no caption)",
        )
        # For Gemini, system + user are merged (no separate system role in basic API)
        gemini_prompt = self._HASHTAG_SYSTEM_PROMPT + "\n\n" + user_prompt
        text_prompt   = self._HASHTAG_SYSTEM_PROMPT + "\n\n" + user_prompt

        raw = None

        # 1) Gemini with screenshot (best: vision-aware hashtags)
        try:
            raw = self._ai._try_gemini(gemini_prompt, image_bytes=compressed, max_output_tokens=150)
            if raw:
                self.log.info("Hashtag generation: Gemini succeeded")
        except Exception as exc:
            self.log.warning(f"Hashtag Gemini failed: {exc}")

        # 2) Groq + LLaMA text fallback
        if not raw:
            try:
                raw = self._ai._try_groq(text_prompt, max_output_tokens=150)
                if raw:
                    self.log.info("Hashtag generation: Groq/LLaMA succeeded")
            except Exception as exc:
                self.log.warning(f"Hashtag Groq failed: {exc}")

        # 3) OpenRouter free-tier fallback
        if not raw:
            try:
                raw = self._ai._try_openrouter(text_prompt, max_output_tokens=150)
                if raw:
                    self.log.info("Hashtag generation: OpenRouter succeeded")
            except Exception as exc:
                self.log.warning(f"Hashtag OpenRouter failed: {exc}")

        tags = parse_hashtags(raw or "", limit=limit)
        if len(tags) >= 4:
            return tags

        # Smart content-aware fallback — always return something TikTok-useful
        self.log.warning("All AI hashtag providers failed or returned <4 tags — using smart fallback")
        fallback_base = ["#fyp", "#viral", "#edit", "#trending"]
        caption_lower = (caption or "").lower()
        if any(w in caption_lower for w in ("anime", "amv", "naruto", "demon", "manga")):
            fallback_base += ["#animeedit", "#animetiktok", "#animeaesthetic"]
        elif any(w in caption_lower for w in ("car", "drift", "bmw", "m5", "luxury", "auto")):
            fallback_base += ["#carsedit", "#automotivelife", "#carsoftiktok"]
        elif any(w in caption_lower for w in ("movie", "scene", "edit", "cinematic", "film")):
            fallback_base += ["#cinedit", "#moviescene", "#sceneedit"]
        elif any(w in caption_lower for w in ("quote", "sigma", "motivation", "relatable")):
            fallback_base += ["#motivation", "#quotestoliveby", "#sigmagrindset"]
        else:
            fallback_base += ["#aesthetic", "#cinematic", "#editaudio"]

        seen: set[str] = set()
        result = []
        for t in (tags + fallback_base):
            key = t.lower()
            if key not in seen:
                seen.add(key)
                result.append(t)
            if len(result) >= limit:
                break
        return result

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
