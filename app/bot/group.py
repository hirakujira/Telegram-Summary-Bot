from __future__ import annotations

from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from app.bot.base import ACTIVE_MEMBER_STATUSES, logger
from app.bot.command_menu import (
    delete_group_owner_command_menu,
    sync_group_owner_command_menu,
)
from app.time_utils import to_iso


class GroupMixin:
    async def capture_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return

        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP} or user.is_bot:
            return
        if not await self._assert_authorized_group(update):
            return
        if self._is_excluded_message(message):
            return

        text = (message.text or message.caption or "").strip()
        if not text:
            return

        display_name = user.full_name
        if user.username:
            display_name = f"{display_name} (@{user.username})"

        await self.db.ensure_chat(chat.id)
        await self.db.save_text_message(
            chat_id=chat.id,
            message_id=message.message_id,
            user_id=user.id,
            user_name=display_name,
            text=text,
            created_at_utc=to_iso(message.date),
            reply_to_message_id=self._resolve_reply_target(message, chat.id),
        )

    async def handle_my_chat_member(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        membership = update.my_chat_member
        if not membership:
            return

        chat = membership.chat
        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return

        was_active = self._is_active_chat_member(membership.old_chat_member)
        is_active = self._is_active_chat_member(membership.new_chat_member)

        if not was_active and is_active:
            added_by = membership.from_user
            if added_by and added_by.id == self.settings.owner_telegram_user_id:
                await self.db.authorize_chat(chat.id)
                await sync_group_owner_command_menu(
                    self.application.bot,
                    chat.id,
                    self.settings.owner_telegram_user_id,
                )
                logger.info("Authorized group %s because owner added the bot", chat.id)
                return

            await self._notify_and_leave_unauthorized_group(
                chat,
                "機器人由非 owner 帳號加入",
            )
            return

        if was_active and not is_active:
            await self.db.revoke_chat_authorization(chat.id)
            await delete_group_owner_command_menu(
                self.application.bot,
                chat.id,
                self.settings.owner_telegram_user_id,
            )
            logger.info("Revoked authorization for group %s after bot removal", chat.id)

    async def handle_owner_chat_member(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        membership = update.chat_member
        if not membership:
            return

        chat = membership.chat
        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return

        member = membership.new_chat_member
        user = getattr(member, "user", None)
        if not user or user.id != self.settings.owner_telegram_user_id:
            return

        if (
            not self._is_active_chat_member(membership.old_chat_member)
            or self._is_active_chat_member(member)
        ):
            return

        await self.db.revoke_chat_authorization(chat.id)
        await delete_group_owner_command_menu(
            self.application.bot,
            chat.id,
            self.settings.owner_telegram_user_id,
        )
        await self._notify_and_leave_unauthorized_group(
            chat,
            "owner 已離開群組",
        )
        logger.info("Revoked authorization for group %s after owner left", chat.id)

    @staticmethod
    def _chat_member_status(chat_member: object) -> str:
        status = getattr(chat_member, "status", "")
        return getattr(status, "value", status)

    @classmethod
    def _is_active_chat_member(cls, chat_member: object) -> bool:
        status = cls._chat_member_status(chat_member)
        if status in ACTIVE_MEMBER_STATUSES:
            return True
        return status == "restricted" and bool(getattr(chat_member, "is_member", False))

    @staticmethod
    def _resolve_reply_target(message: Message, chat_id: int) -> int | None:
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to is None:
            return None

        reply_chat = getattr(reply_to, "chat", None)
        if reply_chat is not None and getattr(reply_chat, "id", chat_id) != chat_id:
            return None
        return getattr(reply_to, "message_id", None)

    @staticmethod
    def _is_excluded_message(message: Message) -> bool:
        return any(
            [
                message.sticker,
                message.photo,
                message.video,
                message.voice,
                message.audio,
                message.video_note,
                message.animation,
            ]
        )
