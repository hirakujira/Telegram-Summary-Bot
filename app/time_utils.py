from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


OFFSET_RE = re.compile(r"^UTC([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)



def utc_now() -> datetime:
    return datetime.now(timezone.utc)



def parse_timezone(tz_text: str):
    tz_text = tz_text.strip()
    match = OFFSET_RE.match(tz_text)
    if match:
        sign, hours_str, mins_str = match.groups()
        hours = int(hours_str)
        minutes = int(mins_str or 0)
        delta = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            delta = -delta
        return timezone(delta)
    return ZoneInfo(tz_text)



def compute_next_run_utc(cron_expr: str, tz_text: str, base_utc: datetime | None = None) -> datetime:
    current_utc = base_utc or utc_now()
    tz = parse_timezone(tz_text)
    current_local = current_utc.astimezone(tz)
    next_local = croniter(cron_expr, current_local).get_next(datetime)
    return next_local.astimezone(timezone.utc)



def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()
