from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
)


logger = logging.getLogger("telegram-summary-bot")


PRIVATE_COMMANDS = (
    BotCommand("start", "開始使用"),
    BotCommand("help", "顯示使用說明"),
    BotCommand("subscribe", "訂閱群組摘要"),
    BotCommand("unsubscribe", "取消群組摘要訂閱"),
    BotCommand("summary", "請求群組摘要"),
)

OWNER_PRIVATE_COMMANDS = (
    *PRIVATE_COMMANDS,
    BotCommand("user_summary_history", "查看使用者摘要紀錄"),
)

GROUP_OWNER_COMMANDS = (
    BotCommand("start", "開始使用"),
    BotCommand("help", "顯示使用說明"),
    BotCommand("authorize_group", "授權舊版既有群組"),
    BotCommand("summary", "立即產生摘要"),
    BotCommand("preview", "私訊預覽摘要"),
    BotCommand("status", "查看群組設定"),
    BotCommand("set_schedule", "設定摘要排程"),
    BotCommand("set_timezone", "設定時區"),
    BotCommand("set_model", "設定摘要模型"),
    BotCommand("set_reasoning", "設定 reasoning 程度"),
    BotCommand("set_style", "設定摘要風格"),
    BotCommand("set_auto", "開關自動摘要"),
    BotCommand("cancel", "取消進行中的設定"),
)


class CommandMenuClient(Protocol):
    """Telegram client interface needed to synchronize command menus."""

    async def set_my_commands(self, commands: Sequence[BotCommand], *, scope: object) -> bool: ...

    async def delete_my_commands(self, *, scope: object) -> bool: ...


async def sync_private_command_menus(client: CommandMenuClient, owner_user_id: int) -> None:
    """Set the general private menu and the owner's private override."""
    await _set_commands(client, PRIVATE_COMMANDS, BotCommandScopeAllPrivateChats())
    await _set_commands(
        client,
        OWNER_PRIVATE_COMMANDS,
        BotCommandScopeChat(chat_id=owner_user_id),
    )


async def sync_group_owner_command_menu(
    client: CommandMenuClient,
    chat_id: int,
    owner_user_id: int,
) -> None:
    """Set the owner-only command menu for an authorized group."""
    await _set_commands(
        client,
        GROUP_OWNER_COMMANDS,
        BotCommandScopeChatMember(chat_id=chat_id, user_id=owner_user_id),
    )


async def delete_group_owner_command_menu(
    client: CommandMenuClient,
    chat_id: int,
    owner_user_id: int,
) -> None:
    """Remove the owner-only command menu after a group is deauthorized."""
    delete_commands = getattr(client, "delete_my_commands", None)
    if not callable(delete_commands):
        return

    scope = BotCommandScopeChatMember(chat_id=chat_id, user_id=owner_user_id)
    try:
        await delete_commands(scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to delete command menu for chat %s: %s", chat_id, exc)


async def _set_commands(
    client: CommandMenuClient,
    commands: Sequence[BotCommand],
    scope: object,
) -> None:
    set_commands = getattr(client, "set_my_commands", None)
    if not callable(set_commands):
        return

    try:
        await set_commands(commands, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to set command menu for scope %s: %s", scope, exc)
