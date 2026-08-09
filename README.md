# Telegram 群組定期摘要機器人 (Python)

這個機器人會在群組中持續收集文字訊息，並支援：

- 自動定期發佈群組摘要
- 手動觸發摘要 (`/summary`)
- 擁有者專屬預覽 (`/preview`)：結果只私訊擁有者，不發佈到群組、不影響排程進度
- 排除機器人、貼圖、圖片、影音類型訊息
- 三種摘要風格：`normal` / `funny` / `roast`
- 只允許指定擁有者調整排程與參數
- 僅處理 owner 明確授權的群組，防止被加入未知群組後收集資料
- 使用者可私訊訂閱自己所在群組的排程摘要
- SQLite 儲存訊息與摘要進度
- 預設使用 `gpt-5.6-luna`，統一透過 Responses API 產生摘要
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
- `DEFAULT_MODEL`: 新群組的預設模型（預設 `gpt-5.6-luna`）
- `DEFAULT_REASONING_EFFORT`: 預設 reasoning 程度（預設 `default`，沿用模型預設）
- `MIN_MESSAGES_TO_SUMMARY`: 自動摘要最低訊息門檻（預設 `8`）
- `MAX_SUMMARY_GAP_HOURS`: 若未達門檻，最多累積幾小時後仍會強制摘要（預設 `24`）
- `PREVIEW_WINDOW_HOURS`: `/preview` 回推的時間範圍（預設 `24`）
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

安全性限制：請由 `OWNER_TELEGRAM_USER_ID` 對應的帳號親自將 bot 加入群組。若由其他帳號加入，bot 會私訊通知 owner 並立刻退出，不會收集群組訊息。

升級至這個版本後，既有群組會暫停授權但保留資料。請由 owner 在要繼續使用的群組內執行 `/authorize_group`，或由 owner 將 bot 移除後重新加入。

## 4. 指令

- `/start` 或 `/help`: 顯示說明
- `/summary`: 立即產生一次摘要（擁有者限定）
- `/preview`: 在群組內輸入，預覽過去 `PREVIEW_WINDOW_HOURS` 小時的摘要，結果只私訊擁有者（擁有者限定）
- `/status`: 查看當前群組設定
- `/set_schedule <cron>`: 設定 cron 排程（擁有者限定）
- `/set_timezone <tz>`: 設定時區（擁有者限定）
- `/set_model <model>`: 設定模型（擁有者限定）
- `/set_reasoning <default|none|minimal|low|medium|high|xhigh|max>`: 設定 reasoning 程度（擁有者限定）
- `/set_style <normal|funny|roast>`: 設定摘要風格（擁有者限定）
- `/set_auto <on|off>`: 開關自動摘要（擁有者限定）
- `/authorize_group`: 授權目前群組（擁有者限定，用於既有群組）
- `/subscribe`: 私訊 bot 後，從可存取的群組清單選擇要訂閱的排程摘要
- `/unsubscribe`: 私訊 bot 後，選擇要取消的摘要訂閱

## 5. 私訊摘要訂閱

使用者先私訊 bot 一次，再執行 `/subscribe`，bot 只會顯示該使用者目前仍在其中的已授權群組。

為了可靠驗證其他使用者是否仍在群組，bot 必須是每個可訂閱群組的管理員。若 Telegram 無法確認成員資格，bot 會採取 fail-closed 策略：不顯示群組、不建立訂閱，也不寄送摘要。

只有群組的排程自動摘要會同步私訊訂閱者；手動 `/summary` 與帶條件的臨時摘要不會通知。通知直接重用已產生的 Telegram HTML 摘要，不會產生額外的 OpenAI API 或 token 用量。每次寄送前都會再次檢查訂閱者仍是群組成員；已離開群組者會自動取消訂閱。

## 6. 排程格式

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

## 7. 摘要風格

用 `/set_style <normal|funny|roast>` 逐群組設定，預設 `normal`。

| 風格 | 語氣 | 取材 |
| --- | --- | --- |
| `normal` | 中性、精簡、好讀 | 依資訊價值排序，省略寒暄與閒聊 |
| `funny` | 活潑有梗 | 重點仍完整，笑點不取笑個人 |
| `roast` | 台式垃圾話、毒舌開嗆 | 娛樂優先，只涵蓋最關鍵幾件事，閒聊與跳針就是素材 |

風格只決定 persona、語氣與取材取向。輸出格式、「回到討論」連結白名單、不杜撰事實與 prompt injection 防護是三種風格共用的硬契約（`app/llm.py` 的 `OUTPUT_CONTRACT`），風格不能覆寫。

`roast` 是群組自願開啟的娛樂模式，會直接開嗆並使用粗俗口語，只保留兩條底線：不能編造沒發生過的事，以及不針對種族、性別、性向、宗教或身心障礙等身分特徵攻擊（後者也會導致模型拒答，讓整份摘要失敗）。要調整尺度改 `app/llm.py` 的 `STYLE_PROMPTS["roast"]` 即可，不需動 `OUTPUT_CONTRACT`。

## 8. OpenAI Responses API

所有摘要都使用 Responses API，不再提供 Chat Completions 或舊版 GPT-4 呼叫路徑。

`OPENAI_MAX_OUTPUT_TOKENS` 會套用為 Responses API 的 `max_output_tokens`。

Reasoning 設定會傳成 Responses API 的 `reasoning.effort`。若模型或指定程度不支援，Bot 會移除 reasoning 設定並以模型預設值重試；`default` 則永遠不傳 reasoning 參數。

## 9. 資料儲存

SQLite 預設路徑：`/app/data/bot.db`（映射到本機 `./data/bot.db`）。

機器人會保留近 180 天原始訊息供摘要使用。

顯示名稱存在 `users` table（`user_id` 為主鍵），`messages` 只存 `user_id`，因此成員改名後所有歷史訊息都會顯示新名字，不保留舊名字。

`messages.reply_to_message_id` 記錄該則訊息回覆的目標，讓摘要能分辨群組中同時進行的多個話題；被回覆的訊息不在本次摘要範圍或未被收集時，仍會保留該關聯。

舊版資料庫（`messages` 內含 `user_name`）會在啟動時自動 migration：訊息內容與筆數完整保留，每位成員取最近一次出現的名稱，既有訊息的回覆欄位為空，之後才開始累積。

摘要中的「回到討論」支援公開群組與超級群組；Telegram 不提供一般私人群組的訊息永久連結。

## 10. 訊息過少時的策略

- 手動 `/summary`：只要有訊息就會摘要（不套用最低門檻）
- 自動摘要：若訊息數 `< MIN_MESSAGES_TO_SUMMARY`，先不發佈，繼續累積
- 但若最早一則「尚未被摘要的訊息」已累積超過 `MAX_SUMMARY_GAP_HOURS`，仍會發佈摘要，避免長時間沒更新
