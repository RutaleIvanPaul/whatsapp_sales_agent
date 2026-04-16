from __future__ import annotations

from groq import AsyncGroq

from app.adapters.vision.base import VisionAdapter
from app.utils.log import log

VISION_TIMEOUT_S = 8.0
VISION_MAX_TOKENS = 200
FALLBACK = "[image received, could not be described]"

DESCRIBE_PROMPT = (
    "In one sentence, describe what product this image shows. "
    "Focus on type, colour, brand, and key features. "
    "If no clear product is visible, say so."
)


class GroqVisionAdapter(VisionAdapter):
    """Vision via Groq (Llama 3.2 vision models). OpenAI-compatible content
    block shape: {type: "image_url", image_url: {"url": ...}}."""

    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._client = AsyncGroq(api_key=api_key, timeout=VISION_TIMEOUT_S)

    async def describe(self, image_url: str) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=VISION_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": DESCRIBE_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": image_url},
                            },
                        ],
                    }
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            return text or FALLBACK
        except Exception as e:
            log(
                "error",
                component="vision",
                provider="groq",
                error_type=type(e).__name__,
                message=str(e)[:200],
            )
            return FALLBACK
