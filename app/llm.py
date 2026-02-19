from __future__ import annotations

import logging

from openai import AsyncOpenAI

logger = logging.getLogger("telegram-summary-bot")


SYSTEM_PROMPT = """你是 Telegram 群組的摘要編輯，請用繁體中文輸出，語氣可輕鬆但內容要準確。
你會收到「摘要中繼資料」與「對話紀錄」。

請盡量遵循這個格式輸出（不要加前言或結語）：

<群組名稱> Summary | <摘要區間> | <訊息總數> 則訊息

1. <主題一>
- <使用者A>：<該主題的重要訊息摘要>
- <使用者B>：<該主題的重要訊息摘要>

2. <主題二>
- ...

格式與內容規則：
1) 章節數量 3 到 7 個，依討論熱度分配。
2) 每個章節至少 2 個條列，優先寫有資訊量、決策、觀點衝突、笑點。
3) 條列盡量帶人名與重點，不要只寫抽象描述。
4) 人名不要 @ 帳號本人，並且對於長名字要簡短，避免對方被摘要打擾。
5) 不要杜撰，不確定就保守描述。
6) 不要輸出任何網址。
7) 若訊息太少，章節可以減少，但仍維持同樣結構。
"""


class OpenAISummarizer:
    def __init__(self, api_key: str, max_output_tokens: int):
        self.client = AsyncOpenAI(api_key=api_key)
        self.max_output_tokens = max_output_tokens

    async def summarize(self, *, transcript: str, model: str, api_style: str) -> str:
        style = self._resolve_api_style(model, api_style)

        if style == "responses":
            return await self._summarize_via_responses(
                transcript=transcript,
                model=model,
                max_output_tokens=self.max_output_tokens,
            )

        return await self._summarize_via_chat(
            transcript=transcript,
            model=model,
            max_tokens=self.max_output_tokens,
        )

    @staticmethod
    def _resolve_api_style(model: str, api_style: str) -> str:
        normalized = api_style.strip().lower()
        if normalized in {"responses", "chat"}:
            return normalized

        model_normalized = model.strip().lower()
        if model_normalized.startswith("gpt-5"):
            return "responses"
        return "chat"

    async def _summarize_via_responses(
        self,
        *,
        transcript: str,
        model: str,
        max_output_tokens: int,
    ) -> str:
        response = await self.client.responses.create(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": transcript}],
                },
            ],
            max_output_tokens=max_output_tokens,
        )
        text = self._extract_response_text(response)
        diagnostics = self._build_response_diagnostics(response)
        if text:
            logger.info(
                "Responses summary succeeded (model=%s text_len=%s max_output_tokens=%s status=%s response_id=%s).",
                model,
                len(text),
                max_output_tokens,
                diagnostics["status"],
                diagnostics["response_id"],
            )
            return text

        logger.error(
            "Responses summary returned empty text (model=%s max_output_tokens=%s diagnostics=%s)",
            model,
            max_output_tokens,
            diagnostics,
        )
        return ""

    async def _summarize_via_chat(self, *, transcript: str, model: str, max_tokens: int) -> str:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            max_tokens=max_tokens,
            temperature=0.7,
        )
        return (response.choices[0].message.content or "").strip()

    @staticmethod
    def _extract_response_text(response) -> str:
        direct = OpenAISummarizer._to_text(OpenAISummarizer._get(response, "output_text"))
        if direct:
            return direct

        chunks: list[str] = []
        output_items = OpenAISummarizer._get(response, "output", []) or []

        for item in output_items:
            item_level_text = OpenAISummarizer._to_text(OpenAISummarizer._get(item, "text"))
            if item_level_text:
                chunks.append(item_level_text)

            contents = OpenAISummarizer._get(item, "content", []) or []
            for content in contents:
                content_text = OpenAISummarizer._to_text(OpenAISummarizer._get(content, "text"))
                if content_text:
                    chunks.append(content_text)

                # Some SDK payloads expose output text as value/output_text fields.
                value_text = OpenAISummarizer._to_text(OpenAISummarizer._get(content, "value"))
                if value_text:
                    chunks.append(value_text)

                output_text = OpenAISummarizer._to_text(OpenAISummarizer._get(content, "output_text"))
                if output_text:
                    chunks.append(output_text)

        return "\n".join(chunks).strip()

    @staticmethod
    def _get(obj, key: str, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def _to_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            chunks = [OpenAISummarizer._to_text(v) for v in value]
            return "\n".join([c for c in chunks if c]).strip()
        if isinstance(value, dict):
            for key in ("text", "value", "output_text"):
                text = OpenAISummarizer._to_text(value.get(key))
                if text:
                    return text
            return ""
        # Pydantic/object style text containers.
        for key in ("text", "value", "output_text"):
            text = OpenAISummarizer._to_text(getattr(value, key, None))
            if text:
                return text
        return ""

    @staticmethod
    def _build_response_diagnostics(response) -> dict:
        output_items = OpenAISummarizer._get(response, "output", []) or []
        incomplete = OpenAISummarizer._get(response, "incomplete_details")
        if isinstance(incomplete, dict):
            incomplete_reason = incomplete.get("reason")
        else:
            incomplete_reason = OpenAISummarizer._get(incomplete, "reason")

        first_item = output_items[0] if output_items else None
        first_content = OpenAISummarizer._get(first_item, "content", []) or []
        first_content_types = [
            OpenAISummarizer._get(item, "type")
            for item in first_content[:5]
        ]

        return {
            "response_id": OpenAISummarizer._get(response, "id"),
            "status": OpenAISummarizer._get(response, "status"),
            "incomplete_reason": incomplete_reason,
            "error": OpenAISummarizer._get(response, "error"),
            "output_count": len(output_items),
            "output_types": [
                OpenAISummarizer._get(item, "type")
                for item in output_items[:5]
            ],
            "first_output_content_types": first_content_types,
            "usage": OpenAISummarizer._get(response, "usage"),
        }
