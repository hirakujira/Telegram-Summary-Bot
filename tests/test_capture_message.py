from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.main import SummaryBot
from app.time_utils import utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890


def build_message(
    *,
    message_id: int,
    text: str,
    age: timedelta = timedelta(minutes=1),
    reply_to=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        caption=None,
        date=utc_now() - age,
        reply_to_message=reply_to,
        sticker=None,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        video_note=None,
        animation=None,
    )


def build_update(*, message, user_id: int, user_name: str, username: str | None = None):
    return SimpleNamespace(
        effective_message=message,
        effective_chat=SimpleNamespace(id=GROUP_ID, type=ChatType.SUPERGROUP),
        effective_user=SimpleNamespace(
            id=user_id,
            is_bot=False,
            full_name=user_name,
            username=username,
        ),
    )


class CaptureMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            telegram_bot_token="test-token",
            openai_api_key="test-key",
            owner_telegram_user_id=OWNER_ID,
            sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
        )
        self.bot = SummaryBot(settings)
        await self.bot.db.connect()
        await self.bot.db.authorize_chat(GROUP_ID)

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def _stored_rows(self) -> list:
        _, rows = await self.bot.db.get_messages_for_summary(
            chat_id=GROUP_ID,
            from_utc_iso=None,
            to_utc_iso="2099-01-01T00:00:00+00:00",
            limit=100,
        )
        return rows

    async def test_stores_reply_target_from_the_same_chat(self) -> None:
        root = build_message(message_id=1, text="露營要不要去", age=timedelta(minutes=5))
        await self.bot.capture_message(
            build_update(message=root, user_id=11, user_name="阿明"), None
        )

        reply = build_message(
            message_id=2,
            text="我要去",
            reply_to=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=GROUP_ID)),
        )
        await self.bot.capture_message(
            build_update(message=reply, user_id=22, user_name="小美"), None
        )

        rows = await self._stored_rows()

        self.assertEqual([row["reply_to_message_id"] for row in rows], [None, 1])
        self.assertEqual([row["user_name"] for row in rows], ["阿明", "小美"])

    async def test_ignores_reply_target_from_another_chat(self) -> None:
        quoted = build_message(
            message_id=3,
            text="引用其他群組的訊息",
            reply_to=SimpleNamespace(message_id=999, chat=SimpleNamespace(id=-100999)),
        )
        await self.bot.capture_message(
            build_update(message=quoted, user_id=11, user_name="阿明"), None
        )

        rows = await self._stored_rows()

        self.assertIsNone(rows[0]["reply_to_message_id"])

    async def test_renaming_a_member_rewrites_older_messages(self) -> None:
        first = build_message(message_id=1, text="第一句", age=timedelta(minutes=5))
        await self.bot.capture_message(
            build_update(message=first, user_id=11, user_name="阿明"), None
        )

        second = build_message(message_id=2, text="第二句")
        await self.bot.capture_message(
            build_update(message=second, user_id=11, user_name="阿明大大", username="ming"),
            None,
        )

        rows = await self._stored_rows()

        self.assertEqual(
            [row["user_name"] for row in rows],
            ["阿明大大 (@ming)", "阿明大大 (@ming)"],
        )


if __name__ == "__main__":
    unittest.main()
