from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from app.bot.base import BotBase
from app.bot.commands import CommandMixin
from app.bot.group import GroupMixin
from app.bot.subscriptions import SubscriptionMixin
from app.bot.summaries import SummaryMixin
from app.config import Settings, load_settings


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


class SummaryBot(
    BotBase,
    CommandMixin,
    GroupMixin,
    SubscriptionMixin,
    SummaryMixin,
):
    """Aggregate the independently testable Telegram Bot behaviours."""


def build_application(settings: Settings) -> Application:
    bot = SummaryBot(settings)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(bot.post_init)
        .post_shutdown(bot.post_shutdown)
        .build()
    )
    bot.application = application

    application.add_handler(
        ChatMemberHandler(bot.handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
    )
    application.add_handler(
        ChatMemberHandler(bot.handle_owner_chat_member, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.start))
    application.add_handler(CommandHandler("subscribe", bot.subscribe))
    application.add_handler(CommandHandler("unsubscribe", bot.unsubscribe))
    application.add_handler(CommandHandler("status", bot.status))
    application.add_handler(CommandHandler("summary", bot.manual_summary))
    application.add_handler(CommandHandler("preview", bot.preview_summary))
    application.add_handler(CommandHandler("set_schedule", bot.set_schedule))
    application.add_handler(CommandHandler("set_timezone", bot.set_timezone))
    application.add_handler(CommandHandler("set_model", bot.set_model))
    application.add_handler(CommandHandler("set_reasoning", bot.set_reasoning))
    application.add_handler(CommandHandler("set_style", bot.set_style))
    application.add_handler(CommandHandler("set_auto", bot.set_auto))
    application.add_handler(CallbackQueryHandler(bot.handle_subscription_callback))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, bot.capture_message),
    )
    return application


def main() -> None:
    app = build_application(load_settings())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
