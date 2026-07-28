from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class DatabaseResponseStyleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.db = Database(
            path=str(self.path),
            default_timezone="UTC+8",
            default_cron_expr="0 9 * * *",
            default_model="test-model",
            default_reasoning_effort="default",
        )

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_migrates_legacy_settings_and_persists_response_style(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE chat_settings (
                  chat_id INTEGER PRIMARY KEY,
                  timezone TEXT NOT NULL,
                  cron_expr TEXT NOT NULL,
                  auto_enabled INTEGER NOT NULL DEFAULT 1,
                  model TEXT NOT NULL,
                  api_style TEXT NOT NULL DEFAULT 'responses',
                  reasoning_effort TEXT NOT NULL DEFAULT 'default',
                  next_run_at_utc TEXT NOT NULL,
                  updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chat_settings(
                  chat_id, timezone, cron_expr, auto_enabled, model, api_style,
                  reasoning_effort, next_run_at_utc, updated_at_utc
                )
                VALUES(1, 'UTC+8', '0 9 * * *', 1, 'test-model', 'responses',
                       'default', '2026-07-29T01:00:00+00:00', '2026-07-29T00:00:00+00:00')
                """
            )

        await self.db.connect()

        settings = await self.db.get_chat_settings(1)
        self.assertEqual(settings.response_style, "normal")

        updated = await self.db.update_chat_settings(1, response_style="roast")
        self.assertEqual(updated.response_style, "roast")


if __name__ == "__main__":
    unittest.main()
