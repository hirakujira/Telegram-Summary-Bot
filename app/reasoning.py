from __future__ import annotations


REASONING_EFFORTS = frozenset(
    {"default", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def normalize_reasoning_effort(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in REASONING_EFFORTS:
        choices = "|".join(sorted(REASONING_EFFORTS))
        raise ValueError(f"reasoning effort 必須是以下其中之一：{choices}")
    return normalized


def reasoning_request_kwargs(reasoning_effort: str) -> dict:
    effort = normalize_reasoning_effort(reasoning_effort)
    if effort == "default":
        return {}
    return {"reasoning": {"effort": effort}}


def is_reasoning_unsupported_error(exc: Exception) -> bool:
    details = " ".join(
        str(value)
        for value in (
            exc,
            getattr(exc, "param", None),
            getattr(exc, "body", None),
        )
        if value is not None
    ).lower()
    return "reasoning" in details or "reasoning_effort" in details


async def call_with_reasoning_fallback(
    *,
    create,
    request_kwargs: dict,
    reasoning_kwargs: dict,
) -> tuple[object, Exception | None]:
    reasoning_request = dict(request_kwargs)
    reasoning_request.update(reasoning_kwargs)

    try:
        return await create(**reasoning_request), None
    except Exception as exc:  # noqa: BLE001
        if not is_reasoning_unsupported_error(exc):
            raise
        return await create(**request_kwargs), exc
