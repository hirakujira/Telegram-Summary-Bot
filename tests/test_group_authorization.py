import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram.constants import ChatType

from app.config import Settings
from app.db import Database
from app.main import SummaryBot
from app.time_utils import to_iso, utc_now


OWNER_ID = 777
GROUP_ID = -1001234567890


class FakeTelegramBot:
    def __init__(self) -> None:
        self.sent: list[SimpleNamespace] = []
        self.left_chats: list[int] = []
        self.command_menus: list[tuple[int, int, list[str]]] = []

    async def send_message(self, *, chat_id, text, parse_mode=None) -> None:
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, parse_mode=parse_mode))

    async def leave_chat(self, chat_id) -> None:
        self.left_chats.append(chat_id)

    async def set_my_commands(self, commands, *, scope) -> bool:
        self.command_menus.append(
            (scope.chat_id, scope.user_id, [command.command for command in commands])
        )
        return True


def build_command_update(*, user_id: int, chat_type: ChatType) -> tuple[SimpleNamespace, SimpleNamespace]:
    message = SimpleNamespace(replies=[])

    async def reply_text(text: str) -> None:
        message.replies.append(text)

    message.reply_text = reply_text
    return (
        SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_message=message,
            effective_chat=SimpleNamespace(id=GROUP_ID, type=chat_type),
        ),
        message,
    )


def build_membership_update(*, actor_id: int, old_status: str, new_status: str):
    return SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(
                id=GROUP_ID,
                type=ChatType.SUPERGROUP,
                title="測試群組",
            ),
            from_user=SimpleNamespace(id=actor_id),
            old_chat_member=SimpleNamespace(status=old_status),
            new_chat_member=SimpleNamespace(status=new_status),
        )
    )


def build_owner_membership_update(
    *,
    old_status: str,
    new_status: str,
    is_member: bool | None = None,
):
    return SimpleNamespace(
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(
                id=GROUP_ID,
                type=ChatType.SUPERGROUP,
                title="測試群組",
            ),
            old_chat_member=SimpleNamespace(
                status=old_status,
                user=SimpleNamespace(id=OWNER_ID),
            ),
            new_chat_member=SimpleNamespace(
                status=new_status,
                user=SimpleNamespace(id=OWNER_ID),
                is_member=is_member,
            ),
        )
    )


class GroupAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        settings = Settings(
            telegram_bot_token="test-token",
            openai_api_key="test-key",
            owner_telegram_user_id=OWNER_ID,
            sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
        )
        self.bot = SummaryBot(settings)
        self.telegram_bot = FakeTelegramBot()
        self.bot.application = SimpleNamespace(bot=self.telegram_bot)
        await self.bot.db.connect()

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def test_owner_adding_bot_authorizes_group(self) -> None:
        await self.bot.handle_my_chat_member(
            build_membership_update(
                actor_id=OWNER_ID,
                old_status="left",
                new_status="member",
            ),
            None,
        )

        self.assertTrue(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(self.telegram_bot.left_chats, [])

    async def test_non_owner_adding_bot_notifies_owner_and_leaves(self) -> None:
        await self.bot.handle_my_chat_member(
            build_membership_update(
                actor_id=OWNER_ID + 1,
                old_status="left",
                new_status="member",
            ),
            None,
        )

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(self.telegram_bot.left_chats, [GROUP_ID])
        self.assertEqual([message.chat_id for message in self.telegram_bot.sent], [OWNER_ID])
        self.assertIn("非 owner", self.telegram_bot.sent[0].text)

    async def test_removing_bot_revokes_existing_authorization(self) -> None:
        await self.bot.db.authorize_chat(GROUP_ID)

        await self.bot.handle_my_chat_member(
            build_membership_update(
                actor_id=OWNER_ID,
                old_status="member",
                new_status="left",
            ),
            None,
        )

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))

    async def test_owner_leaving_revokes_authorization_and_makes_bot_leave(self) -> None:
        await self.bot.db.authorize_chat(GROUP_ID)
        await self.bot.db.add_subscription(user_id=101, chat_id=GROUP_ID)

        await self.bot.handle_owner_chat_member(
            build_owner_membership_update(old_status="member", new_status="left"),
            None,
        )

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(await self.bot.db.get_subscriber_ids(GROUP_ID), [])
        self.assertEqual(self.telegram_bot.left_chats, [GROUP_ID])
        self.assertEqual([message.chat_id for message in self.telegram_bot.sent], [OWNER_ID])
        self.assertIn("owner 已離開群組", self.telegram_bot.sent[0].text)

    async def test_restricted_owner_still_in_group_keeps_authorization(self) -> None:
        await self.bot.db.authorize_chat(GROUP_ID)

        await self.bot.handle_owner_chat_member(
            build_owner_membership_update(
                old_status="member",
                new_status="restricted",
                is_member=True,
            ),
            None,
        )

        self.assertTrue(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(self.telegram_bot.left_chats, [])

    async def test_owner_can_authorize_existing_group(self) -> None:
        update, message = build_command_update(
            user_id=OWNER_ID,
            chat_type=ChatType.SUPERGROUP,
        )

        await self.bot.authorize_group(update, None)

        self.assertTrue(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(message.replies, ["已授權此群組，並同步擁有者指令選單。"])
        self.assertEqual(self.telegram_bot.command_menus[0][0:2], (GROUP_ID, OWNER_ID))
        self.assertIn("authorize_group", self.telegram_bot.command_menus[0][2])

    async def test_non_owner_cannot_authorize_group(self) -> None:
        update, message = build_command_update(
            user_id=OWNER_ID + 1,
            chat_type=ChatType.SUPERGROUP,
        )

        await self.bot.authorize_group(update, None)

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(message.replies, ["你沒有權限執行這個指令。"])
        self.assertEqual(self.telegram_bot.command_menus, [])

    async def test_authorize_group_rejects_private_chat(self) -> None:
        update, message = build_command_update(
            user_id=OWNER_ID,
            chat_type=ChatType.PRIVATE,
        )

        await self.bot.authorize_group(update, None)

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(message.replies, ["此指令必須在群組中執行才能授權。"])
        self.assertEqual(self.telegram_bot.command_menus, [])

    async def test_unauthorized_group_activity_leaves_without_storing_message(self) -> None:
        message = SimpleNamespace(
            message_id=1,
            text="不應被保存",
            caption=None,
            date=utc_now(),
            sticker=None,
            photo=None,
            video=None,
            voice=None,
            audio=None,
            video_note=None,
            animation=None,
        )
        update = SimpleNamespace(
            effective_message=message,
            effective_chat=SimpleNamespace(
                id=GROUP_ID,
                type=ChatType.SUPERGROUP,
                title="測試群組",
            ),
            effective_user=SimpleNamespace(
                id=1,
                is_bot=False,
                full_name="Alice",
                username=None,
            ),
        )

        await self.bot.capture_message(update, None)

        _, rows = await self.bot.db.get_messages_for_summary(
            chat_id=GROUP_ID,
            from_utc_iso=None,
            to_utc_iso=to_iso(utc_now()),
            limit=10,
        )
        self.assertEqual(rows, [])
        self.assertEqual(self.telegram_bot.left_chats, [GROUP_ID])


class AuthorizationDatabaseTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_migration_marks_existing_chats_unauthorized_and_excludes_schedule(self) -> None:
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
                  response_style TEXT NOT NULL DEFAULT 'normal',
                  next_run_at_utc TEXT NOT NULL,
                  updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO chat_settings(
                  chat_id, timezone, cron_expr, auto_enabled, model, api_style,
                  reasoning_effort, response_style, next_run_at_utc, updated_at_utc
                )
                VALUES(?, 'UTC+8', '0 9 * * *', 1, 'test-model', 'responses',
                       'default', 'normal', '2020-01-01T00:00:00+00:00',
                       '2020-01-01T00:00:00+00:00')
                """,
                (GROUP_ID,),
            )

        await self.db.connect()

        self.assertFalse(await self.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(await self.db.get_due_chats(to_iso(utc_now())), [])

        await self.db.authorize_chat(GROUP_ID)
        due_chats = await self.db.get_due_chats(to_iso(utc_now()))

        self.assertEqual([settings.chat_id for settings in due_chats], [GROUP_ID])


if __name__ == "__main__":
    unittest.main()
