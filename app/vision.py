import os
import re
import json
import time
import base64
import logging
import asyncio
from typing import Dict, Any

from PIL import Image

from app.ai_router import AIProviderRouter, parse_hashtags


VISION_PROMPT = """
You are an elite viral short-form content evaluator.

Your job:
- analyze reels/videos
- detect reposted or low quality content
- reject content with:
  - visible watermarks
  - usernames
  - creator tags
  - personal branding
  - stolen/reuploaded indicators
  - low quality edits
  - blurry footage
  - excessive text overlays

Accept:
- clean anime edits
- gaming edits
- meme edits
- cinematic edits
- motivational edits
- high engagement style clips

Return STRICT JSON:

{
  "approved": true,
  "reason": "why",
  "hashtags": [
    "#edit",
    "#viral",
    "#fyp"
  ],
  "caption": "short viral caption"
}

Hashtags must be viral and relevant.
No markdown.
No explanation outside JSON.
"""


class VisionEvaluator:

    def __init__(self):

        self.logger = logging.getLogger(
            "VisionEvaluator"
        )

        self.ai = AIProviderRouter()

        self.max_dim = int(
            os.getenv(
                "GEMINI_MAX_DIM",
                "720"
            )
        )

    async def evaluate(
        self,
        video_path: str,
        views: int = 0,
        likes: int = 0
    ) -> Dict[str, Any]:

        try:

            self.logger.info(
                f"Starting vision evaluation: {video_path}"
            )

            if not os.path.exists(video_path):

                return {
                    "approved": False,
                    "reason": "video file missing",
                    "hashtags": [],
                    "caption": ""
                }

            screenshots = await self.extract_frames(
                video_path
            )

            if not screenshots:

                return {
                    "approved": False,
                    "reason": "failed to extract frames",
                    "hashtags": [],
                    "caption": ""
                }

            prompt = f"""
{VISION_PROMPT}

Video metrics:
- Views: {views}
- Likes: {likes}

Analyze the screenshots carefully.
"""

            analysis = await self.run_ai_analysis(
                prompt,
                screenshots
            )

            if not analysis:

                return {
                    "approved": False,
                    "reason": "ai returned empty result",
                    "hashtags": [],
                    "caption": ""
                }

            return analysis

        except Exception as e:

            self.logger.exception(
                f"Vision evaluation failed: {e}"
            )

            return {
                "approved": False,
                "reason": str(e),
                "hashtags": [],
                "caption": ""
            }

    async def extract_frames(
        self,
        video_path: str
    ):

        try:

            import cv2

            frames = []

            cap = cv2.VideoCapture(
                video_path
            )

            total_frames = int(
                cap.get(
                    cv2.CAP_PROP_FRAME_COUNT
                )
            )

            if total_frames <= 0:
                return []

            sample_positions = [
                0.15,
                0.35,
                0.55,
                0.75,
            ]

            for pos in sample_positions:

                frame_no = int(
                    total_frames * pos
                )

                cap.set(
                    cv2.CAP_PROP_POS_FRAMES,
                    frame_no
                )

                success, frame = cap.read()

                if not success:
                    continue

                frame_path = (
                    f"/tmp/frame_{time.time()}_{frame_no}.jpg"
                )

                cv2.imwrite(
                    frame_path,
                    frame
                )

                frames.append(frame_path)

            cap.release()

            return frames

        except Exception as e:

            self.logger.exception(
                f"Frame extraction failed: {e}"
            )

            return []

    async def run_ai_analysis(
        self,
        prompt: str,
        screenshots
    ):

        try:

            combined_prompt = prompt

            for image_path in screenshots:

                image_data = self.prepare_image(
                    image_path
                )

                combined_prompt += (
                    "\n\n"
                    f"Screenshot(base64): {image_data[:150]}"
                )

            raw_response = await self.ai.generate(
                combined_prompt
            )

            return self.parse_response(
                raw_response
            )

        except Exception as e:

            self.logger.exception(
                f"AI analysis failed: {e}"
            )

            return {
                "approved": False,
                "reason": str(e),
                "hashtags": [],
                "caption": ""
            }

    def prepare_image(
        self,
        image_path: str
    ):

        try:

            image = Image.open(
                image_path
            )

            image.thumbnail(
                (
                    self.max_dim,
                    self.max_dim
                )
            )

            temp_path = (
                f"{image_path}_compressed.jpg"
            )

            image.save(
                temp_path,
                format="JPEG",
                quality=75
            )

            with open(
                temp_path,
                "rb"
            ) as f:

                encoded = base64.b64encode(
                    f.read()
                ).decode("utf-8")

            try:
                os.remove(temp_path)
            except:
                pass

            return encoded

        except Exception as e:

            self.logger.exception(
                f"Image prepare failed: {e}"
            )

            return ""

    def parse_response(
        self,
        raw_text: str
    ):

        try:

            if not raw_text:

                return {
                    "approved": False,
                    "reason": "empty ai response",
                    "hashtags": [],
                    "caption": ""
                }

            match = re.search(
                r"\{.*\}",
                raw_text,
                re.DOTALL
            )

            if match:
                raw_text = match.group(0)

            data = json.loads(
                raw_text
            )

            approved = bool(
                data.get(
                    "approved",
                    False
                )
            )

            reason = str(
                data.get(
                    "reason",
                    "unknown"
                )
            )

            hashtags = data.get(
                "hashtags",
                []
            )

            caption = str(
                data.get(
                    "caption",
                    ""
                )
            )

            if isinstance(
                hashtags,
                str
            ):
                hashtags = parse_hashtags(
                    hashtags
                )

            if not isinstance(
                hashtags,
                list
            ):
                hashtags = []

            hashtags = [
                str(tag).strip()
                for tag in hashtags
                if str(tag).startswith("#")
            ]

            hashtags = hashtags[:15]

            return {
                "approved": approved,
                "reason": reason,
                "hashtags": hashtags,
                "caption": caption
            }

        except Exception as e:

            self.logger.exception(
                f"Response parse failed: {e}"
            )

            return {
                "approved": False,
                "reason": "invalid ai json",
                "hashtags": [],
                "caption": ""
            }
