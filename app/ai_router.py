import os
import json
import asyncio
import logging
from typing import Optional

import requests

logger = logging.getLogger("AIRouter")


def parse_hashtags(text: str):
    if not text:
        return []

    tags = []
    words = text.replace("\n", " ").split()

    for word in words:
        if word.startswith("#"):
            clean = "".join(c for c in word if c.isalnum() or c == "#")
            if len(clean) > 1:
                tags.append(clean.lower())

    return list(dict.fromkeys(tags))[:15]


class AIProviderRouter:
    def __init__(self):
        self.gemini_key = os.getenv("GEMINI_API_KEY", "")
        self.groq_key = os.getenv("GROQ_API_KEY", "")
        self.openrouter_key = os.getenv("OPENROUTER_API_KEY", "")

        self.gemini_model = os.getenv(
            "GEMINI_MODEL",
            "gemini-2.0-flash"
        )

        self.groq_model = os.getenv(
            "GROQ_MODEL",
            "llama-3.3-70b-versatile"
        )

        self.openrouter_model = os.getenv(
            "OPENROUTER_MODEL",
            "meta-llama/llama-3.3-70b-instruct"
        )

    async def generate(
        self,
        prompt: str
    ) -> str:

        providers = [
            ("gemini", self._gemini),
            ("groq", self._groq),
            ("openrouter", self._openrouter),
        ]

        last_error = None

        for provider_name, provider_func in providers:

            try:
                logger.info(
                    f"Trying provider: {provider_name}"
                )

                result = await provider_func(prompt)

                if result:
                    logger.info(
                        f"{provider_name} success"
                    )
                    return result

            except Exception as e:
                last_error = e
                err = str(e).lower()

                quota_error = any(x in err for x in [
                    "429",
                    "quota",
                    "resource_exhausted",
                    "rate limit",
                    "exceeded your current quota"
                ])

                if quota_error:
                    logger.warning(
                        f"{provider_name} quota exhausted -> fallback"
                    )
                    continue

                logger.warning(
                    f"{provider_name} failed: {e}"
                )

                await asyncio.sleep(1)

        raise RuntimeError(
            f"All AI providers failed: {last_error}"
        )

    async def _gemini(self, prompt: str):

        if not self.gemini_key:
            raise RuntimeError("Missing GEMINI_API_KEY")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent?key={self.gemini_key}"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": prompt
                        }
                    ]
                }
            ]
        }

        response = requests.post(
            url,
            json=payload,
            timeout=45
        )

        if response.status_code != 200:
            raise RuntimeError(response.text)

        data = response.json()

        return (
            data["candidates"][0]
            ["content"]["parts"][0]["text"]
        )

    async def _groq(self, prompt: str):

        if not self.groq_key:
            raise RuntimeError("Missing GROQ_API_KEY")

        url = "https://api.groq.com/openai/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.groq_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.groq_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.3,
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=45
        )

        if response.status_code != 200:
            raise RuntimeError(response.text)

        data = response.json()

        return (
            data["choices"][0]
            ["message"]["content"]
        )

    async def _openrouter(self, prompt: str):

        if not self.openrouter_key:
            raise RuntimeError(
                "Missing OPENROUTER_API_KEY"
            )

        url = "https://openrouter.ai/api/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {self.openrouter_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self.openrouter_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "temperature": 0.3,
        }

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=45
        )

        if response.status_code != 200:
            raise RuntimeError(response.text)

        data = response.json()

        return (
            data["choices"][0]
            ["message"]["content"]
        )
