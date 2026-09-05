from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telegram import (
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
)
from telegram.constants import ChatType

from app.bot.command_menu import (
    GROUP_OWNER_COMMANDS,
    OWNER_PRIVATE_COMMANDS,
    PRIVATE_COMMANDS,
    delete_group_owner_command_menu,
    sync_private_command_menus,
)
from app.config import Settings
from app.main import SummaryBot


OWNER_ID = 777
GROUP_ID = -1001234567890


class FakeTelegramBot:
    def __init__(self, *, fail_set: bool = False, fail_delete: bool = False) -> None:
        self.fail_set = fail_set
        self.fail_delete = fail_delete
        self.set_calls: list[tuple[object, list[str]]] = []
        self.delete_calls: list[object] = []

    async def set_my_commands(self, commands, *, scope) -> bool:
        if self.fail_set:
            raise RuntimeError("set failed")
        self.set_calls.append((scope, [command.command for command in commands]))
        return True

    async def delete_my_commands(self, *, scope) -> bool:
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.delete_calls.append(scope)
        return True


class FakeJobQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int, int]] = []

    def run_repeating(self, *_args, **_kwargs) -> None:
        self.calls.append((_args[0], _kwargs["interval"], _kwargs["first"]))


def membership_update(*, old_status: str, new_status: str) -> SimpleNamespace:
    return SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID, type=ChatType.SUPERGROUP),
            from_user=SimpleNamespace(id=OWNER_ID),
            old_chat_member=SimpleNamespace(status=old_status),
            new_chat_member=SimpleNamespace(status=new_status),
        )
    )


def owner_membership_update(*, old_status: str, new_status: str) -> SimpleNamespace:
    return SimpleNamespace(
        chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=GROUP_ID, type=ChatType.SUPERGROUP),
            old_chat_member=SimpleNamespace(
                status=old_status,
                user=SimpleNamespace(id=OWNER_ID),
            ),
            new_chat_member=SimpleNamespace(
                status=new_status,
                user=SimpleNamespace(id=OWNER_ID),
            ),
        )
    )


class CommandMenuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.bot = SummaryBot(
            Settings(
                telegram_bot_token="test-token",
                openai_api_key="test-key",
                owner_telegram_user_id=OWNER_ID,
                sqlite_path=str(Path(self.temp_dir.name) / "bot.db"),
            )
        )
        self.telegram = FakeTelegramBot()
        self.bot.application = SimpleNamespace(bot=self.telegram)
        await self.bot.db.connect()

    async def asyncTearDown(self) -> None:
        await self.bot.db.close()
        self.temp_dir.cleanup()

    async def test_private_menus_include_general_and_owner_override(self) -> None:
        await sync_private_command_menus(self.telegram, OWNER_ID)

        general_scope, general_commands = self.telegram.set_calls[0]
        owner_scope, owner_commands = self.telegram.set_calls[1]
        self.assertIsInstance(general_scope, BotCommandScopeAllPrivateChats)
        self.assertEqual(general_commands, [command.command for command in PRIVATE_COMMANDS])
        self.assertIsInstance(owner_scope, BotCommandScopeChat)
        self.assertEqual(owner_scope.chat_id, OWNER_ID)
        self.assertEqual(owner_commands, [command.command for command in OWNER_PRIVATE_COMMANDS])
        self.assertNotIn("set_schedule", owner_commands)
        self.assertNotIn("authorize_group", owner_commands)

    def test_group_owner_menu_includes_authorize_group(self) -> None:
        commands = [command.command for command in GROUP_OWNER_COMMANDS]

        self.assertIn("authorize_group", commands)

    async def test_post_init_syncs_existing_authorized_group_menu(self) -> None:
        await self.bot.db.authorize_chat(GROUP_ID)
        await self.bot.db.close()
        job_queue = FakeJobQueue()
        application = SimpleNamespace(bot=self.telegram, job_queue=job_queue)

        await self.bot.post_init(application)

        group_scope, group_commands = self.telegram.set_calls[2]
        self.assertIsInstance(group_scope, BotCommandScopeChatMember)
        self.assertEqual(group_scope.chat_id, GROUP_ID)
        self.assertEqual(group_scope.user_id, OWNER_ID)
        self.assertEqual(group_commands, [command.command for command in GROUP_OWNER_COMMANDS])
        self.assertEqual(len(job_queue.calls), 2)
        self.assertEqual(job_queue.calls[0][1:], (120, 10))

    async def test_menu_api_failure_does_not_block_initialization(self) -> None:
        await self.bot.db.close()
        self.telegram.fail_set = True
        job_queue = FakeJobQueue()

        await self.bot.post_init(SimpleNamespace(bot=self.telegram, job_queue=job_queue))

        self.assertEqual(len(job_queue.calls), 2)

    async def test_authorization_adds_and_revocation_deletes_group_menu(self) -> None:
        await self.bot.handle_my_chat_member(
            membership_update(old_status="left", new_status="member"),
            None,
        )
        await self.bot.handle_my_chat_member(
            membership_update(old_status="member", new_status="left"),
            None,
        )

        group_scope, group_commands = self.telegram.set_calls[0]
        self.assertIsInstance(group_scope, BotCommandScopeChatMember)
        self.assertEqual(group_commands, [command.command for command in GROUP_OWNER_COMMANDS])
        self.assertEqual(len(self.telegram.delete_calls), 1)
        self.assertEqual(self.telegram.delete_calls[0].chat_id, GROUP_ID)

    async def test_owner_leaving_deletes_group_menu(self) -> None:
        await self.bot.db.authorize_chat(GROUP_ID)

        await self.bot.handle_owner_chat_member(
            owner_membership_update(old_status="member", new_status="left"),
            None,
        )

        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))
        self.assertEqual(len(self.telegram.delete_calls), 1)
        self.assertEqual(self.telegram.delete_calls[0].chat_id, GROUP_ID)

    async def test_menu_api_failures_do_not_block_lifecycle(self) -> None:
        self.telegram.fail_set = True
        await self.bot.handle_my_chat_member(
            membership_update(old_status="left", new_status="member"),
            None,
        )
        self.assertTrue(await self.bot.db.is_chat_authorized(GROUP_ID))

        self.telegram.fail_delete = True
        await self.bot.handle_my_chat_member(
            membership_update(old_status="member", new_status="left"),
            None,
        )
        self.assertFalse(await self.bot.db.is_chat_authorized(GROUP_ID))

    async def test_missing_menu_api_is_ignored(self) -> None:
        await sync_private_command_menus(SimpleNamespace(), OWNER_ID)
        await delete_group_owner_command_menu(SimpleNamespace(), GROUP_ID, OWNER_ID)


if __name__ == "__main__":
    unittest.main()
