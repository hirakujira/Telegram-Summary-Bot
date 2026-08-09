from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.main import SummaryBot
from app.time_utils import to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890
SECOND_GROUP_ID = -1001234567891
SUBSCRIBER_ID = 101
STALE_SUBSCRIBER_ID = 102
UNKNOWN_SUBSCRIBER_ID = 103
SUMMARY_OUTPUT = """測試群組 Summary | 2026-07-29 to 2026-07-30 | 2 則訊息

1. 🧪 **部署討論**
- **Alice**：確認今天上線
"""


class RecordingSummarizer:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def summarize(self, *, transcript, model, reasoning_effort, response_style, topic=None):
        self.calls.append(
            {
                "transcript": transcript,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "response_style": response_style,
                "topic": topic,
            }
        )
        return SUMMARY_OUTPUT


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self.members: dict[tuple[int, int], object] = {}
        self.titles = {
            GROUP_ID: "測試群組",
            SECOND_GROUP_ID: "第二群組",
        }

    async def send_message(self, *, chat_id, text, parse_mode=None) -> None:
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, parse_mode=parse_mode))

    async def get_chat(self, chat_id):
        return SimpleNamespace(title=self.titles[chat_id], username=None)

    async def get_chat_member(self, chat_id, user_id):
        member = self.members.get((chat_id, user_id))
        if isinstance(member, Exception):
            raise member
        if member is None:
            return SimpleNamespace(status="left", is_member=False)
        return member


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[SimpleNamespace] = []

    async def reply_text(self, text, reply_markup=None) -> None:
        self.replies.append(SimpleNamespace(text=text, reply_markup=reply_markup))


class FakeCallbackQuery:
    def __init__(self, *, user_id: int, data: str) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(type=ChatType.PRIVATE))
        self.answered = False
        self.edits: list[str] = []

    async def answer(self) -> None:
        self.answered = True

    async def edit_message_text(self, text: str) -> None:
        self.edits.append(text)


def private_update(*, user_id: int) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        ),
        message,
    )


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            telegram_bot_token="test-token",
            openai_api_key="test-key",
            owner_telegram_user_id=OWNER_ID,
            sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
            min_messages_to_summary=1,
        )
        self.bot = SummaryBot(settings)
        self.telegram_bot = FakeTelegramBot()
        self.bot.application = SimpleNamespace(bot=self.telegram_bot)
        await self.bot.db.connect()
        await self.bot.db.authorize_chat(GROUP_ID)

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def _save_message(self) -> None:
        await self.bot.db.save_text_message(
            chat_id=GROUP_ID,
            message_id=1,
            user_id=1,
            user_name="Alice",
            text="今天要上線",
            created_at_utc=to_iso(utc_now() - timedelta(minutes=1)),
        )

    async def test_subscription_storage_is_idempotent_and_revocation_cleans_up(self) -> None:
        self.assertTrue(await self.bot.db.add_subscription(SUBSCRIBER_ID, GROUP_ID))
        self.assertFalse(await self.bot.db.add_subscription(SUBSCRIBER_ID, GROUP_ID))
        self.assertEqual(
            await self.bot.db.get_subscriber_ids(GROUP_ID),
            [SUBSCRIBER_ID],
        )

        await self.bot.db.revoke_chat_authorization(GROUP_ID)

        self.assertEqual(await self.bot.db.get_subscriber_ids(GROUP_ID), [])
        self.assertEqual(await self.bot.db.get_subscribed_chat_ids(SUBSCRIBER_ID), [])

    async def test_subscribe_only_lists_authorized_groups_where_user_is_active(self) -> None:
        await self.bot.db.authorize_chat(SECOND_GROUP_ID)
        self.telegram_bot.members[(GROUP_ID, SUBSCRIBER_ID)] = SimpleNamespace(
            status="member",
            is_member=True,
        )
        self.telegram_bot.members[(SECOND_GROUP_ID, SUBSCRIBER_ID)] = SimpleNamespace(
            status="left",
            is_member=False,
        )
        update, message = private_update(user_id=SUBSCRIBER_ID)

        await self.bot.subscribe(update, None)

        self.assertEqual(message.replies[0].text, "請選擇要訂閱排程摘要的群組：")
        buttons = message.replies[0].reply_markup.inline_keyboard
        self.assertEqual([[button.text for button in row] for row in buttons], [["測試群組"]])
        self.assertEqual(buttons[0][0].callback_data, f"subscribe:{GROUP_ID}")

    async def test_subscribe_callback_rechecks_membership_before_creating_subscription(self) -> None:
        query = FakeCallbackQuery(user_id=SUBSCRIBER_ID, data=f"subscribe:{GROUP_ID}")
        update = SimpleNamespace(callback_query=query)

        await self.bot.handle_subscription_callback(update, None)

        self.assertTrue(query.answered)
        self.assertIn("無法確認你仍在這個群組", query.edits[0])
        self.assertEqual(await self.bot.db.get_subscribed_chat_ids(SUBSCRIBER_ID), [])

        self.telegram_bot.members[(GROUP_ID, SUBSCRIBER_ID)] = SimpleNamespace(
            status="restricted",
            is_member=True,
        )
        query = FakeCallbackQuery(user_id=SUBSCRIBER_ID, data=f"subscribe:{GROUP_ID}")
        await self.bot.handle_subscription_callback(SimpleNamespace(callback_query=query), None)

        self.assertIn("已訂閱", query.edits[0])
        self.assertEqual(await self.bot.db.get_subscribed_chat_ids(SUBSCRIBER_ID), [GROUP_ID])

    async def test_unsubscribe_removes_existing_subscription_without_membership_check(self) -> None:
        await self.bot.db.add_subscription(SUBSCRIBER_ID, GROUP_ID)
        query = FakeCallbackQuery(user_id=SUBSCRIBER_ID, data=f"unsubscribe:{GROUP_ID}")

        await self.bot.handle_subscription_callback(SimpleNamespace(callback_query=query), None)

        self.assertIn("已取消", query.edits[0])
        self.assertEqual(await self.bot.db.get_subscribed_chat_ids(SUBSCRIBER_ID), [])

    async def test_auto_summary_reuses_rendered_html_without_extra_model_calls(self) -> None:
        await self._save_message()
        self.bot.summarizer = RecordingSummarizer()
        await self.bot.db.add_subscription(SUBSCRIBER_ID, GROUP_ID)
        await self.bot.db.add_subscription(STALE_SUBSCRIBER_ID, GROUP_ID)
        await self.bot.db.add_subscription(UNKNOWN_SUBSCRIBER_ID, GROUP_ID)
        self.telegram_bot.members[(GROUP_ID, SUBSCRIBER_ID)] = SimpleNamespace(
            status="member",
            is_member=True,
        )
        self.telegram_bot.members[(GROUP_ID, STALE_SUBSCRIBER_ID)] = SimpleNamespace(
            status="left",
            is_member=False,
        )
        self.telegram_bot.members[(GROUP_ID, UNKNOWN_SUBSCRIBER_ID)] = RuntimeError("API failed")

        posted = await self.bot.generate_and_post_summary(GROUP_ID, triggered_by="auto")

        self.assertTrue(posted)
        self.assertEqual(len(self.bot.summarizer.calls), 1)
        self.assertEqual([message.chat_id for message in self.telegram_bot.sent], [GROUP_ID, SUBSCRIBER_ID])
        self.assertEqual(self.telegram_bot.sent[0].text, self.telegram_bot.sent[1].text)
        self.assertEqual(self.telegram_bot.sent[1].parse_mode, "HTML")
        self.assertEqual(
            await self.bot.db.get_subscribed_chat_ids(STALE_SUBSCRIBER_ID),
            [],
        )
        self.assertEqual(
            await self.bot.db.get_subscribed_chat_ids(UNKNOWN_SUBSCRIBER_ID),
            [GROUP_ID],
        )

    async def test_manual_summary_does_not_notify_subscribers(self) -> None:
        await self._save_message()
        self.bot.summarizer = RecordingSummarizer()
        await self.bot.db.add_subscription(SUBSCRIBER_ID, GROUP_ID)
        self.telegram_bot.members[(GROUP_ID, SUBSCRIBER_ID)] = SimpleNamespace(
            status="member",
            is_member=True,
        )

        posted = await self.bot.generate_and_post_summary(GROUP_ID, triggered_by="manual")

        self.assertTrue(posted)
        self.assertEqual(len(self.bot.summarizer.calls), 1)
        self.assertEqual([message.chat_id for message in self.telegram_bot.sent], [GROUP_ID])


if __name__ == "__main__":
    unittest.main()
