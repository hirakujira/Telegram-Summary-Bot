from __future__ import annotations

import re
from html import escape
from urllib.parse import urlsplit


_SUMMARY_HEADER_PATTERN = re.compile(
    r"^(?P<title>.+? Summary) \| (?P<date_range>.+?) \| (?P<count>\d+ 則訊息)$"
)
_TOPIC_PATTERN = re.compile(r"^\d+\.\s+")
_BULLET_PATTERN = re.compile(r"^-\s+\*\*(?P<name>.+?)\*\*：\s*(?P<text>.+)$")
_DISCUSSION_LINK_PATTERN = re.compile(r"^\[💬 回到討論\]\((?P<url>.+)\)$")
_INLINE_BOLD_PATTERN = re.compile(r"\*\*(?P<text>.+?)\*\*")


def format_summary_for_telegram(summary_text: str) -> str:
    lines = summary_text.strip().splitlines()
    if not lines:
        return ""

    output = [_format_summary_header(lines[0].strip())]
    topic_title: str | None = None
    topic_lines: list[str] = []

    def append_topic() -> None:
        if topic_title is None:
            return

        output.extend(["", f"<b>{escape(topic_title.replace('**', ''))}</b>"])
        body = "\n".join(
            _format_topic_line(line.strip())
            for line in topic_lines
            if line.strip()
        )
        if body:
            output.append(f"<blockquote expandable>{body}</blockquote>")

    for line in lines[1:]:
        stripped = line.strip()
        if _TOPIC_PATTERN.match(stripped):
            append_topic()
            topic_title = stripped
            topic_lines = []
        elif topic_title is not None:
            topic_lines.append(stripped)
        elif stripped:
            output.extend(["", escape(stripped)])

    append_topic()
    return "\n".join(output)


def build_preview_header(*, chat_title: str, chat_id: int, window_hours: int) -> str:
    metadata = escape(f"{chat_title} · chat_id {chat_id} · 過去 {window_hours} 小時")
    return f"🔍 <b>預覽（未發佈到群組）</b>\n<code>{metadata}</code>"


def _format_summary_header(line: str) -> str:
    match = _SUMMARY_HEADER_PATTERN.fullmatch(line)
    if not match:
        return f"<b>{escape(line.replace('**', ''))}</b>"

    title = escape(match.group("title"))
    metadata = escape(f"{match.group('date_range')} · {match.group('count')}")
    return f"<b>{title}</b>\n<code>{metadata}</code>"


def _format_topic_line(line: str) -> str:
    bullet = _BULLET_PATTERN.fullmatch(line)
    if bullet:
        name = escape(bullet.group("name"))
        text = _format_inline_bold(bullet.group("text"))
        return f"• <b>{name}</b>：{text}"

    discussion_link = _DISCUSSION_LINK_PATTERN.fullmatch(line)
    if discussion_link and _is_telegram_link(discussion_link.group("url")):
        url = escape(discussion_link.group("url"), quote=True)
        return f'<a href="{url}">💬 回到討論</a>'

    return escape(line.replace("**", ""))


def _format_inline_bold(text: str) -> str:
    escaped = escape(text)
    return _INLINE_BOLD_PATTERN.sub(r"<b>\g<text></b>", escaped)


def _is_telegram_link(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "t.me" and bool(parsed.path)


def build_message_link(*, chat_id: int, message_id: int, chat_username: str | None) -> str | None:
    if chat_username:
        return f"https://t.me/{chat_username.lstrip('@')}/{message_id}"

    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text[4:]}/{message_id}"

    # Telegram only supports message permalinks for channels and supergroups.
    return None


REPLY_LABEL_HINT = (
    "回覆關係說明: 每則訊息開頭的 [m<編號>] 是本次摘要專用的內部代號；"
    "[m<編號> 回覆 <代號>] 表示這則訊息在回覆哪一則，代號 w<編號> 代表被回覆的訊息不在本次範圍內。"
    "群組可能同時進行多個話題，請優先依這些回覆關係判斷哪些訊息屬於同一串討論，"
    "但絕對不要在輸出中提到任何代號。"
)


def _row_value(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
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
    node_labels = {row["message_id"]: f"m{index}" for index, row in enumerate(rows, start=1)}
    outside_labels: dict[int, str] = {}

    def label_for(message_id: int) -> str:
        if message_id in node_labels:
            return node_labels[message_id]
        if message_id not in outside_labels:
            outside_labels[message_id] = f"w{len(outside_labels) + 1}"
        return outside_labels[message_id]

    entries = []
    has_reply = False
    for row in rows:
        message_id = row["message_id"]
        reply_target = _row_value(row, "reply_to_message_id")
        if reply_target and reply_target != message_id:
            marker = f"[{node_labels[message_id]} 回覆 {label_for(reply_target)}]"
            has_reply = True
        else:
            marker = f"[{node_labels[message_id]}]"

        message_link = build_message_link(
            chat_id=chat_id,
            message_id=message_id,
            chat_username=chat_username,
        )
        source = f"[討論連結: {message_link}]" if message_link else ""
        entries.append(
            f"{marker}[{row['created_at_utc']}]{source} {row['user_name']}: {row['text']}"
        )

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

    if has_reply:
        lines.append(REPLY_LABEL_HINT)
        lines.append("")

    lines.append("[對話紀錄]")
    lines.extend(entries)

    return "\n".join(lines)
