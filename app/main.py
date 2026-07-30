from __future__ import annotations

import logging
from datetime import datetime, timedelta

from croniter import CroniterBadCronError, croniter
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import Settings, load_settings
from app.db import ChatSettings, Database
from app.llm import OpenAIQueryParser, OpenAISummarizer
from app.reasoning import normalize_reasoning_effort
from app.response_style import normalize_response_style
from app.summary_format import (
    build_preview_header,
    build_transcript,
    format_summary_for_telegram,
)
from app.summary_query import SummaryQueryError, resolve_query
from app.time_utils import compute_next_run_utc, parse_timezone, to_iso, utc_now


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram-summary-bot")

MESSAGE_RETENTION_DAYS = 180


class SummaryBot:
    def __init__(self, settings: Settings):
        self.settings = settings
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
        application.job_queue.run_repeating(self.cleanup_tick, interval=24 * 3600, first=120)
        logger.info("Bot initialized")

    async def post_shutdown(self, _: Application) -> None:
        await self.db.close()

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        await update.effective_message.reply_text(
            "已啟動。\n"
            "可用指令：\n"
            "/summary - 立即產生摘要\n"
            "/summary <條件> - 針對指定時間範圍或主題摘要，例如 /summary 這兩週以來討論到露營的事情\n"
            f"/preview - 私訊預覽過去 {self.settings.preview_window_hours} 小時摘要 (僅擁有者)\n"
            "/status - 查看目前設定\n"
            "/set_schedule <cron> - 設定排程 (僅擁有者)\n"
            "/set_timezone <tz> - 設定時區 (僅擁有者)\n"
            "/set_model <model> - 設定模型 (僅擁有者)\n"
            "/set_reasoning <default|none|minimal|low|medium|high|xhigh|max> - 設定 reasoning (僅擁有者)\n"
            "/set_style <normal|funny|roast> - 設定摘要風格 (僅擁有者)\n"
            "/set_auto <on|off> - 開關自動摘要 (僅擁有者)\n"
            "\n"
            "提醒：請在 BotFather 關閉 privacy mode，才能接收群組完整訊息。\n"
            "提醒：/preview 結果只會私訊擁有者，擁有者需先私訊 bot 一次。"
        )

    async def status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        settings = await self.db.get_chat_settings(chat.id)
        last = await self.db.get_last_summarized_at(chat.id)

        text = (
            f"chat_id: {chat.id}\n"
            f"timezone: {settings.timezone}\n"
            f"schedule(cron): {settings.cron_expr}\n"
            f"auto: {'on' if settings.auto_enabled else 'off'}\n"
            f"model: {settings.model}\n"
            f"reasoning_effort: {settings.reasoning_effort}\n"
            f"response_style: {settings.response_style}\n"
            f"min_messages_to_summary: {self.settings.min_messages_to_summary}\n"
            f"max_summary_gap_hours: {self.settings.max_summary_gap_hours}\n"
            f"openai_max_output_tokens: {self.settings.openai_max_output_tokens}\n"
            f"next_run_utc: {settings.next_run_at_utc}\n"
            f"last_summarized_utc: {last or 'NULL'}"
        )
        await message.reply_text(text)

    async def set_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        cron_expr = " ".join(context.args).strip()
        if not cron_expr:
            await message.reply_text("用法：/set_schedule <cron>，例如 /set_schedule 0 9 * * *")
            return

        settings = await self.db.get_chat_settings(chat.id)

        try:
            tz = parse_timezone(settings.timezone)
            croniter(cron_expr, utc_now().astimezone(tz))
        except (CroniterBadCronError, ValueError) as exc:
            await message.reply_text(f"cron 格式錯誤：{exc}")
            return

        updated = await self.db.update_chat_settings(
            chat.id,
            cron_expr=cron_expr,
            recompute_next_run=True,
        )
        await message.reply_text(
            f"已更新排程為 {updated.cron_expr}，下一次執行 UTC: {updated.next_run_at_utc}"
        )

    async def set_timezone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        if not context.args:
            await message.reply_text("用法：/set_timezone <timezone>，例如 /set_timezone UTC+8 或 /set_timezone Asia/Taipei")
            return

        tz_text = " ".join(context.args).strip()
        settings = await self.db.get_chat_settings(chat.id)

        try:
            tz = parse_timezone(tz_text)
            croniter(settings.cron_expr, utc_now().astimezone(tz))
        except (CroniterBadCronError, ValueError) as exc:
            await message.reply_text(f"時區格式錯誤：{exc}")
            return

        updated = await self.db.update_chat_settings(
            chat.id,
            timezone=tz_text,
            recompute_next_run=True,
        )
        await message.reply_text(f"已更新時區為 `{updated.timezone}`，下一次執行 UTC: {updated.next_run_at_utc}")

    async def set_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        model = " ".join(context.args).strip()
        if not model:
            await message.reply_text("用法：/set_model <model_name>")
            return

        updated = await self.db.update_chat_settings(chat.id, model=model)
        await message.reply_text(f"已更新模型為 `{updated.model}`")

    async def set_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        value = " ".join(context.args).strip().lower()
        if value not in {"on", "off"}:
            await message.reply_text("用法：/set_auto <on|off>")
            return

        updated = await self.db.update_chat_settings(chat.id, auto_enabled=(value == "on"))
        await message.reply_text(f"自動摘要已設為 {'on' if updated.auto_enabled else 'off'}")

    async def set_reasoning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        try:
            value = normalize_reasoning_effort(" ".join(context.args))
        except ValueError:
            await message.reply_text(
                "用法：/set_reasoning <default|none|minimal|low|medium|high|xhigh|max>"
            )
            return

        updated = await self.db.update_chat_settings(chat.id, reasoning_effort=value)
        await message.reply_text(
            f"已更新 reasoning_effort 為 `{updated.reasoning_effort}`；"
            "若目前模型不支援，產生摘要時會自動 fallback 到模型預設。"
        )

    async def set_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        try:
            value = normalize_response_style(" ".join(context.args))
        except ValueError:
            await message.reply_text("用法：/set_style <normal|funny|roast>")
            return

        updated = await self.db.update_chat_settings(chat.id, response_style=value)
        await message.reply_text(f"已更新摘要風格為 `{updated.response_style}`")

    async def manual_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return

        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        raw_args = " ".join(context.args).strip() if context.args else ""
        if raw_args:
            await self._run_custom_summary(chat.id, message, raw_args)
            return

        await message.reply_text("開始產生摘要，請稍候...")
        try:
            posted = await self.generate_and_post_summary(chat.id, triggered_by="manual")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Manual summary failed for chat %s: %s", chat.id, exc)
            await message.reply_text("產生摘要失敗，請稍後再試。")
            return
        if not posted:
            await message.reply_text("這段期間沒有可摘要的文字訊息。")

    async def _run_custom_summary(self, chat_id: int, message: Message, raw_args: str) -> None:
        settings = await self.db.get_chat_settings(chat_id)
        now = utc_now()
        try:
            parsed = await self.query_parser.parse(
                text=raw_args,
                model=settings.model,
                timezone_text=settings.timezone,
                now_utc=now,
            )
            query = resolve_query(
                parsed,
                timezone_text=settings.timezone,
                now_utc=now,
                retention_days=MESSAGE_RETENTION_DAYS,
            )
        except SummaryQueryError as exc:
            await message.reply_text(str(exc))
            return

        topic_part = f"；主題：{query.topic}" if query.topic else ""
        await message.reply_text(
            f"開始整理摘要（範圍：{query.range_label}{topic_part}），請稍候..."
        )

        from_iso_override = to_iso(query.start_utc) if query.start_utc else None
        to_iso_override = to_iso(query.end_utc) if query.end_utc else None

        try:
            posted = await self.generate_and_post_summary(
                chat_id,
                triggered_by="custom",
                topic=query.topic,
                from_iso_override=from_iso_override,
                to_iso_override=to_iso_override,
                advance_cursor=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Custom summary failed for chat %s: %s", chat_id, exc)
            await message.reply_text("產生摘要失敗，請稍後再試。")
            return

        if not posted:
            if query.topic:
                await message.reply_text(f"在這個範圍內找不到與「{query.topic}」相關的訊息。")
            else:
                await message.reply_text("在這個範圍內找不到可摘要的文字訊息。")

    async def preview_summary(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return

        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return

        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.reply_text(
                "請在要預覽的群組內輸入 /preview，預覽結果會私訊給你。"
            )
            return

        window_hours = self.settings.preview_window_hours
        chat_title, chat_username = await self._resolve_chat_metadata(chat.id)
        # Probing the DM first fails fast before spending an LLM call, and the
        # group is intentionally never used to report preview status.
        if not await self._notify_owner(
            f"開始產生「{chat_title}」過去 {window_hours} 小時的預覽摘要，請稍候..."
        ):
            return

        end_time = utc_now()
        start_time = end_time - timedelta(hours=window_hours)
        total_count, rows = await self.db.get_messages_for_summary(
            chat_id=chat.id,
            from_utc_iso=to_iso(start_time),
            to_utc_iso=to_iso(end_time),
            limit=self.settings.max_messages_per_summary,
        )

        if not rows:
            await self._notify_owner(
                f"「{chat_title}」過去 {window_hours} 小時沒有可摘要的文字訊息。"
            )
            return

        settings = await self.db.get_chat_settings(chat.id)
        try:
            full_text = await self._render_summary(
                chat_id=chat.id,
                chat_title=chat_title,
                chat_username=chat_username,
                settings=settings,
                rows=rows,
                total_count=total_count,
                triggered_by="preview",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Preview summary failed for chat %s: %s", chat.id, exc)
            await self._notify_owner("產生預覽摘要失敗，請稍後再試。")
            return

        if not full_text:
            await self._notify_owner("產生預覽摘要失敗，請稍後再試。")
            return

        await self._notify_owner(
            f"{build_preview_header(chat_title=chat_title, chat_id=chat.id, window_hours=window_hours)}\n\n{full_text}",
            parse_mode="HTML",
        )

    async def scheduler_tick(self, _: ContextTypes.DEFAULT_TYPE) -> None:
        now_iso = to_iso(utc_now())
        due_chats = await self.db.get_due_chats(now_iso)

        for chat_settings in due_chats:
            try:
                await self.generate_and_post_summary(chat_settings.chat_id, triggered_by="auto")
            except Exception as exc:  # noqa: BLE001
                logger.exception("Auto summary failed for chat %s: %s", chat_settings.chat_id, exc)
            finally:
                next_run = compute_next_run_utc(chat_settings.cron_expr, chat_settings.timezone)
                await self.db.set_next_run(chat_settings.chat_id, to_iso(next_run))

    async def cleanup_tick(self, _: ContextTypes.DEFAULT_TYPE) -> None:
        cutoff = utc_now() - timedelta(days=MESSAGE_RETENTION_DAYS)
        await self.db.purge_old_messages(to_iso(cutoff))

    async def capture_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user:
            return

        if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            return
        if user.is_bot:
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

    @staticmethod
    def _resolve_reply_target(message: Message, chat_id: int) -> int | None:
        reply_to = getattr(message, "reply_to_message", None)
        if reply_to is None:
            return None

        # Quotes of messages from other chats cannot be linked to this chat's thread.
        reply_chat = getattr(reply_to, "chat", None)
        if reply_chat is not None and getattr(reply_chat, "id", chat_id) != chat_id:
            return None
        return getattr(reply_to, "message_id", None)

    async def generate_and_post_summary(
        self,
        chat_id: int,
        triggered_by: str,
        *,
        topic: str | None = None,
        from_iso_override: str | None = None,
        to_iso_override: str | None = None,
        advance_cursor: bool = True,
    ) -> bool:
        settings = await self.db.get_chat_settings(chat_id)
        end_time = utc_now()
        end_iso = to_iso_override or to_iso(end_time)
        if from_iso_override is not None:
            from_iso_time = from_iso_override
        else:
            from_iso_time = await self.db.get_last_summarized_at(chat_id)

        total_count, rows = await self.db.get_messages_for_summary(
            chat_id=chat_id,
            from_utc_iso=from_iso_time,
            to_utc_iso=end_iso,
            limit=self.settings.max_messages_per_summary,
        )

        if not rows:
            return False
        if triggered_by == "auto" and self._should_skip_auto_summary_for_low_volume(
            rows=rows,
            total_count=total_count,
            end_time=end_time,
        ):
            return False

        chat_title, chat_username = await self._resolve_chat_metadata(chat_id)
        full_text = await self._render_summary(
            chat_id=chat_id,
            chat_title=chat_title,
            chat_username=chat_username,
            settings=settings,
            rows=rows,
            total_count=total_count,
            triggered_by=triggered_by,
            topic=topic,
        )
        if not full_text:
            return False

        try:
            await self.application.bot.send_message(
                chat_id=chat_id,
                text=full_text,
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Send message failed for chat %s: %s", chat_id, exc)
            return False

        if advance_cursor:
            await self.db.set_last_summarized_at(chat_id, end_iso)
        return True

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

    async def _notify_owner(self, text: str, *, parse_mode: str | None = None) -> bool:
        owner_id = self.settings.owner_telegram_user_id
        try:
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

    async def _render_summary(
        self,
        *,
        chat_id: int,
        chat_title: str,
        chat_username: str | None,
        settings: ChatSettings,
        rows: list,
        total_count: int,
        triggered_by: str,
        topic: str | None = None,
    ) -> str | None:
        summary_start = self._format_summary_date(rows[0]["created_at_utc"])
        summary_end = self._format_summary_date(rows[-1]["created_at_utc"])
        summary_range = f"{summary_start} to {summary_end}"
        transcript = self._build_transcript(
            rows=rows,
            total_count=total_count,
            chat_title=chat_title,
            summary_range=summary_range,
            chat_id=chat_id,
            chat_username=chat_username,
        )
        logger.info(
            "Start summary generation (chat_id=%s triggered_by=%s model=%s reasoning_effort=%s response_style=%s pending_total=%s transcript_chars=%s)",
            chat_id,
            triggered_by,
            settings.model,
            settings.reasoning_effort,
            settings.response_style,
            total_count,
            len(transcript),
        )
        summary_text = await self.summarizer.summarize(
            transcript=transcript,
            model=settings.model,
            reasoning_effort=settings.reasoning_effort,
            response_style=settings.response_style,
            topic=topic,
        )

        full_text = format_summary_for_telegram(summary_text)
        if not full_text:
            logger.error(
                "Summary generation returned empty text for chat %s (model=%s)",
                chat_id,
                settings.model,
            )
            return None
        return full_text

    @staticmethod
    def _build_transcript(
        *,
        rows: list,
        total_count: int,
        chat_title: str,
        summary_range: str,
        chat_id: int,
        chat_username: str | None,
    ) -> str:
        return build_transcript(
            rows=rows,
            total_count=total_count,
            chat_title=chat_title,
            summary_range=summary_range,
            chat_id=chat_id,
            chat_username=chat_username,
        )

    async def _resolve_chat_metadata(self, chat_id: int) -> tuple[str, str | None]:
        try:
            chat = await self.application.bot.get_chat(chat_id)
            title = getattr(chat, "title", None) or f"Chat {chat_id}"
            return title, getattr(chat, "username", None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to resolve chat metadata for %s: %s", chat_id, exc)
        return f"Chat {chat_id}", None

    @staticmethod
    def _format_summary_date(iso_timestamp: str) -> str:
        return datetime.fromisoformat(iso_timestamp).strftime("%Y-%m-%d")

    def _should_skip_auto_summary_for_low_volume(self, *, rows: list, total_count: int, end_time: datetime) -> bool:
        if total_count >= self.settings.min_messages_to_summary:
            return False

        oldest_pending = datetime.fromisoformat(rows[0]["created_at_utc"])
        pending_age_hours = (end_time - oldest_pending).total_seconds() / 3600
        if pending_age_hours >= self.settings.max_summary_gap_hours:
            return False

        logger.info(
            "Skip auto summary for low message volume: total_count=%s, min_required=%s, pending_age_hours=%.2f, max_gap_hours=%s",
            total_count,
            self.settings.min_messages_to_summary,
            pending_age_hours,
            self.settings.max_summary_gap_hours,
        )
        return True


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

    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.start))
    application.add_handler(CommandHandler("status", bot.status))
    application.add_handler(CommandHandler("summary", bot.manual_summary))
    application.add_handler(CommandHandler("preview", bot.preview_summary))
    application.add_handler(CommandHandler("set_schedule", bot.set_schedule))
    application.add_handler(CommandHandler("set_timezone", bot.set_timezone))
    application.add_handler(CommandHandler("set_model", bot.set_model))
    application.add_handler(CommandHandler("set_reasoning", bot.set_reasoning))
    application.add_handler(CommandHandler("set_style", bot.set_style))
    application.add_handler(CommandHandler("set_auto", bot.set_auto))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, bot.capture_message),
    )

    return application


def main() -> None:
    settings = load_settings()
    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
