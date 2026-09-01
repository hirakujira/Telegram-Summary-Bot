from __future__ import annotations

from croniter import CroniterBadCronError, croniter
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes, ConversationHandler

from app.reasoning import normalize_reasoning_effort
from app.response_style import normalize_response_style
from app.time_utils import parse_timezone, utc_now


class CommandMixin:
    SETTING_VALUE = 1

    async def start(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_authorized_group(update):
            return
        message = update.effective_message
        user = update.effective_user
        if not message:
            return

        if not user or user.id != self.settings.owner_telegram_user_id:
            await message.reply_text(
                "已啟動。\n\n"
                "請私訊機器人使用：\n"
                "/subscribe - 訂閱你所在群組的排程摘要\n"
                "/unsubscribe - 取消摘要訂閱"
                "\n/summary <條件> - 對你所在的授權群組請求摘要"
            )
            return

        await message.reply_text(
            "已啟動。\n"
            "僅擁有者：\n"
            "/summary - 立即產生摘要\n"
            "/summary <條件> - 針對指定時間範圍或主題摘要，例如 /summary 這兩週以來討論到露營的事情\n"
            f"/preview - 私訊預覽過去 {self.settings.preview_window_hours} 小時摘要\n"
            "/status - 查看目前設定\n"
            "/set_schedule <cron> - 設定排程（未帶值可依提示輸入）\n"
            "/set_timezone <tz> - 設定時區（未帶值可依提示輸入）\n"
            "/set_model <model> - 設定模型（未帶值可依提示輸入）\n"
            "/set_reasoning <default|none|minimal|low|medium|high|xhigh|max> - 設定 reasoning（未帶值可依提示輸入）\n"
            "/set_style <normal|funny|roast> - 設定摘要風格（未帶值可依提示輸入）\n"
            "/set_auto <on|off> - 開關自動摘要（未帶值可依提示輸入）\n"
            "/cancel - 取消進行中的設定\n"
            "/user_summary_history - 查看一般用戶私訊摘要紀錄\n"
            "\n"
            "一般用戶（請私訊機器人）：\n"
            "/subscribe - 訂閱你所在群組的排程摘要\n"
            "/unsubscribe - 取消摘要訂閱\n"
            "/summary <條件> - 對你所在的授權群組請求摘要\n"
            "\n"
            "提醒：請在 BotFather 關閉 privacy mode，才能接收群組完整訊息。\n"
            "提醒：/preview 結果只會私訊擁有者，擁有者需先私訊 bot 一次。"
        )

    async def status(self, update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._assert_owner(update) or not await self._assert_authorized_group(update):
            return
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
            f"message_retention_days: {self.settings.message_retention_days}\n"
            f"openai_max_output_tokens: {self.settings.openai_max_output_tokens}\n"
            f"next_run_utc: {settings.next_run_at_utc}\n"
            f"last_summarized_utc: {last or 'NULL'}"
        )
        await message.reply_text(text)

    async def set_schedule(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "schedule")

    async def set_timezone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "timezone")

    async def set_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "model")

    async def set_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "auto")

    async def set_reasoning(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "reasoning")

    async def set_style(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        return await self._begin_setting(update, context, "style")

    async def _begin_setting(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        setting: str,
    ) -> int:
        if not await self._assert_group_setting_access(update):
            return ConversationHandler.END
        message = update.effective_message
        value = " ".join(context.args).strip()
        if value:
            await self._apply_setting(update, setting, value)
            return ConversationHandler.END

        if message:
            await message.reply_text(self._setting_initial_prompt(setting))
        context.chat_data["pending_group_setting"] = setting
        return self.SETTING_VALUE

    async def receive_setting_value(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        if not await self._assert_group_setting_access(update):
            return ConversationHandler.END
        setting = context.chat_data.get("pending_group_setting")
        message = update.effective_message
        if not setting or not message:
            return ConversationHandler.END

        if await self._apply_setting(update, setting, (message.text or "").strip()):
            context.chat_data.pop("pending_group_setting", None)
            return ConversationHandler.END
        return self.SETTING_VALUE

    async def cancel_setting(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        message = update.effective_message
        context.chat_data.pop("pending_group_setting", None)
        if message:
            await message.reply_text("已取消目前的設定。")
        return ConversationHandler.END

    async def cancel_without_setting(
        self, update: Update, _: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        if message:
            await message.reply_text("目前沒有進行中的設定。")

    async def _assert_group_setting_access(self, update: Update) -> bool:
        if not await self._assert_owner(update):
            return False
        chat = update.effective_chat
        message = update.effective_message
        if not chat or chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
            if message:
                await message.reply_text("此指令只能在已授權群組中使用。")
            return False
        return await self._assert_authorized_group(update)

    async def _apply_setting(self, update: Update, setting: str, raw_value: str) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        if not message or not chat:
            return False

        value = raw_value.strip()
        try:
            if setting == "schedule":
                settings = await self.db.get_chat_settings(chat.id)
                croniter(value, utc_now().astimezone(parse_timezone(settings.timezone)))
                updated = await self.db.update_chat_settings(
                    chat.id, cron_expr=value, recompute_next_run=True
                )
                await message.reply_text(
                    f"已更新排程為 {updated.cron_expr}，下一次執行 UTC: {updated.next_run_at_utc}"
                )
            elif setting == "timezone":
                settings = await self.db.get_chat_settings(chat.id)
                croniter(settings.cron_expr, utc_now().astimezone(parse_timezone(value)))
                updated = await self.db.update_chat_settings(
                    chat.id, timezone=value, recompute_next_run=True
                )
                await message.reply_text(
                    f"已更新時區為 `{updated.timezone}`，下一次執行 UTC: {updated.next_run_at_utc}"
                )
            elif setting == "model":
                if not value:
                    raise ValueError
                updated = await self.db.update_chat_settings(chat.id, model=value)
                await message.reply_text(f"已更新模型為 `{updated.model}`")
            elif setting == "auto":
                if value.lower() not in {"on", "off"}:
                    raise ValueError
                updated = await self.db.update_chat_settings(
                    chat.id, auto_enabled=(value.lower() == "on")
                )
                await message.reply_text(f"自動摘要已設為 {'on' if updated.auto_enabled else 'off'}")
            elif setting == "reasoning":
                updated = await self.db.update_chat_settings(
                    chat.id, reasoning_effort=normalize_reasoning_effort(value)
                )
                await message.reply_text(
                    f"已更新 reasoning_effort 為 `{updated.reasoning_effort}`；"
                    "若目前模型不支援，產生摘要時會自動 fallback 到模型預設。"
                )
            else:
                updated = await self.db.update_chat_settings(
                    chat.id, response_style=normalize_response_style(value)
                )
                await message.reply_text(f"已更新摘要風格為 `{updated.response_style}`")
        except (CroniterBadCronError, ValueError) as exc:
            if setting == "schedule" and value:
                await message.reply_text(f"cron 格式錯誤：{exc}")
            elif setting == "timezone" and value:
                await message.reply_text(f"時區格式錯誤：{exc}")
            else:
                await message.reply_text(self._setting_prompt(setting))
            return False
        return True

    @staticmethod
    def _setting_prompt(setting: str) -> str:
        prompts = {
            "schedule": "用法：/set_schedule <cron>，例如 /set_schedule 0 9 * * *",
            "timezone": "用法：/set_timezone <timezone>，例如 /set_timezone UTC+8 或 /set_timezone Asia/Taipei",
            "model": "用法：/set_model <model_name>",
            "reasoning": "用法：/set_reasoning <default|none|minimal|low|medium|high|xhigh|max>",
            "style": "用法：/set_style <normal|funny|roast>",
            "auto": "用法：/set_auto <on|off>",
        }
        return prompts[setting]

    @staticmethod
    def _setting_initial_prompt(setting: str) -> str:
        prompts = {
            "schedule": "請輸入 cron，例如 0 9 * * *。",
            "timezone": "請輸入時區，例如 UTC+8 或 Asia/Taipei。",
            "model": "請輸入 model 名稱。",
            "reasoning": "請輸入 reasoning 程度。",
            "style": "請輸入摘要風格。",
            "auto": "請輸入自動摘要：on 或 off。",
        }
        return prompts[setting]
