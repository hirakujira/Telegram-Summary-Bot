# Telegram 群組定期摘要機器人 (Python)

這個機器人會在群組中持續收集文字訊息，並支援：

- 自動定期發佈群組摘要
- 手動觸發摘要 (`/summary`)
- 排除機器人、貼圖、圖片、影音類型訊息
- 只允許指定擁有者調整排程與參數
- SQLite 儲存訊息與摘要進度
- OpenAI 模型支援 `gpt-4` / `gpt-5` 系列，可切換 API 呼叫類型
- 訊息量過少時自動延後摘要，避免產生空洞內容
- 每個摘要主題附上原始訊息連結，可直接回到相關討論點

## 1. 準備設定

複製環境變數範本：

```bash
cp .env.example .env
```

編輯 `.env`：

- `TELEGRAM_BOT_TOKEN`: BotFather 建立 bot 後取得
- `OPENAI_API_KEY`: OpenAI API 金鑰
- `OWNER_TELEGRAM_USER_ID`: 你的 Telegram user id（只有這個 id 可改設定）
- `MIN_MESSAGES_TO_SUMMARY`: 自動摘要最低訊息門檻（預設 `8`）
- `MAX_SUMMARY_GAP_HOURS`: 若未達門檻，最多累積幾小時後仍會強制摘要（預設 `24`）
- `OPENAI_MAX_OUTPUT_TOKENS`: 單次摘要請求的輸出 token 上限（預設 `1800`，可用來控管預算），使用 reasonable 模型的時候因為推理會佔用 token，所以需要設大一點

## 2. 啟動

```bash
docker compose up -d --build
```

## 3. BotFather 必做設定

為了讓 bot 能看到群組成員訊息內容：

1. 把 bot 加入目標群組
2. 在 BotFather 對該 bot 執行 `/setprivacy`，選擇 `Disable`

否則 bot 可能只會收到指令，無法做完整摘要。

## 4. 指令

- `/start` 或 `/help`: 顯示說明
- `/summary`: 立即產生一次摘要（擁有者限定）
- `/status`: 查看當前群組設定
- `/set_schedule <cron>`: 設定 cron 排程（擁有者限定）
- `/set_timezone <tz>`: 設定時區（擁有者限定）
- `/set_model <model>`: 設定模型（擁有者限定）
- `/set_api_style <auto|responses|chat>`: 設定 API 風格（擁有者限定）
- `/set_auto <on|off>`: 開關自動摘要（擁有者限定）

## 5. 排程格式

`/set_schedule` 使用標準 5 欄 cron：

```text
分鐘 小時 日 月 星期
```

例如：

- `0 9 * * *` 每天 09:00
- `0 */6 * * *` 每 6 小時
- `30 9 * * 1` 每週一 09:30

時區可設：

- `UTC+8`
- `Asia/Taipei`

## 6. OpenAI API 風格

- `auto`: 模型名稱以 `gpt-5` 開頭時用 Responses API，其餘用 Chat Completions API
- `responses`: 強制用 Responses API
- `chat`: 強制用 Chat Completions API

`OPENAI_MAX_OUTPUT_TOKENS` 會套用在單次摘要請求（Responses 的 `max_output_tokens` / Chat 的 `max_tokens`）。

## 7. 資料儲存

SQLite 預設路徑：`/app/data/bot.db`（映射到本機 `./data/bot.db`）。

機器人會保留近 30 天原始訊息供摘要使用。

摘要中的「回到討論」支援公開群組與超級群組；Telegram 不提供一般私人群組的訊息永久連結。

## 8. 訊息過少時的策略

- 手動 `/summary`：只要有訊息就會摘要（不套用最低門檻）
- 自動摘要：若訊息數 `< MIN_MESSAGES_TO_SUMMARY`，先不發佈，繼續累積
- 但若最早一則「尚未被摘要的訊息」已累積超過 `MAX_SUMMARY_GAP_HOURS`，仍會發佈摘要，避免長時間沒更新
