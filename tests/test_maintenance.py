from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.config import Settings
from app.main import SummaryBot
from app.time_utils import to_iso, utc_now


GROUP_ID = -1001234567890


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = SummaryBot(
            Settings(
                telegram_bot_token="test-token",
                openai_api_key="test-key",
                owner_telegram_user_id=777,
                sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
                message_retention_days=7,
            )
        )
        await self.bot.db.connect()
        await self.bot.db.authorize_chat(GROUP_ID)

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def _save_message(self, *, message_id: int, age: timedelta) -> None:
        await self.bot.db.save_text_message(
            chat_id=GROUP_ID,
            message_id=message_id,
            user_id=1,
            user_name="Alice",
            text=f"訊息 {message_id}",
            created_at_utc=to_iso(utc_now() - age),
        )

    async def test_cleanup_uses_configured_message_retention_days(self) -> None:
        await self._save_message(message_id=1, age=timedelta(days=8))
        await self._save_message(message_id=2, age=timedelta(days=6))

        await self.bot.cleanup_tick(None)

        _, rows = await self.bot.db.get_messages_for_summary(
            chat_id=GROUP_ID,
            from_utc_iso=None,
            to_utc_iso="2099-01-01T00:00:00+00:00",
            limit=10,
        )
        self.assertEqual([row["message_id"] for row in rows], [2])

    async def test_scheduler_runs_due_authorized_chat_and_reschedules_it(self) -> None:
        await self.bot.db.set_next_run(
            GROUP_ID,
            to_iso(utc_now() - timedelta(minutes=1)),
        )
        calls: list[tuple[int, str]] = []

        async def record_summary(chat_id: int, triggered_by: str) -> bool:
            calls.append((chat_id, triggered_by))
            return True

        self.bot.generate_and_post_summary = record_summary

        await self.bot.scheduler_tick(None)

        settings = await self.bot.db.get_chat_settings(GROUP_ID)
        self.assertEqual(calls, [(GROUP_ID, "auto")])
        self.assertGreater(settings.next_run_at_utc, to_iso(utc_now()))
