from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType
from telegram.ext import ConversationHandler, MessageHandler, filters

from app.config import Settings
from app.main import SummaryBot, build_application
from app.time_utils import to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def build_update(
    *, user_id: int = OWNER_ID, chat_type: ChatType = ChatType.SUPERGROUP, text: str = ""
) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    message.text = text
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=SimpleNamespace(id=GROUP_ID, type=chat_type),
    )
    return update, message


class ManagementCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context(*args: str) -> SimpleNamespace:
        return SimpleNamespace(args=list(args), chat_data={})

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = SummaryBot(
            Settings(
                telegram_bot_token="test-token",
                openai_api_key="test-key",
                owner_telegram_user_id=OWNER_ID,
                sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
                message_retention_days=90,
            )
        )
        await self.bot.db.connect()
        await self.bot.db.authorize_chat(GROUP_ID)

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def test_status_shows_group_and_global_settings(self) -> None:
        await self.bot.db.set_last_summarized_at(GROUP_ID, to_iso(utc_now()))
        update, message = build_update()

        await self.bot.status(update, None)

        self.assertIn("chat_id: -1001234567890", message.replies[0])
        self.assertIn("message_retention_days: 90", message.replies[0])
        self.assertIn("last_summarized_utc:", message.replies[0])

    async def test_non_owner_cannot_view_status(self) -> None:
        update, message = build_update(user_id=OWNER_ID + 1)

        await self.bot.status(update, None)

        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])

    async def test_owner_can_update_model_auto_reasoning_and_style(self) -> None:
        update, _ = build_update()

        await self.bot.set_model(update, self.context("test-model"))
        await self.bot.set_auto(update, self.context("off"))
        await self.bot.set_reasoning(update, self.context("high"))
        await self.bot.set_style(update, self.context("roast"))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(settings.model, "test-model")
        self.assertFalse(settings.auto_enabled)
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertEqual(settings.response_style, "roast")

    async def test_owner_can_update_schedule_and_timezone(self) -> None:
        update, _ = build_update()

        await self.bot.set_schedule(
            update,
            self.context("0", "9", "*", "*", "*"),
        )
        await self.bot.set_timezone(update, self.context("UTC+9"))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(settings.cron_expr, "0 9 * * *")
        self.assertEqual(settings.timezone, "UTC+9")
        self.assertGreater(settings.next_run_at_utc, to_iso(utc_now()))

    async def test_invalid_setting_values_leave_existing_settings_unchanged(self) -> None:
        update, message = build_update()
        before = await self.bot.db.get_chat_settings(GROUP_ID)

        await self.bot.set_auto(update, self.context("maybe"))
        await self.bot.set_reasoning(update, self.context("unknown"))
        await self.bot.set_style(update, self.context("formal"))
        await self.bot.set_schedule(update, self.context())

        after = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(after, before)
        self.assertEqual(len(message.replies), 4)
        self.assertTrue(all("用法：" in reply for reply in message.replies[:3]))
        self.assertEqual(message.replies[3], "請輸入 cron，例如 0 9 * * *。")

    async def test_non_owner_cannot_change_group_settings(self) -> None:
        update, message = build_update(user_id=OWNER_ID + 1)

        await self.bot.set_model(update, self.context("other-model"))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertNotEqual(settings.model, "other-model")
        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])

    async def test_empty_argument_starts_conversation_and_valid_reply_updates_setting(self) -> None:
        update, message = build_update()
        context = self.context()

        state = await self.bot.set_auto(update, context)

        self.assertEqual(state, self.bot.SETTING_VALUE)
        self.assertEqual(message.replies, ["請輸入自動摘要：on 或 off。"])
        reply, reply_message = build_update(text="off")
        state = await self.bot.receive_setting_value(reply, context)

        self.assertEqual(state, -1)
        self.assertEqual(reply_message.replies, ["自動摘要已設為 off"])
        self.assertFalse((await self.bot.db.get_chat_settings(GROUP_ID)).auto_enabled)

    async def test_invalid_conversation_reply_keeps_waiting_for_same_setting(self) -> None:
        update, _ = build_update()
        context = self.context()
        await self.bot.set_style(update, context)
        reply, message = build_update(text="formal")

        state = await self.bot.receive_setting_value(reply, context)

        self.assertEqual(state, self.bot.SETTING_VALUE)
        self.assertEqual(message.replies, ["用法：/set_style <normal|funny|roast>"])
        self.assertEqual(context.chat_data["pending_group_setting"], "style")

    async def test_cancel_clears_pending_setting(self) -> None:
        update, message = build_update()
        context = self.context()
        await self.bot.set_model(update, context)

        state = await self.bot.cancel_setting(update, context)

        self.assertEqual(state, -1)
        self.assertNotIn("pending_group_setting", context.chat_data)
        self.assertEqual(message.replies[-1], "已取消目前的設定。")

    async def test_cancel_without_pending_setting_reports_nothing_to_cancel(self) -> None:
        update, message = build_update()

        await self.bot.cancel_without_setting(update, self.context())

        self.assertEqual(message.replies, ["目前沒有進行中的設定。"])

    async def test_settings_cannot_start_outside_authorized_group(self) -> None:
        update, message = build_update(chat_type=ChatType.PRIVATE)

        state = await self.bot.set_model(update, self.context())

        self.assertEqual(state, -1)
        self.assertEqual(message.replies, ["此指令只能在已授權群組中使用。"])

    def test_conversation_handles_setting_text_before_capture_handler(self) -> None:
        application = build_application(self.bot.settings)
        handlers = application.handlers[0]
        conversation_index = next(
            index
            for index, handler in enumerate(handlers)
            if isinstance(handler, ConversationHandler)
        )
        capture_index = next(
            index
            for index, handler in enumerate(handlers)
            if isinstance(handler, MessageHandler)
            and handler.callback.__name__ == "capture_message"
        )
        conversation = handlers[conversation_index]

        self.assertLess(conversation_index, capture_index)
        value_handler = conversation.states[self.bot.SETTING_VALUE][0]
        self.assertIsInstance(value_handler, MessageHandler)
        self.assertTrue(value_handler.filters.check_update)
        self.assertEqual(conversation.per_chat, True)
        self.assertEqual(conversation.per_user, True)
        self.assertTrue(conversation.allow_reentry)
