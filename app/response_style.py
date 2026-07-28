from __future__ import annotations

RESPONSE_STYLES = {"normal", "funny", "roast"}


def normalize_response_style(value: str) -> str:
    style = value.strip().lower()
    if style not in RESPONSE_STYLES:
        raise ValueError(f"Unsupported response style: {value}")
    return style
