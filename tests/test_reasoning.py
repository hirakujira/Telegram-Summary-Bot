from __future__ import annotations

import unittest

from app.config import Settings
from app.reasoning import (
    call_with_reasoning_fallback,
    is_reasoning_unsupported_error,
    normalize_reasoning_effort,
    reasoning_request_kwargs,
)


class FakeAPIError(Exception):
    def __init__(self, message: str, *, param: str | None = None):
        super().__init__(message)
        self.param = param
        self.body = None


class ReasoningTests(unittest.TestCase):
    def test_normalizes_reasoning_effort(self) -> None:
        self.assertEqual(normalize_reasoning_effort(" HIGH "), "high")
        self.assertEqual(normalize_reasoning_effort("max"), "max")

    def test_default_model_is_gpt_5_6_luna(self) -> None:
        settings = Settings("telegram-token", "openai-key", 123)
        self.assertEqual(settings.default_model, "gpt-5.6-luna")

    def test_rejects_unknown_reasoning_effort(self) -> None:
        with self.assertRaises(ValueError):
            normalize_reasoning_effort("extreme")

    def test_default_omits_reasoning_parameter(self) -> None:
        self.assertEqual(reasoning_request_kwargs("responses", "default"), {})
        self.assertEqual(reasoning_request_kwargs("chat", "default"), {})

    def test_builds_api_specific_reasoning_parameters(self) -> None:
        self.assertEqual(
            reasoning_request_kwargs("responses", "low"),
            {"reasoning": {"effort": "low"}},
        )
        self.assertEqual(
            reasoning_request_kwargs("chat", "low"),
            {"reasoning_effort": "low"},
        )

    def test_detects_reasoning_parameter_error(self) -> None:
        exc = FakeAPIError("Unsupported parameter", param="reasoning_effort")
        self.assertTrue(is_reasoning_unsupported_error(exc))

    def test_does_not_treat_unrelated_error_as_reasoning_fallback(self) -> None:
        exc = FakeAPIError("Invalid max_output_tokens", param="max_output_tokens")
        self.assertFalse(is_reasoning_unsupported_error(exc))


class ReasoningFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_without_reasoning_when_model_rejects_it(self) -> None:
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeAPIError("Unsupported parameter", param="reasoning")
            return "fallback response"

        response, fallback_error = await call_with_reasoning_fallback(
            create=create,
            request_kwargs={"model": "gpt-4o-mini", "input": "hello"},
            reasoning_kwargs={"reasoning": {"effort": "low"}},
        )

        self.assertEqual(response, "fallback response")
        self.assertIsNotNone(fallback_error)
        self.assertEqual(calls[0]["reasoning"], {"effort": "low"})
        self.assertNotIn("reasoning", calls[1])

    async def test_does_not_retry_unrelated_api_error(self) -> None:
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            raise FakeAPIError("Invalid input", param="input")

        with self.assertRaises(FakeAPIError):
            await call_with_reasoning_fallback(
                create=create,
                request_kwargs={"model": "gpt-5.6-luna", "input": "hello"},
                reasoning_kwargs={"reasoning": {"effort": "low"}},
            )

        self.assertEqual(len(calls), 1)

    async def test_chat_reasoning_uses_completion_tokens_then_restores_fallback(self) -> None:
        calls = []

        async def create(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise FakeAPIError("Unsupported value", param="reasoning_effort")
            return "fallback response"

        await call_with_reasoning_fallback(
            create=create,
            request_kwargs={
                "model": "example-model",
                "max_tokens": 1800,
                "temperature": 0.7,
            },
            reasoning_kwargs={"reasoning_effort": "high"},
            drop_when_reasoning=("max_tokens", "temperature"),
            add_when_reasoning={"max_completion_tokens": 1800},
        )

        self.assertNotIn("max_tokens", calls[0])
        self.assertNotIn("temperature", calls[0])
        self.assertEqual(calls[0]["max_completion_tokens"], 1800)
        self.assertEqual(calls[1]["max_tokens"], 1800)
        self.assertEqual(calls[1]["temperature"], 0.7)


if __name__ == "__main__":
    unittest.main()
