from __future__ import annotations

from datetime import datetime, timedelta

from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

from app.bot.base import logger
from app.db import ChatSettings
from app.summary_format import (
    build_preview_header,
    build_transcript,
    format_summary_for_telegram,
)
from app.summary_query import SummaryQueryError, resolve_query
from app.time_utils import compute_next_run_utc, to_iso, utc_now


class SummaryMixin:
    async def manual_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update) or not await self._assert_authorized_group(update):
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
                retention_days=self.settings.message_retention_days,
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
        if not await self._assert_owner(update) or not await self._assert_authorized_group(update):
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
            disable_link_preview=True,
        )

    async def scheduler_tick(self, _: ContextTypes.DEFAULT_TYPE) -> None:
        now_iso = to_iso(utc_now())
        due_chats = await self.db.get_due_chats(now_iso)

        for chat_settings in due_chats:
            try:
                await self.generate_and_post_summary(
                    chat_settings.chat_id,
                    triggered_by="auto",
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Auto summary failed for chat %s: %s",
                    chat_settings.chat_id,
                    exc,
                )
            finally:
                next_run = compute_next_run_utc(
                    chat_settings.cron_expr,
                    chat_settings.timezone,
                )
                await self.db.set_next_run(chat_settings.chat_id, to_iso(next_run))

    async def cleanup_tick(self, _: ContextTypes.DEFAULT_TYPE) -> None:
        cutoff = utc_now() - timedelta(days=self.settings.message_retention_days)
        await self.db.purge_old_messages(to_iso(cutoff))

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
        if not await self.db.is_chat_authorized(chat_id):
            logger.warning("Skip summary for unauthorized chat %s", chat_id)
            return False

        settings = await self.db.get_chat_settings(chat_id)
        end_time = utc_now()
        end_iso = to_iso_override or to_iso(end_time)
        from_iso_time = (
            from_iso_override
            if from_iso_override is not None
            else await self.db.get_last_summarized_at(chat_id)
        )
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
                link_preview_options=self._disabled_link_preview(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Send message failed for chat %s: %s", chat_id, exc)
            return False

        if advance_cursor:
            await self.db.set_last_summarized_at(chat_id, end_iso)
        if triggered_by == "auto":
            await self._notify_subscribers(chat_id, full_text)
        return True

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

    @staticmethod
    def _format_summary_date(iso_timestamp: str) -> str:
        return datetime.fromisoformat(iso_timestamp).strftime("%Y-%m-%d")

    def _should_skip_auto_summary_for_low_volume(
        self,
        *,
        rows: list,
        total_count: int,
        end_time: datetime,
    ) -> bool:
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
