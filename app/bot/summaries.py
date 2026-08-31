from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from secrets import token_urlsafe

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatType
from telegram.error import TimedOut
from telegram.ext import ContextTypes

from app.bot.base import logger
from app.db import ChatSettings
from app.summary_format import (
    build_preview_header,
    build_transcript,
    format_summary_for_telegram,
)
from app.summary_query import SummaryQueryError, resolve_query
from app.time_utils import compute_next_run_utc, parse_timezone, to_iso, utc_now


@dataclass(slots=True)
class UserSummaryRequest:
    token: str
    user_id: int
    prompt: str
    expires_at: datetime


class SummaryMixin:
    async def manual_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat and chat.type == ChatType.PRIVATE:
            await self.user_summary(update, context)
            return
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

    async def user_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        prompt = " ".join(context.args).strip() if context.args else ""
        if not prompt:
            await message.reply_text("請輸入條件，例如 /summary 最近三天關於出遊的討論。")
            return
        if len(prompt) > 500:
            await message.reply_text("條件最多 500 個字。")
            return
        if (
            user.id != self.settings.owner_telegram_user_id
            and self.settings.daily_user_summary_limit == 0
        ):
            await message.reply_text("一般使用者私訊摘要目前未啟用。")
            return

        token = token_urlsafe(12)
        self.user_summary_requests[user.id] = UserSummaryRequest(
            token=token,
            user_id=user.id,
            prompt=prompt,
            expires_at=utc_now() + timedelta(minutes=10),
        )
        buttons: list[list[InlineKeyboardButton]] = []
        for chat_id in await self.db.get_authorized_chat_ids():
            if not await self._check_active_membership(chat_id, user.id):
                continue
            title, _ = await self._resolve_chat_metadata(chat_id)
            buttons.append(
                [InlineKeyboardButton(title, callback_data=f"user_summary:{token}:{chat_id}")]
            )
        if not buttons:
            self.user_summary_requests.pop(user.id, None)
            await message.reply_text("目前沒有可確認你仍是成員的已授權群組可供摘要。")
            return
        await message.reply_text(
            "請選擇要摘要的群組（10 分鐘內有效）：",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    async def handle_user_summary_callback(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        callback = update.callback_query
        if not callback or not callback.from_user:
            return
        await callback.answer()
        if not callback.message or callback.message.chat.type != ChatType.PRIVATE:
            await callback.edit_message_text("請私訊機器人使用摘要功能。")
            return
        try:
            _, token, raw_chat_id = (callback.data or "").split(":", 2)
        except ValueError:
            await callback.edit_message_text("這個摘要請求無效，請重新輸入 /summary。")
            return
        request = self.user_summary_requests.get(callback.from_user.id)
        if (
            not request
            or request.token != token
            or request.expires_at < utc_now()
        ):
            await callback.edit_message_text("這個摘要請求已失效，請重新輸入 /summary。")
            return
        # Consume the one-time request before any await so double taps cannot
        # start concurrent summaries (including for the unlimited owner).
        self.user_summary_requests.pop(callback.from_user.id, None)
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            await callback.edit_message_text("這個摘要請求無效，請重新輸入 /summary。")
            return
        if not await self.db.is_chat_authorized(chat_id):
            await callback.edit_message_text("這個群組目前無法摘要，請重新輸入 /summary。")
            return
        settings = await self.db.get_chat_settings(chat_id)
        if not await self._user_summary_access_allowed(chat_id, callback.from_user.id):
            await self._record_user_summary_failure(
                callback.from_user.id, chat_id, request.prompt, settings.timezone
            )
            await callback.edit_message_text("無法確認你仍在這個已授權群組，請重新輸入 /summary。")
            return

        now = utc_now()
        try:
            parsed = await self.query_parser.parse(
                text=request.prompt, model=settings.model, timezone_text=settings.timezone, now_utc=now
            )
            resolved = resolve_query(
                parsed, timezone_text=settings.timezone, now_utc=now,
                retention_days=self.settings.message_retention_days,
            )
        except SummaryQueryError as exc:
            await self._record_user_summary_failure(callback.from_user.id, chat_id, request.prompt, settings.timezone)
            await callback.edit_message_text(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("User summary query parsing failed: %s", exc)
            await self._record_user_summary_failure(
                callback.from_user.id,
                chat_id,
                request.prompt,
                settings.timezone,
            )
            await callback.edit_message_text("產生摘要失敗，請稍後再試。")
            return

        day_key = now.astimezone(parse_timezone(settings.timezone)).date().isoformat()
        limit = None if callback.from_user.id == self.settings.owner_telegram_user_id else self.settings.daily_user_summary_limit
        request_id = await self.db.reserve_user_summary(
            user_id=callback.from_user.id, chat_id=chat_id, prompt=request.prompt, day_key=day_key,
            created_at_utc=to_iso(now), limit=limit,
        )
        if request_id is None:
            await callback.edit_message_text(
                f"你在此群組今天的摘要額度（{limit} 次）已用完，將依群組時區 {settings.timezone} 於午夜重置。"
            )
            return
        await callback.edit_message_text("開始整理摘要，請稍候...")
        from_iso = to_iso(resolved.start_utc) if resolved.start_utc else None
        to_iso_override = to_iso(resolved.end_utc) if resolved.end_utc else to_iso(now)
        try:
            total_count, rows = await self.db.get_messages_for_summary(
                chat_id=chat_id, from_utc_iso=from_iso, to_utc_iso=to_iso_override,
                limit=self.settings.max_messages_per_summary,
            )
            if not rows:
                await self.db.set_user_summary_status(request_id, "failed")
                await callback.edit_message_text("在這個範圍內找不到可摘要的文字訊息。")
                return
            title, username = await self._resolve_chat_metadata(chat_id)
            full_text = await self._render_summary(
                chat_id=chat_id, chat_title=title, chat_username=username, settings=settings,
                rows=rows, total_count=total_count, triggered_by="user", topic=resolved.topic,
            )
            if not full_text:
                await self.db.set_user_summary_status(request_id, "failed")
                return
            if not await self._user_summary_access_allowed(chat_id, callback.from_user.id):
                await self.db.set_user_summary_status(request_id, "failed")
                await callback.edit_message_text(
                    "無法再次確認你仍在這個已授權群組，因此未傳送摘要。"
                )
                return
            await self.application.bot.send_message(
                chat_id=callback.from_user.id, text=full_text, parse_mode="HTML",
                link_preview_options=self._disabled_link_preview(),
            )
        except TimedOut:
            logger.warning("User summary delivery timed out; retaining pending request %s", request_id)
            await callback.edit_message_text(
                "摘要傳送結果尚未確認，為避免重複傳送，請稍後再查看額度。"
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("User summary failed (request_id=%s): %s", request_id, exc)
            await self.db.set_user_summary_status(request_id, "failed")
            await callback.edit_message_text("產生摘要失敗，請稍後再試。")
            return
        await self.db.set_user_summary_status(request_id, "succeeded")

    async def _user_summary_access_allowed(self, chat_id: int, user_id: int) -> bool:
        return bool(
            await self.db.is_chat_authorized(chat_id)
            and await self._check_active_membership(chat_id, user_id)
        )

    async def _record_user_summary_failure(
        self, user_id: int, chat_id: int, prompt: str, timezone_text: str
    ) -> None:
        now = utc_now()
        day_key = now.astimezone(parse_timezone(timezone_text)).date().isoformat()
        await self.db.record_failed_user_summary(
            user_id=user_id, chat_id=chat_id, prompt=prompt, day_key=day_key, created_at_utc=to_iso(now)
        )

    async def user_summary_history(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update):
            return
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return
        if chat.type != ChatType.PRIVATE:
            await message.reply_text("請私訊機器人使用 /user_summary_history。")
            return
        history = await self.db.get_user_summary_history(
            excluded_user_id=self.settings.owner_telegram_user_id
        )
        if not history:
            await message.reply_text("目前沒有一般使用者的私訊摘要紀錄。")
            return
        chunks: list[str] = []
        current_chunk = ""
        for item in history:
            title, _ = await self._resolve_chat_metadata(item.chat_id)
            entry = (
                f"使用者：{item.user_id}\n群組：{title}\n條件：{item.prompt}\n"
                f"時間（UTC）：{item.created_at_utc}\n狀態：{item.status}"
            )
            if current_chunk and len(current_chunk) + len(entry) + 2 > 4000:
                chunks.append(current_chunk)
                current_chunk = entry
            else:
                current_chunk = f"{current_chunk}\n\n{entry}".strip()
        if current_chunk:
            chunks.append(current_chunk)

        for chunk in chunks:
            await message.reply_text(chunk)

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
        now = utc_now()
        cutoff = now - timedelta(days=self.settings.message_retention_days)
        await self.db.purge_old_messages(to_iso(cutoff))
        await self.db.purge_old_user_summary_requests(to_iso(cutoff))
        self.user_summary_requests = {
            user_id: request
            for user_id, request in self.user_summary_requests.items()
            if request.expires_at >= now
        }

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
