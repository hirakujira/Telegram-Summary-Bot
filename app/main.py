from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
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
    application.add_handler(CommandHandler("authorize_group", bot.authorize_group))
    application.add_handler(CommandHandler("status", bot.status))
    application.add_handler(CommandHandler("summary", bot.manual_summary))
    application.add_handler(CommandHandler("user_summary_history", bot.user_summary_history))
    application.add_handler(CommandHandler("preview", bot.preview_summary))
    setting_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("set_schedule", bot.set_schedule),
            CommandHandler("set_timezone", bot.set_timezone),
            CommandHandler("set_model", bot.set_model),
            CommandHandler("set_reasoning", bot.set_reasoning),
            CommandHandler("set_style", bot.set_style),
            CommandHandler("set_auto", bot.set_auto),
        ],
        states={
            bot.SETTING_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.receive_setting_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", bot.cancel_setting)],
        name="group-setting",
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )
    application.add_handler(setting_conversation)
    application.add_handler(CommandHandler("cancel", bot.cancel_without_setting))
    application.add_handler(
        CallbackQueryHandler(bot.handle_subscription_callback, pattern=r"^(subscribe|unsubscribe):")
    )
    application.add_handler(
        CallbackQueryHandler(bot.handle_user_summary_callback, pattern=r"^user_summary:")
    )
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, bot.capture_message),
    )
    return application


def main() -> None:
    app = build_application(load_settings())
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
