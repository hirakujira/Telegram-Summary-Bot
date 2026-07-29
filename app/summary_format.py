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
        text = escape(bullet.group("text"))
        return f"• <b>{name}</b>：{text}"

    discussion_link = _DISCUSSION_LINK_PATTERN.fullmatch(line)
    if discussion_link and _is_telegram_link(discussion_link.group("url")):
        url = escape(discussion_link.group("url"), quote=True)
        return f'<a href="{url}">💬 回到討論</a>'

    return escape(line.replace("**", ""))


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
        source = f"[討論連結: {message_link}]" if message_link else ""
        lines.append(
            f"[{row['created_at_utc']}]{source} {row['user_name']}: {row['text']}"
        )

    return "\n".join(lines)
