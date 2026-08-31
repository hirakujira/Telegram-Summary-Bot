from __future__ import annotations

import logging

from telegram import LinkPreviewOptions, Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes

from app.config import Settings
from app.db import Database
from app.llm import OpenAIQueryParser, OpenAISummarizer


logger = logging.getLogger("telegram-summary-bot")

ACTIVE_MEMBER_STATUSES = {"member", "administrator", "owner", "creator"}


class BotBase:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.user_summary_requests: dict[int, object] = {}
        self.db = Database(
            path=settings.sqlite_path,
            default_timezone=settings.default_timezone,
            default_cron_expr=settings.default_cron_expr,
            default_model=settings.default_model,
            default_reasoning_effort=settings.default_reasoning_effort,
        )
        self.summarizer = OpenAISummarizer(
            api_key=settings.openai_api_key,
            max_output_tokens=settings.openai_max_output_tokens,
        )
        self.query_parser = OpenAIQueryParser(
            api_key=settings.openai_api_key,
            max_output_tokens=settings.openai_max_output_tokens,
        )

    async def post_init(self, application: Application) -> None:
        await self.db.connect()
        application.job_queue.run_repeating(self.scheduler_tick, interval=30, first=10)
        application.job_queue.run_repeating(
            self.cleanup_tick,
            interval=24 * 3600,
            first=120,
        )
        logger.info("Bot initialized")

    async def post_shutdown(self, _: Application) -> None:
        await self.db.close()

    @property
    def application(self) -> Application:
        if not hasattr(self, "_application"):
            raise RuntimeError("Application not set")
        return self._application

    @application.setter
    def application(self, value: Application) -> None:
        self._application = value

    async def _assert_owner(self, update: Update) -> bool:
        user = update.effective_user
        message = update.effective_message
        if not user or not message:
            return False

        if user.id != self.settings.owner_telegram_user_id:
            await message.reply_text("你沒有權限執行這個指令。")
            return False
        return True

    async def _assert_authorized_group(self, update: Update) -> bool:
        chat = update.effective_chat
        if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return True
        if await self.db.is_chat_authorized(chat.id):
            return True

        await self._notify_and_leave_unauthorized_group(
            chat,
            "偵測到未授權群組的活動",
        )
        return False

    async def _notify_and_leave_unauthorized_group(self, chat, reason: str) -> None:
        chat_title = getattr(chat, "title", None) or "未命名群組"
        await self._notify_owner(
            f"{reason}：{chat_title}（chat_id: {chat.id}）。機器人已退出，"
            "如需使用請由 owner 親自重新加入。"
        )
        try:
            await self.application.bot.leave_chat(chat.id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to leave unauthorized chat %s: %s", chat.id, exc)

    async def _notify_owner(
        self,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_link_preview: bool = False,
    ) -> bool:
        owner_id = self.settings.owner_telegram_user_id
        try:
            if disable_link_preview:
                await self.application.bot.send_message(
                    chat_id=owner_id,
                    text=text,
                    parse_mode=parse_mode,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            else:
                await self.application.bot.send_message(
                    chat_id=owner_id,
                    text=text,
                    parse_mode=parse_mode,
                )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to DM owner %s (owner must send /start to the bot in private first): %s",
                owner_id,
                exc,
            )
            return False

    @staticmethod
    def _disabled_link_preview() -> LinkPreviewOptions:
        return LinkPreviewOptions(is_disabled=True)

    async def _resolve_chat_metadata(self, chat_id: int) -> tuple[str, str | None]:
        try:
            chat = await self.application.bot.get_chat(chat_id)
            title = getattr(chat, "title", None) or f"Chat {chat_id}"
            return title, getattr(chat, "username", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve chat metadata for %s: %s", chat_id, exc)
        return f"Chat {chat_id}", None
