from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.time_utils import parse_timezone


class SummaryQueryError(Exception):
    """Raised when a natural-language /summary request cannot be honoured."""


# Strict JSON schema for the Responses API structured output. The model only
# performs judgement (interpreting the request); all date math and validation
# stay in deterministic code below.
QUERY_JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "has_time_range": {"type": "boolean"},
        "start_local": {"type": "string"},
        "end_local": {"type": "string"},
        "topic": {"type": "string"},
    },
    "required": ["has_time_range", "start_local", "end_local", "topic"],
}

_WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")


@dataclass(slots=True)
class ParsedQuery:
    """Raw fields returned by the model before deterministic validation."""

    has_time_range: bool
    start_local: str
    end_local: str
    topic: str


@dataclass(slots=True)
class SummaryQuery:
    """Validated query: UTC bounds, focus topic, and a human-readable label."""

    start_utc: datetime | None
    end_utc: datetime | None
    topic: str | None
    range_label: str


def build_parser_instructions(*, now_local: datetime, timezone_text: str) -> str:
    weekday = _WEEKDAYS[now_local.weekday()]
    return (
        "你是聊天摘要機器人的查詢解析器。使用者會用自然語言描述他想摘要的範圍，"
        "你要從中抽取「時間範圍」與「主題」。\n\n"
        f"目前當地時間：{now_local:%Y-%m-%d %H:%M}（時區 {timezone_text}，星期{weekday}）。"
        "所有相對時間都以這個時間為基準計算。\n\n"
        "輸出規則：\n"
        "- has_time_range：使用者有提到任何時間範圍時為 true，否則為 false。\n"
        "- start_local / end_local：當地時間，格式必須是「YYYY-MM-DD HH:MM」。\n"
        "  - 「這兩週以來」「最近三天」「過去 24 小時」這類從過去延續到現在的說法："
        "start_local 設為往前推算的起點，end_local 設為目前當地時間。\n"
        "  - 有明確結束點（例如「上週」「昨天」「上個月」）時，start_local 與 end_local 都要填。\n"
        "  - has_time_range 為 false 時，start_local 與 end_local 都填空字串。\n"
        "- topic：使用者想聚焦的主題或關鍵字，去掉時間字眼，只保留核心內容；"
        "沒有指定特定主題時填空字串。\n"
        "- 只輸出符合 schema 的 JSON，不要加任何解釋。"
    )


def resolve_query(
    parsed: ParsedQuery,
    *,
    timezone_text: str,
    now_utc: datetime,
    retention_days: int,
) -> SummaryQuery:
    tz = parse_timezone(timezone_text)
    topic = parsed.topic.strip() or None

    has_range = parsed.has_time_range and (parsed.start_local or parsed.end_local)
    if not has_range:
        return SummaryQuery(
            start_utc=None,
            end_utc=None,
            topic=topic,
            range_label="沿用上次摘要後的訊息",
        )

    now_local = now_utc.astimezone(tz)
    start_local = _parse_local(parsed.start_local, tz) if parsed.start_local else None
    end_local = _parse_local(parsed.end_local, tz) if parsed.end_local else now_local

    start_utc = start_local.astimezone(timezone.utc) if start_local else None
    end_utc = end_local.astimezone(timezone.utc)

    if start_utc and start_utc >= end_utc:
        raise SummaryQueryError(
            "時間範圍看起來顛倒了，請換個說法（例如「最近三天討論到的事情」）。"
        )

    retention_floor = now_utc - timedelta(days=retention_days)
    if end_utc < retention_floor:
        raise SummaryQueryError(
            f"資料只保留最近 {retention_days} 天，這段期間的訊息已經清掉了。"
        )

    return SummaryQuery(
        start_utc=start_utc,
        end_utc=end_utc,
        topic=topic,
        range_label=_format_range_label(start_local, end_local),
    )


def _parse_local(value: str, tz) -> datetime:
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SummaryQueryError(
            "我看不懂這個時間範圍，請換個說法（例如「最近三天」）。"
        ) from exc
    if parsed.tzinfo is not None:
        return parsed.astimezone(tz)
    return parsed.replace(tzinfo=tz)


def _format_range_label(start_local: datetime | None, end_local: datetime) -> str:
    end_text = f"{end_local:%Y-%m-%d %H:%M}"
    if start_local is None:
        return f"到 {end_text} 為止"
    return f"{start_local:%Y-%m-%d %H:%M} ~ {end_text}"
