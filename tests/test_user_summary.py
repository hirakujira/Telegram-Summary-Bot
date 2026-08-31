from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.bot.summaries import UserSummaryRequest
from app.main import SummaryBot
from app.summary_query import ParsedQuery
from app.time_utils import parse_timezone, to_iso, utc_now


OWNER_ID = 777
USER_ID = 101
GROUP_ID = -1001234567890


class FakeParser:
    async def parse(self, *, text, model, timezone_text, now_utc):
        now_local = now_utc.astimezone(parse_timezone(timezone_text))
        return ParsedQuery(
            has_time_range=True,
            start_local=(now_local - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            end_local=now_local.strftime("%Y-%m-%d %H:%M"),
            topic="露營",
        )


class FakeSummarizer:
    async def summarize(self, **_):
        return "測試群組 Summary | 2026-01-01 to 2026-01-01 | 1 則訊息\n\n1. ⛺ **露營**\n- **Alice**：出遊"


class FakeTelegramBot:
    def __init__(self):
        self.members = {(GROUP_ID, USER_ID): SimpleNamespace(status="member", is_member=True)}
        self.sent = []

    async def get_chat_member(self, chat_id, user_id):
        member = self.members[(chat_id, user_id)]
        if isinstance(member, Exception):
            raise member
        return member

    async def get_chat(self, _):
        return SimpleNamespace(title="測試群組", username=None)

    async def send_message(self, **kwargs):
        self.sent.append(SimpleNamespace(**kwargs))


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(SimpleNamespace(text=text, reply_markup=reply_markup))


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = SimpleNamespace(chat=SimpleNamespace(type=ChatType.PRIVATE))
        self.edits = []

    async def answer(self):
        pass

    async def edit_message_text(self, text):
        self.edits.append(text)


class UserSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = SummaryBot(
            Settings(
                telegram_bot_token="token",
                openai_api_key="key",
                owner_telegram_user_id=OWNER_ID,
                sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
                daily_user_summary_limit=1,
            )
        )
        self.telegram = FakeTelegramBot()
        self.bot.application = SimpleNamespace(bot=self.telegram)
        self.bot.query_parser = FakeParser()
        self.bot.summarizer = FakeSummarizer()
        await self.bot.db.connect()
        await self.bot.db.authorize_chat(GROUP_ID)
        await self.bot.db.save_text_message(
            chat_id=GROUP_ID, message_id=1, user_id=1, user_name="Alice",
            text="週末去露營", created_at_utc=to_iso(utc_now() - timedelta(hours=1)),
        )

    async def asyncTearDown(self):
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def test_private_request_sends_only_to_requester_and_audits_success(self):
        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID), effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        )
        await self.bot.user_summary(update, SimpleNamespace(args=["最近一天", "露營"]))
        button = message.replies[0].reply_markup.inline_keyboard[0][0]

        callback = FakeCallback(button.callback_data)
        await self.bot.handle_user_summary_callback(SimpleNamespace(callback_query=callback), None)

        self.assertEqual([item.chat_id for item in self.telegram.sent], [USER_ID])
        self.assertEqual((await self.bot.db.get_last_summarized_at(GROUP_ID)), None)
        history = await self.bot.db.get_user_summary_history(excluded_user_id=OWNER_ID)
        self.assertEqual([(item.prompt, item.status) for item in history], [("最近一天 露營", "succeeded")])

    async def test_quota_reservation_blocks_second_request(self):
        now = utc_now()
        first = await self.bot.db.reserve_user_summary(
            user_id=USER_ID, chat_id=GROUP_ID, prompt="first", day_key="2026-01-01",
            created_at_utc=to_iso(now), limit=1,
        )
        second = await self.bot.db.reserve_user_summary(
            user_id=USER_ID, chat_id=GROUP_ID, prompt="second", day_key="2026-01-01",
            created_at_utc=to_iso(now), limit=1,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    async def test_concurrent_quota_reservations_do_not_exceed_limit(self):
        now = utc_now()

        results = await asyncio.gather(
            *[
                self.bot.db.reserve_user_summary(
                    user_id=USER_ID,
                    chat_id=GROUP_ID,
                    prompt=f"request {index}",
                    day_key="2026-08-31",
                    created_at_utc=to_iso(now),
                    limit=1,
                )
                for index in range(2)
            ]
        )

        self.assertEqual(sum(request_id is not None for request_id in results), 1)

    async def test_callback_rejects_another_users_request(self):
        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID), effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        )
        await self.bot.user_summary(update, SimpleNamespace(args=["最近一天"]))
        callback = FakeCallback(message.replies[0].reply_markup.inline_keyboard[0][0].callback_data)
        callback.from_user = SimpleNamespace(id=USER_ID + 1)

        await self.bot.handle_user_summary_callback(SimpleNamespace(callback_query=callback), None)

        self.assertIn("失效", callback.edits[0])
        self.assertEqual(self.telegram.sent, [])

    async def test_disabled_user_summary_does_not_offer_groups(self):
        self.bot.settings.daily_user_summary_limit = 0
        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=USER_ID),
            effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        )

        await self.bot.user_summary(update, SimpleNamespace(args=["最近一天"]))

        self.assertEqual(message.replies[0].text, "一般使用者私訊摘要目前未啟用。")

    async def test_failed_request_does_not_consume_quota(self):
        now = utc_now()
        await self.bot.db.record_failed_user_summary(
            user_id=USER_ID,
            chat_id=GROUP_ID,
            prompt="failed",
            day_key="2026-08-31",
            created_at_utc=to_iso(now),
        )

        request_id = await self.bot.db.reserve_user_summary(
            user_id=USER_ID,
            chat_id=GROUP_ID,
            prompt="retry",
            day_key="2026-08-31",
            created_at_utc=to_iso(now),
            limit=1,
        )

        self.assertIsNotNone(request_id)

    async def test_owner_can_view_non_owner_request_history(self):
        await self.bot.db.record_failed_user_summary(
            user_id=USER_ID,
            chat_id=GROUP_ID,
            prompt="昨天的露營討論",
            day_key="2026-08-31",
            created_at_utc=to_iso(utc_now()),
        )
        message = FakeMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=OWNER_ID),
            effective_message=message,
            effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
        )

        await self.bot.user_summary_history(update, None)

        self.assertIn(f"使用者：{USER_ID}", message.replies[0].text)
        self.assertIn("群組：測試群組", message.replies[0].text)
        self.assertIn("條件：昨天的露營討論", message.replies[0].text)
        self.assertIn("狀態：failed", message.replies[0].text)

    async def test_cleanup_removes_expired_summary_request(self):
        self.bot.user_summary_requests[USER_ID] = UserSummaryRequest(
            token="expired",
            user_id=USER_ID,
            prompt="最近一天",
            expires_at=utc_now() - timedelta(seconds=1),
        )

        await self.bot.cleanup_tick(None)

        self.assertNotIn(USER_ID, self.bot.user_summary_requests)


if __name__ == "__main__":
    unittest.main()
