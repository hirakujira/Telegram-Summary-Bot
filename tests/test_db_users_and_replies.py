from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


LEGACY_MESSAGES_SCHEMA = """
CREATE TABLE messages (
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  user_name TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  PRIMARY KEY(chat_id, message_id)
)
"""

CHAT_ID = -1001234567890


class MessageStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "bot.db"
        self.db = self._build_db()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    def _build_db(self) -> Database:
        return Database(
            path=str(self.path),
            default_timezone="UTC+8",
            default_cron_expr="0 9 * * *",
            default_model="test-model",
            default_reasoning_effort="default",
        )

    def _seed_legacy_messages(self, rows: list[tuple]) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(LEGACY_MESSAGES_SCHEMA)
            conn.executemany(
                """
                INSERT INTO messages(
                  chat_id, message_id, user_id, user_name, text, created_at_utc
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    async def _save(
        self,
        *,
        message_id: int,
        user_id: int,
        user_name: str,
        text: str,
        created_at_utc: str,
        reply_to_message_id: int | None = None,
    ) -> None:
        await self.db.save_text_message(
            chat_id=CHAT_ID,
            message_id=message_id,
            user_id=user_id,
            user_name=user_name,
            text=text,
            created_at_utc=created_at_utc,
            reply_to_message_id=reply_to_message_id,
        )

    async def _all_rows(self) -> list:
        _, rows = await self.db.get_messages_for_summary(
            chat_id=CHAT_ID,
            from_utc_iso=None,
            to_utc_iso="2099-01-01T00:00:00+00:00",
            limit=100,
        )
        return rows

    async def test_migration_keeps_every_message_and_the_latest_name(self) -> None:
        self._seed_legacy_messages(
            [
                (CHAT_ID, 1, 11, "舊名字", "第一句", "2026-07-01T00:00:00+00:00"),
                (CHAT_ID, 2, 11, "新名字", "第二句", "2026-07-02T00:00:00+00:00"),
                (CHAT_ID, 3, 22, "另一個人", "第三句", "2026-07-03T00:00:00+00:00"),
            ]
        )

        await self.db.connect()
        rows = await self._all_rows()

        self.assertEqual([row["message_id"] for row in rows], [1, 2, 3])
        self.assertEqual([row["text"] for row in rows], ["第一句", "第二句", "第三句"])
        # The old name is gone: every historical message shows the current one.
        self.assertEqual([row["user_name"] for row in rows], ["新名字", "新名字", "另一個人"])
        self.assertTrue(all(row["reply_to_message_id"] is None for row in rows))

    async def test_migration_is_idempotent_across_restarts(self) -> None:
        self._seed_legacy_messages(
            [(CHAT_ID, 1, 11, "阿明", "第一句", "2026-07-01T00:00:00+00:00")]
        )

        await self.db.connect()
        await self._save(
            message_id=2,
            user_id=11,
            user_name="阿明",
            text="第二句",
            created_at_utc="2026-07-02T00:00:00+00:00",
            reply_to_message_id=1,
        )
        await self.db.close()

        self.db = self._build_db()
        await self.db.connect()
        rows = await self._all_rows()

        self.assertEqual([row["message_id"] for row in rows], [1, 2])
        self.assertEqual(rows[1]["reply_to_message_id"], 1)

    async def test_rename_applies_to_previously_stored_messages(self) -> None:
        await self.db.connect()
        await self._save(
            message_id=1,
            user_id=11,
            user_name="阿明",
            text="第一句",
            created_at_utc="2026-07-01T00:00:00+00:00",
        )
        await self._save(
            message_id=2,
            user_id=11,
            user_name="阿明 (@ming)",
            text="第二句",
            created_at_utc="2026-07-02T00:00:00+00:00",
        )

        rows = await self._all_rows()

        self.assertEqual([row["user_name"] for row in rows], ["阿明 (@ming)", "阿明 (@ming)"])

    async def test_reply_target_is_stored_even_when_the_target_is_unknown(self) -> None:
        await self.db.connect()
        await self._save(
            message_id=5,
            user_id=11,
            user_name="阿明",
            text="回覆機器人加入群組前的訊息",
            created_at_utc="2026-07-01T00:00:00+00:00",
            reply_to_message_id=3,
        )
        await self._save(
            message_id=6,
            user_id=22,
            user_name="小美",
            text="沒有回覆任何人",
            created_at_utc="2026-07-01T00:01:00+00:00",
        )

        rows = await self._all_rows()

        self.assertEqual(rows[0]["reply_to_message_id"], 3)
        self.assertIsNone(rows[1]["reply_to_message_id"])

    async def test_duplicate_message_id_does_not_overwrite_stored_text(self) -> None:
        await self.db.connect()
        await self._save(
            message_id=1,
            user_id=11,
            user_name="阿明目前名稱",
            text="原始內容",
            created_at_utc="2026-07-01T00:00:00+00:00",
        )
        await self._save(
            message_id=1,
            user_id=11,
            user_name="阿明舊名稱",
            text="重複送達的內容",
            created_at_utc="2026-07-01T00:00:00+00:00",
        )

        rows = await self._all_rows()

        self.assertEqual([row["text"] for row in rows], ["原始內容"])
        self.assertEqual([row["user_name"] for row in rows], ["阿明目前名稱"])

    async def test_commit_flushes_deferred_message_write(self) -> None:
        await self.db.connect()
        inserted = await self.db.save_text_message(
            chat_id=CHAT_ID,
            message_id=1,
            user_id=11,
            user_name="阿明",
            text="延後提交",
            created_at_utc="2026-07-01T00:00:00+00:00",
            commit=False,
        )

        await self.db.commit()
        rows = await self._all_rows()

        self.assertTrue(inserted)
        self.assertEqual([row["text"] for row in rows], ["延後提交"])

    async def test_message_without_stored_user_falls_back_to_placeholder(self) -> None:
        await self.db.connect()
        await self._save(
            message_id=1,
            user_id=11,
            user_name="阿明",
            text="第一句",
            created_at_utc="2026-07-01T00:00:00+00:00",
        )
        assert self.db.conn is not None
        await self.db.conn.execute("DELETE FROM users")
        await self.db.conn.commit()

        rows = await self._all_rows()

        self.assertEqual(rows[0]["user_name"], "未知成員")


if __name__ == "__main__":
    unittest.main()
