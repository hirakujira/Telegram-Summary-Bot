from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.main import SummaryBot
from app.time_utils import to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def build_update(*, user_id: int = OWNER_ID) -> tuple[SimpleNamespace, FakeMessage]:
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        effective_chat=SimpleNamespace(id=GROUP_ID, type=ChatType.SUPERGROUP),
    )
    return update, message


class ManagementCommandTests(unittest.IsolatedAsyncioTestCase):
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

        await self.bot.set_model(update, SimpleNamespace(args=["test-model"]))
        await self.bot.set_auto(update, SimpleNamespace(args=["off"]))
        await self.bot.set_reasoning(update, SimpleNamespace(args=["high"]))
        await self.bot.set_style(update, SimpleNamespace(args=["roast"]))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(settings.model, "test-model")
        self.assertFalse(settings.auto_enabled)
        self.assertEqual(settings.reasoning_effort, "high")
        self.assertEqual(settings.response_style, "roast")

    async def test_owner_can_update_schedule_and_timezone(self) -> None:
        update, _ = build_update()

        await self.bot.set_schedule(
            update,
            SimpleNamespace(args=["0", "9", "*", "*", "*"]),
        )
        await self.bot.set_timezone(update, SimpleNamespace(args=["UTC+9"]))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(settings.cron_expr, "0 9 * * *")
        self.assertEqual(settings.timezone, "UTC+9")
        self.assertGreater(settings.next_run_at_utc, to_iso(utc_now()))

    async def test_invalid_setting_values_leave_existing_settings_unchanged(self) -> None:
        update, message = build_update()
        before = await self.bot.db.get_chat_settings(GROUP_ID)

        await self.bot.set_auto(update, SimpleNamespace(args=["maybe"]))
        await self.bot.set_reasoning(update, SimpleNamespace(args=["unknown"]))
        await self.bot.set_style(update, SimpleNamespace(args=["formal"]))
        await self.bot.set_schedule(update, SimpleNamespace(args=[]))

        after = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(after, before)
        self.assertEqual(len(message.replies), 4)
        self.assertTrue(all("用法：" in reply for reply in message.replies))

    async def test_non_owner_cannot_change_group_settings(self) -> None:
        update, message = build_update(user_id=OWNER_ID + 1)

        await self.bot.set_model(update, SimpleNamespace(args=["other-model"]))

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertNotEqual(settings.model, "other-model")
        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])
