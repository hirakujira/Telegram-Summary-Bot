from __future__ import annotations

import logging

from openai import AsyncOpenAI

from app.reasoning import call_with_reasoning_fallback, reasoning_request_kwargs

logger = logging.getLogger("telegram-summary-bot")


SYSTEM_PROMPT = """你是 Telegram 群組的摘要編輯，請用繁體中文輸出，語氣可輕鬆但內容要準確。
你會收到「摘要中繼資料」與「對話紀錄」。

請盡量遵循這個格式輸出（不要加前言或結語）：

<群組名稱> Summary | <摘要區間> | <訊息總數> 則訊息

1. <主題一>
- <使用者A>：<該主題的重要訊息摘要>
- <使用者B>：<該主題的重要訊息摘要>
[💬 回到討論](<該主題最相關訊息的討論連結>)

2. <主題二>
- ...
[💬 回到討論](<該主題最相關訊息的討論連結>)

格式與內容規則：
1) 主題數量 3 到 7 個，依討論熱度分配。
2) 每個主題至少 2 個條列，優先寫有資訊量、決策、觀點衝突、笑點（如果有笑點的話）。
3) 主題前面加一個符合內容的 emoji，主題內容使用粗體凸顯。
4) 條列盡量帶人名與重點。
5) 不要杜撰，不確定就保守描述。
6) 每個主題的最後一行必須放一個「💬 回到討論」Markdown 連結，連到最能代表該主題的原始訊息。
7) 連結只能逐字使用對話紀錄中標示為「討論連結」的網址，不可自行組合、改寫或杜撰；除此之外不要輸出其他網址。
8) 同一個連結可以被不同主題使用，但應優先選擇最貼近各主題討論點的訊息。
9) 若對話紀錄完全沒有提供「討論連結」，則省略所有「回到討論」行。
10) 人名可能附帶有用戶自己添加的資訊，可以縮短人名，不要直接 @ 對方。
11) 若訊息太少，章節可以減少，但仍維持同樣結構。
"""


class OpenAISummarizer:
    def __init__(self, api_key: str, max_output_tokens: int):
        self.client = AsyncOpenAI(api_key=api_key)
        self.max_output_tokens = max_output_tokens
        self._reasoning_fallbacks: set[tuple[str, str, str]] = set()

    async def summarize(
        self,
        *,
        transcript: str,
        model: str,
        api_style: str,
        reasoning_effort: str,
    ) -> str:
        style = self._resolve_api_style(model, api_style)

        if style == "responses":
            return await self._summarize_via_responses(
                transcript=transcript,
                model=model,
                max_output_tokens=self.max_output_tokens,
                reasoning_effort=reasoning_effort,
            )

        return await self._summarize_via_chat(
            transcript=transcript,
            model=model,
            max_tokens=self.max_output_tokens,
            reasoning_effort=reasoning_effort,
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
        reasoning_effort: str,
    ) -> str:
        request_kwargs = {
            "model": model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": SYSTEM_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": transcript}],
                },
            ],
            "max_output_tokens": max_output_tokens,
        }
        response = await self._create_with_reasoning_fallback(
            create=self.client.responses.create,
            request_kwargs=request_kwargs,
            api_style="responses",
            model=model,
            reasoning_effort=reasoning_effort,
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

    async def _summarize_via_chat(
        self,
        *,
        transcript: str,
        model: str,
        max_tokens: int,
        reasoning_effort: str,
    ) -> str:
        request_kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        response = await self._create_with_reasoning_fallback(
            create=self.client.chat.completions.create,
            request_kwargs=request_kwargs,
            api_style="chat",
            model=model,
            reasoning_effort=reasoning_effort,
            drop_when_reasoning=("max_tokens", "temperature"),
            add_when_reasoning={"max_completion_tokens": max_tokens},
        )
        return (response.choices[0].message.content or "").strip()

    async def _create_with_reasoning_fallback(
        self,
        *,
        create,
        request_kwargs: dict,
        api_style: str,
        model: str,
        reasoning_effort: str,
        drop_when_reasoning: tuple[str, ...] = (),
        add_when_reasoning: dict | None = None,
    ):
        reasoning_kwargs = reasoning_request_kwargs(api_style, reasoning_effort)
        fallback_key = (api_style, model, reasoning_effort)
        if not reasoning_kwargs or fallback_key in self._reasoning_fallbacks:
            return await create(**request_kwargs)

        response, fallback_error = await call_with_reasoning_fallback(
            create=create,
            request_kwargs=request_kwargs,
            reasoning_kwargs=reasoning_kwargs,
            drop_when_reasoning=drop_when_reasoning,
            add_when_reasoning=add_when_reasoning,
        )
        if fallback_error:
            self._reasoning_fallbacks.add(fallback_key)
            logger.warning(
                "Model rejected reasoning setting; retried with model default "
                "(model=%s api_style=%s reasoning_effort=%s error=%s)",
                model,
                api_style,
                reasoning_effort,
                fallback_error,
            )
        return response

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
