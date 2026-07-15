from __future__ import annotations


def build_message_link(*, chat_id: int, message_id: int, chat_username: str | None) -> str | None:
    if chat_username:
        return f"https://t.me/{chat_username.lstrip('@')}/{message_id}"

    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text[4:]}/{message_id}"

    # Telegram only supports message permalinks for channels and supergroups.
    return None


def build_transcript(
    *,
    rows: list,
    total_count: int,
    chat_title: str,
    summary_range: str,
    chat_id: int,
    chat_username: str | None,
) -> str:
    lines = []
    lines.append("[摘要中繼資料]")
    lines.append(f"群組名稱: {chat_title}")
    lines.append(f"摘要區間: {summary_range}")
    lines.append(f"訊息總數: {total_count}")
    lines.append(f"提供給模型的訊息數: {len(rows)}")
    lines.append("")

    if total_count > len(rows):
        lines.append(
            f"注意：本次共 {total_count} 則訊息，為控制長度僅摘要最後 {len(rows)} 則。"
        )
        lines.append("")

    lines.append("[對話紀錄]")
    for row in rows:
        message_link = build_message_link(
            chat_id=chat_id,
            message_id=row["message_id"],
            chat_username=chat_username,
        )
        source = f"[訊息 ID: {row['message_id']}]"
        if message_link:
            source += f"[討論連結: {message_link}]"
        lines.append(
            f"[{row['created_at_utc']}]{source} {row['user_name']}: {row['text']}"
        )

    return "\n".join(lines)
