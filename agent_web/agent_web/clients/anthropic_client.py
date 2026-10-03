"""
ModelClient implementation dùng Anthropic API.

API key LUÔN lấy từ biến môi trường ANTHROPIC_API_KEY (mục 8) — không bao
giờ hardcode hoặc ghi log giá trị key.
"""

from __future__ import annotations

import json
import os
import re

from anthropic import AsyncAnthropic


class AnthropicModelClient:
    def __init__(self, model: str = "claude-sonnet-4-6", *, api_key: str | None = None):
        # api_key=None -> SDK tự đọc từ env ANTHROPIC_API_KEY
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "Thiếu ANTHROPIC_API_KEY trong biến môi trường. Không "
                "hardcode API key trong code."
            )
        self._client = AsyncAnthropic(api_key=key)
        self._model = model

    @property
    def model_name(self) -> str:
        return self._model

    async def complete_json(
        self, *, system: str, user: str, max_tokens: int = 2000
    ) -> dict:
        system_full = (
            system
            + "\n\nCHỈ trả về một JSON object hợp lệ, không kèm markdown "
            "fence, không kèm văn bản giải thích nào khác."
        )
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system_full,
            messages=[{"role": "user", "content": user}],
        )
        text_parts = [
            block.text for block in response.content if getattr(block, "type", "") == "text"
        ]
        text = "".join(text_parts).strip()
        text = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Model không trả JSON hợp lệ: {exc}\nRaw: {text[:500]}")
