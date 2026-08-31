from __future__ import annotations

import unittest
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.main import SummaryBot


OWNER_ID = 777


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def build_update(*, user_id: int) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        ),
        message,
    )


class StartHelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = SummaryBot(
            Settings(
                telegram_bot_token="test-token",
                openai_api_key="test-key",
                owner_telegram_user_id=OWNER_ID,
            )
        )

    async def test_owner_sees_management_commands(self) -> None:
        update, message = build_update(user_id=OWNER_ID)

        await self.bot.start(update, None)

        self.assertIn("僅擁有者：", message.replies[0])
        self.assertIn("一般用戶（請私訊機器人）：", message.replies[0])
        self.assertIn("/summary - 立即產生摘要", message.replies[0])
        self.assertIn("/set_schedule <cron>", message.replies[0])
        self.assertIn("/subscribe", message.replies[0])
        self.assertNotIn("(僅擁有者)", message.replies[0])
        self.assertNotIn("/authorize_group", message.replies[0])

    async def test_non_owner_sees_private_summary_and_subscription_commands(self) -> None:
        update, message = build_update(user_id=OWNER_ID + 1)

        await self.bot.start(update, None)

        self.assertIn("/subscribe", message.replies[0])
        self.assertIn("/unsubscribe", message.replies[0])
        self.assertNotIn("僅擁有者：", message.replies[0])
        self.assertIn("/summary <條件>", message.replies[0])
        self.assertNotIn("/set_schedule", message.replies[0])
        self.assertNotIn("/status", message.replies[0])


if __name__ == "__main__":
    unittest.main()
