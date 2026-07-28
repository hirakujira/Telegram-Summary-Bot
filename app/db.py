from __future__ import annotations

from dataclasses import dataclass

import aiosqlite

from app.time_utils import compute_next_run_utc, to_iso, utc_now


@dataclass(slots=True)
class ChatSettings:
    chat_id: int
    timezone: str
    cron_expr: str
    auto_enabled: bool
    model: str
    reasoning_effort: str
    response_style: str
    next_run_at_utc: str


class Database:
    def __init__(
        self,
        path: str,
        default_timezone: str,
        default_cron_expr: str,
        default_model: str,
        default_reasoning_effort: str,
    ):
        self.path = path
        self.default_timezone = default_timezone
        self.default_cron_expr = default_cron_expr
        self.default_model = default_model
        self.default_reasoning_effort = default_reasoning_effort
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode = WAL")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
              chat_id INTEGER PRIMARY KEY,
              timezone TEXT NOT NULL,
              cron_expr TEXT NOT NULL,
              auto_enabled INTEGER NOT NULL DEFAULT 1,
              model TEXT NOT NULL,
              api_style TEXT NOT NULL DEFAULT 'responses',
              reasoning_effort TEXT NOT NULL DEFAULT 'default',
              response_style TEXT NOT NULL DEFAULT 'normal',
              next_run_at_utc TEXT NOT NULL,
              updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summary_state (
              chat_id INTEGER PRIMARY KEY,
              last_summarized_at_utc TEXT,
              FOREIGN KEY(chat_id) REFERENCES chat_settings(chat_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
              chat_id INTEGER NOT NULL,
              message_id INTEGER NOT NULL,
              user_id INTEGER NOT NULL,
              user_name TEXT NOT NULL,
              text TEXT NOT NULL,
              created_at_utc TEXT NOT NULL,
              PRIMARY KEY(chat_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_time
            ON messages(chat_id, created_at_utc);
            """
        )
        columns_cursor = await self.conn.execute("PRAGMA table_info(chat_settings)")
        columns = {row[1] for row in await columns_cursor.fetchall()}
        if "reasoning_effort" not in columns:
            await self.conn.execute(
                "ALTER TABLE chat_settings "
                "ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT 'default'"
            )
        if "response_style" not in columns:
            await self.conn.execute(
                "ALTER TABLE chat_settings "
                "ADD COLUMN response_style TEXT NOT NULL DEFAULT 'normal'"
            )
        await self.conn.execute(
            "UPDATE chat_settings SET api_style = 'responses' WHERE api_style <> 'responses'"
        )
        await self.conn.execute(
            """
            UPDATE chat_settings
            SET response_style = 'normal'
            WHERE response_style NOT IN ('normal', 'funny', 'roast')
            """
        )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def ensure_chat(self, chat_id: int) -> ChatSettings:
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row:
            return self._row_to_settings(row)

        next_run = compute_next_run_utc(self.default_cron_expr, self.default_timezone)
        now = to_iso(utc_now())
        await self.conn.execute(
            """
            INSERT INTO chat_settings(
              chat_id, timezone, cron_expr, auto_enabled, model, api_style,
              reasoning_effort, response_style, next_run_at_utc, updated_at_utc
            )
            VALUES(?, ?, ?, 1, ?, 'responses', ?, 'normal', ?, ?)
            """,
            (
                chat_id,
                self.default_timezone,
                self.default_cron_expr,
                self.default_model,
                self.default_reasoning_effort,
                to_iso(next_run),
                now,
            ),
        )
        await self.conn.execute(
            "INSERT OR IGNORE INTO summary_state(chat_id, last_summarized_at_utc) VALUES(?, NULL)",
            (chat_id,),
        )
        await self.conn.commit()

        return ChatSettings(
            chat_id=chat_id,
            timezone=self.default_timezone,
            cron_expr=self.default_cron_expr,
            auto_enabled=True,
            model=self.default_model,
            reasoning_effort=self.default_reasoning_effort,
            response_style="normal",
            next_run_at_utc=to_iso(next_run),
        )

    async def get_chat_settings(self, chat_id: int) -> ChatSettings:
        await self.ensure_chat(chat_id)
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT * FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return self._row_to_settings(row)

    async def update_chat_settings(
        self,
        chat_id: int,
        *,
        timezone: str | None = None,
        cron_expr: str | None = None,
        auto_enabled: bool | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        response_style: str | None = None,
        recompute_next_run: bool = False,
    ) -> ChatSettings:
        settings = await self.get_chat_settings(chat_id)

        new_timezone = timezone if timezone is not None else settings.timezone
        new_cron_expr = cron_expr if cron_expr is not None else settings.cron_expr
        new_auto_enabled = settings.auto_enabled if auto_enabled is None else auto_enabled
        new_model = model if model is not None else settings.model
        new_reasoning_effort = (
            reasoning_effort if reasoning_effort is not None else settings.reasoning_effort
        )
        new_response_style = (
            response_style if response_style is not None else settings.response_style
        )
        new_next_run = settings.next_run_at_utc

        if recompute_next_run:
            new_next_run = to_iso(compute_next_run_utc(new_cron_expr, new_timezone))

        assert self.conn is not None
        await self.conn.execute(
            """
            UPDATE chat_settings
            SET timezone = ?, cron_expr = ?, auto_enabled = ?, model = ?,
                reasoning_effort = ?, response_style = ?, next_run_at_utc = ?, updated_at_utc = ?
            WHERE chat_id = ?
            """,
            (
                new_timezone,
                new_cron_expr,
                1 if new_auto_enabled else 0,
                new_model,
                new_reasoning_effort,
                new_response_style,
                new_next_run,
                to_iso(utc_now()),
                chat_id,
            ),
        )
        await self.conn.commit()

        return await self.get_chat_settings(chat_id)

    async def get_last_summarized_at(self, chat_id: int) -> str | None:
        await self.ensure_chat(chat_id)
        assert self.conn is not None
        cursor = await self.conn.execute(
            "SELECT last_summarized_at_utc FROM summary_state WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return row["last_summarized_at_utc"]

    async def set_last_summarized_at(self, chat_id: int, timestamp_utc_iso: str) -> None:
        await self.ensure_chat(chat_id)
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT INTO summary_state(chat_id, last_summarized_at_utc)
            VALUES(?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_summarized_at_utc = excluded.last_summarized_at_utc
            """,
            (chat_id, timestamp_utc_iso),
        )
        await self.conn.commit()

    async def save_text_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        user_name: str,
        text: str,
        created_at_utc: str,
    ) -> None:
        assert self.conn is not None
        await self.conn.execute(
            """
            INSERT OR IGNORE INTO messages(chat_id, message_id, user_id, user_name, text, created_at_utc)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (chat_id, message_id, user_id, user_name, text, created_at_utc),
        )
        await self.conn.commit()

    async def get_messages_for_summary(
        self,
        *,
        chat_id: int,
        from_utc_iso: str | None,
        to_utc_iso: str,
        limit: int,
    ) -> tuple[int, list[aiosqlite.Row]]:
        assert self.conn is not None

        if from_utc_iso:
            count_cursor = await self.conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM messages
                WHERE chat_id = ?
                  AND created_at_utc > ?
                  AND created_at_utc <= ?
                """,
                (chat_id, from_utc_iso, to_utc_iso),
            )
            data_cursor = await self.conn.execute(
                """
                SELECT *
                FROM messages
                WHERE chat_id = ?
                  AND created_at_utc > ?
                  AND created_at_utc <= ?
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (chat_id, from_utc_iso, to_utc_iso, limit),
            )
        else:
            count_cursor = await self.conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM messages
                WHERE chat_id = ?
                  AND created_at_utc <= ?
                """,
                (chat_id, to_utc_iso),
            )
            data_cursor = await self.conn.execute(
                """
                SELECT *
                FROM messages
                WHERE chat_id = ?
                  AND created_at_utc <= ?
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                (chat_id, to_utc_iso, limit),
            )

        count_row = await count_cursor.fetchone()
        rows_desc = await data_cursor.fetchall()
        rows_asc = list(reversed(rows_desc))
        return count_row["cnt"], rows_asc

    async def get_due_chats(self, now_utc_iso: str) -> list[ChatSettings]:
        assert self.conn is not None
        cursor = await self.conn.execute(
            """
            SELECT *
            FROM chat_settings
            WHERE auto_enabled = 1 AND next_run_at_utc <= ?
            ORDER BY next_run_at_utc ASC
            """,
            (now_utc_iso,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_settings(r) for r in rows]

    async def set_next_run(self, chat_id: int, next_run_utc_iso: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "UPDATE chat_settings SET next_run_at_utc = ?, updated_at_utc = ? WHERE chat_id = ?",
            (next_run_utc_iso, to_iso(utc_now()), chat_id),
        )
        await self.conn.commit()

    async def purge_old_messages(self, older_than_utc_iso: str) -> None:
        assert self.conn is not None
        await self.conn.execute(
            "DELETE FROM messages WHERE created_at_utc < ?",
            (older_than_utc_iso,),
        )
        await self.conn.commit()

    @staticmethod
    def _row_to_settings(row: aiosqlite.Row) -> ChatSettings:
        return ChatSettings(
            chat_id=row["chat_id"],
            timezone=row["timezone"],
            cron_expr=row["cron_expr"],
            auto_enabled=bool(row["auto_enabled"]),
            model=row["model"],
            reasoning_effort=row["reasoning_effort"],
            response_style=row["response_style"],
            next_run_at_utc=row["next_run_at_utc"],
        )
