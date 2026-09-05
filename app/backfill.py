from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Protocol

from app.db import Database
from app.time_utils import to_iso


COMMIT_BATCH_SIZE = 500
PROGRESS_INTERVAL = 100


class BackfillError(ValueError):
    pass


class MessageClient(Protocol):
    def iter_messages(
        self,
        entity: object,
        *,
        reverse: bool,
        offset_date: datetime,
    ) -> AsyncIterator[object]: ...


class MessageStore(Protocol):
    async def save_text_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        user_id: int,
        user_name: str,
        text: str,
        created_at_utc: str,
        reply_to_message_id: int | None = None,
        commit: bool = True,
    ) -> bool: ...

    async def commit(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BackfillResult:
    scanned: int = 0
    saved: int = 0
    skipped: int = 0


def parse_utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "dates must be ISO-8601 datetimes with a UTC offset"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("dates must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def get_group_chat_id(entity: object) -> int:
    from telethon.tl.types import Channel, Chat

    if isinstance(entity, Chat):
        return -entity.id
    if isinstance(entity, Channel) and entity.megagroup:
        return -100_000_000_0000 - entity.id
    raise BackfillError("--chat must resolve to a Telegram group or supergroup")


def display_name(sender: object) -> str:
    name = " ".join(
        part
        for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
        if part
    )
    if not name:
        name = getattr(sender, "title", None) or "未知成員"
    username = getattr(sender, "username", None)
    return f"{name} (@{username})" if username else name


async def get_sender(message: object) -> object | None:
    sender = getattr(message, "sender", None)
    if sender is not None:
        return sender
    load_sender = getattr(message, "get_sender", None)
    if load_sender is not None:
        return await load_sender()
    return None


def reply_target(message: object, chat_id: int) -> int | None:
    reply_to = getattr(message, "reply_to", None)
    message_id = getattr(reply_to, "reply_to_msg_id", None)
    if message_id is None:
        return None

    peer = getattr(reply_to, "reply_to_peer_id", None)
    if peer is not None and peer_chat_id(peer) != chat_id:
        return None
    return message_id


def peer_chat_id(peer: object) -> int:
    channel_id = getattr(peer, "channel_id", None)
    if channel_id is not None:
        return -100_000_000_0000 - channel_id
    chat_id = getattr(peer, "chat_id", None)
    if chat_id is not None:
        return -chat_id
    return getattr(peer, "user_id", None)


async def backfill_messages(
    client: MessageClient,
    database: MessageStore,
    entity: object,
    chat_id: int,
    from_utc: datetime,
    to_utc: datetime,
    report_progress: Callable[[BackfillResult, datetime | None], None] | None = None,
) -> BackfillResult:
    result = BackfillResult()
    uncommitted_writes = 0
    # Telethon reverses offset semantics with `reverse=True`. The API's date
    # offset is exclusive, so step back one second to include messages exactly
    # at the requested UTC-second boundary.
    offset_date = from_utc - timedelta(seconds=1)
    async for message in client.iter_messages(
        entity,
        reverse=True,
        offset_date=offset_date,
    ):
        result = BackfillResult(result.scanned + 1, result.saved, result.skipped)
        date = getattr(message, "date", None)
        if report_progress and result.scanned % PROGRESS_INTERVAL == 0:
            report_progress(result, date.astimezone(timezone.utc) if date else None)
        if date is None:
            result = BackfillResult(result.scanned, result.saved, result.skipped + 1)
            continue
        message_date = date.astimezone(timezone.utc)
        if message_date < from_utc:
            continue
        if message_date >= to_utc:
            break

        sender = await get_sender(message)
        text = (getattr(message, "message", None) or "").strip()
        sender_id = getattr(sender, "id", None)
        if sender is None or sender_id is None or not text:
            result = BackfillResult(result.scanned, result.saved, result.skipped + 1)
            continue

        inserted = await database.save_text_message(
            chat_id=chat_id,
            message_id=message.id,
            user_id=sender_id,
            user_name=display_name(sender),
            text=text,
            created_at_utc=to_iso(message_date),
            reply_to_message_id=reply_target(message, chat_id),
            commit=False,
        )
        if inserted:
            result = BackfillResult(result.scanned, result.saved + 1, result.skipped)
            uncommitted_writes += 1
            if uncommitted_writes == COMMIT_BATCH_SIZE:
                await database.commit()
                uncommitted_writes = 0
        else:
            result = BackfillResult(result.scanned, result.saved, result.skipped + 1)
    if uncommitted_writes:
        await database.commit()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill authorized Telegram group history.")
    parser.add_argument("--chat", required=True, help="Group @username, invite-linked entity, or ID")
    parser.add_argument("--from", dest="from_utc", required=True, type=parse_utc_datetime)
    parser.add_argument("--to", dest="to_utc", required=True, type=parse_utc_datetime)
    parser.add_argument("--session", required=True, help="Persistent local Telethon session file path")
    parser.add_argument("--api-id", default=os.getenv("TELEGRAM_API_ID"))
    parser.add_argument("--api-hash", default=os.getenv("TELEGRAM_API_HASH"))
    parser.add_argument("--database", default=os.getenv("SQLITE_PATH", "data/bot.db"))
    return parser


async def run(args: argparse.Namespace) -> BackfillResult:
    from telethon import TelegramClient

    if args.from_utc >= args.to_utc:
        raise BackfillError("--from must be earlier than --to")
    if not args.api_id or not args.api_hash:
        raise BackfillError("provide --api-id/--api-hash or TELEGRAM_API_ID/TELEGRAM_API_HASH")

    session_path = Path(args.session).expanduser()
    if not session_path.parent.exists():
        raise BackfillError("--session parent directory does not exist")

    database = Database(
        args.database,
        default_timezone="UTC+8",
        default_cron_expr="0 9 * * *",
        default_model="gpt-5.6-luna",
        default_reasoning_effort="default",
    )
    await database.connect()
    try:
        async with TelegramClient(
            str(session_path),
            int(args.api_id),
            args.api_hash,
            flood_sleep_threshold=300,
        ) as client:
            await client.start()
            entity = await client.get_entity(args.chat)
            chat_id = get_group_chat_id(entity)
            if not await database.is_chat_authorized(chat_id):
                raise BackfillError("the resolved group is not authorized in this database")

            total_seconds = (args.to_utc - args.from_utc).total_seconds()

            def report_progress(result: BackfillResult, message_date: datetime | None) -> None:
                if message_date is None:
                    progress = 0
                    timestamp = "unknown"
                else:
                    progress = min(
                        100,
                        max(0, (message_date - args.from_utc).total_seconds() / total_seconds * 100),
                    )
                    timestamp = to_iso(message_date)
                print(
                    f"Backfill progress: {progress:.1f}% "
                    f"scanned={result.scanned} saved={result.saved} "
                    f"skipped={result.skipped} at={timestamp}",
                    flush=True,
                )

            print("Backfill started. Progress updates every 100 messages.", flush=True)
            return await backfill_messages(
                client,
                database,
                entity,
                chat_id,
                args.from_utc,
                args.to_utc,
                report_progress,
            )
    finally:
        await database.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        result = asyncio.run(run(args))
    except (BackfillError, ValueError) as exc:
        raise SystemExit(f"backfill failed: {exc}") from exc
    print(f"Backfill complete: scanned={result.scanned} saved={result.saved} skipped={result.skipped}")


if __name__ == "__main__":
    main()
