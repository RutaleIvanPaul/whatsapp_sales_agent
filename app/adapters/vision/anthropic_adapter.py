from __future__ import annotations

from anthropic import AsyncAnthropic

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


class AnthropicVisionAdapter(VisionAdapter):
    def __init__(self, api_key: str, model: str) -> None:
        self._model = model
        self._client = AsyncAnthropic(api_key=api_key, timeout=VISION_TIMEOUT_S)

    async def describe(self, image_url: str) -> str:
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=VISION_MAX_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {"type": "url", "url": image_url},
                            },
                            {"type": "text", "text": DESCRIBE_PROMPT},
                        ],
                    }
                ],
            )
            text_parts = [b.text for b in response.content if b.type == "text"]
            description = "".join(text_parts).strip()
            return description or FALLBACK
        except Exception as e:
            log(
                "error",
                component="vision",
                error_type=type(e).__name__,
                message=str(e)[:200],
            )
            return FALLBACK
