from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from app.bot.base import logger


class SubscriptionMixin:
    async def subscribe(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return
        if chat.type != ChatType.PRIVATE:
            await message.reply_text("請私訊機器人使用 /subscribe。")
            return

        subscribed_chat_ids = set(await self.db.get_subscribed_chat_ids(user.id))
        buttons: list[list[InlineKeyboardButton]] = []
        for chat_id in await self.db.get_authorized_chat_ids():
            if chat_id in subscribed_chat_ids:
                continue
            if not await self._check_active_membership(chat_id, user.id):
                continue
            chat_title, _ = await self._resolve_chat_metadata(chat_id)
            buttons.append(
                [
                    InlineKeyboardButton(
                        chat_title,
                        callback_data=f"subscribe:{chat_id}",
                    )
                ]
            )

        subscribed_summary = await self._build_subscribed_summary(subscribed_chat_ids)
        if not buttons:
            if not subscribed_chat_ids:
                await message.reply_text("目前沒有你可訂閱的群組摘要。")
                return

            await message.reply_text(
                "目前沒有其他可訂閱的群組摘要。" + subscribed_summary
            )
            return

        await message.reply_text(
            "請選擇要訂閱排程摘要的群組：" + subscribed_summary,
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def unsubscribe(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return
        if chat.type != ChatType.PRIVATE:
            await message.reply_text("請私訊機器人使用 /unsubscribe。")
            return

        buttons: list[list[InlineKeyboardButton]] = []
        for chat_id in await self.db.get_subscribed_chat_ids(user.id):
            chat_title, _ = await self._resolve_chat_metadata(chat_id)
            buttons.append(
                [
                    InlineKeyboardButton(
                        chat_title,
                        callback_data=f"unsubscribe:{chat_id}",
                    )
                ]
            )

        if not buttons:
            await message.reply_text("你目前沒有任何摘要訂閱。")
            return

        await message.reply_text(
            "請選擇要取消訂閱的群組：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def handle_subscription_callback(
        self,
        update: Update,
        _: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        if not query or not query.from_user:
            return

        await query.answer()
        message = query.message
        if not message or message.chat.type != ChatType.PRIVATE:
            await query.edit_message_text("請私訊機器人管理摘要訂閱。")
            return

        action, separator, raw_chat_id = (query.data or "").partition(":")
        if not separator:
            await query.edit_message_text("這個訂閱操作無效，請重新輸入指令。")
            return
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            await query.edit_message_text("這個訂閱操作無效，請重新輸入指令。")
            return

        if action == "subscribe":
            if not await self.db.is_chat_authorized(chat_id):
                await query.edit_message_text("這個群組目前無法訂閱，請重新輸入 /subscribe。")
                return
            if not await self._check_active_membership(chat_id, query.from_user.id):
                await query.edit_message_text("無法確認你仍在這個群組，因此未建立訂閱。")
                return

            chat_title, _ = await self._resolve_chat_metadata(chat_id)
            if await self.db.add_subscription(query.from_user.id, chat_id):
                await query.edit_message_text(f"已訂閱「{chat_title}」的排程摘要。")
            else:
                await query.edit_message_text(f"你已訂閱「{chat_title}」的排程摘要。")
            return

        if action == "unsubscribe":
            chat_title, _ = await self._resolve_chat_metadata(chat_id)
            if await self.db.remove_subscription(query.from_user.id, chat_id):
                await query.edit_message_text(f"已取消「{chat_title}」的摘要訂閱。")
            else:
                await query.edit_message_text("這個訂閱已不存在。")
            return

        await query.edit_message_text("這個訂閱操作無效，請重新輸入指令。")

    async def _build_subscribed_summary(self, chat_ids: set[int]) -> str:
        if not chat_ids:
            return ""

        titles = []
        for chat_id in sorted(chat_ids):
            chat_title, _ = await self._resolve_chat_metadata(chat_id)
            titles.append(f"- {chat_title}")
        return "\n\n你目前已訂閱：\n" + "\n".join(titles)

    async def _check_active_membership(self, chat_id: int, user_id: int) -> bool | None:
        try:
            chat_member = await self.application.bot.get_chat_member(chat_id, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to verify subscriber membership (chat_id=%s user_id=%s): %s",
                chat_id,
                user_id,
                exc,
            )
            return None

        return self._is_active_chat_member(chat_member)

    async def _notify_subscribers(self, chat_id: int, full_text: str) -> None:
        for user_id in await self.db.get_subscriber_ids(chat_id):
            membership = await self._check_active_membership(chat_id, user_id)
            if membership is None:
                continue
            if not membership:
                await self.db.remove_subscription(user_id, chat_id)
                logger.info(
                    "Removed stale subscription (chat_id=%s user_id=%s)",
                    chat_id,
                    user_id,
                )
                continue

            try:
                await self.application.bot.send_message(
                    chat_id=user_id,
                    text=full_text,
                    parse_mode="HTML",
                    link_preview_options=self._disabled_link_preview(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to notify subscriber (chat_id=%s user_id=%s): %s",
                    chat_id,
                    user_id,
                    exc,
                )
