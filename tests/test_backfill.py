from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.backfill import (
    BackfillResult,
    backfill_messages,
    display_name,
    parse_utc_datetime,
    reply_target,
    run,
)


UTC = timezone.utc
CHAT_ID = -1001234567890


class FakeClient:
    def __init__(self, messages: list[object]):
        self.messages = messages
        self.reverse = None

    async def iter_messages(self, entity, *, reverse):
        self.reverse = reverse
        for message in self.messages:
            yield message


class FakeDatabase:
    def __init__(self):
        self.saved = []
        self.message_keys = set()
        self.commits = 0

    async def save_text_message(self, **kwargs):
        key = (kwargs["chat_id"], kwargs["message_id"])
        if key in self.message_keys:
            return False
        self.message_keys.add(key)
        self.saved.append(kwargs)
        return True

    async def commit(self):
        self.commits += 1


class FakeRunDatabase:
    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def is_chat_authorized(self, chat_id):
        self.authorized_chat_id = chat_id
        return True


class FakeTelegramClient:
    instance = None

    def __init__(self, session, api_id, api_hash, *, flood_sleep_threshold):
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self.flood_sleep_threshold = flood_sleep_threshold
        self.started = False
        FakeTelegramClient.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def start(self):
        self.started = True

    async def get_entity(self, chat):
        self.chat = chat
        return object()


def message(message_id, date, text, sender=None, reply_to=None):
    return SimpleNamespace(
        id=message_id,
        date=date,
        message=text,
        sender=sender,
        reply_to=reply_to,
    )


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_eligible_messages_in_chronological_order(self):
        sender = SimpleNamespace(id=7, first_name="Ada", last_name="Lovelace", username="ada")
        client = FakeClient(
            [
                message(1, datetime(2024, 1, 1, tzinfo=UTC), "before", sender),
                message(2, datetime(2024, 1, 2, tzinfo=UTC), " root ", sender),
                message(
                    3,
                    datetime(2024, 1, 3, tzinfo=UTC),
                    "reply",
                    sender,
                    SimpleNamespace(reply_to_msg_id=2, reply_to_peer_id=None),
                ),
                message(4, datetime(2024, 1, 4, tzinfo=UTC), "after", sender),
            ]
        )
        database = FakeDatabase()

        result = await backfill_messages(
            client,
            database,
            object(),
            CHAT_ID,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 4, tzinfo=UTC),
        )

        self.assertTrue(client.reverse)
        self.assertEqual([row["message_id"] for row in database.saved], [2, 3])
        self.assertEqual(database.saved[0]["user_name"], "Ada Lovelace (@ada)")
        self.assertEqual(database.saved[1]["reply_to_message_id"], 2)
        self.assertEqual(result.saved, 2)
        self.assertEqual(database.commits, 1)

    async def test_skips_empty_or_senderless_messages_and_cross_chat_replies(self):
        sender = SimpleNamespace(id=7, first_name="Ada", last_name=None, username=None)
        client = FakeClient(
            [
                message(1, datetime(2024, 1, 2, tzinfo=UTC), "", sender),
                message(2, datetime(2024, 1, 2, tzinfo=UTC), "missing sender"),
                message(
                    3,
                    datetime(2024, 1, 2, tzinfo=UTC),
                    "other chat",
                    sender,
                    SimpleNamespace(
                        reply_to_msg_id=99,
                        reply_to_peer_id=SimpleNamespace(channel_id=999),
                    ),
                ),
            ]
        )
        database = FakeDatabase()

        result = await backfill_messages(
            client,
            database,
            object(),
            CHAT_ID,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
        )

        self.assertEqual(result.skipped, 2)
        self.assertEqual(len(database.saved), 1)
        self.assertIsNone(database.saved[0]["reply_to_message_id"])

    async def test_existing_messages_are_not_counted_as_saved(self):
        sender = SimpleNamespace(id=7, first_name="Ada", last_name=None, username=None)
        client = FakeClient([message(1, datetime(2024, 1, 2, tzinfo=UTC), "already saved", sender)])
        database = FakeDatabase()

        await backfill_messages(
            client,
            database,
            object(),
            CHAT_ID,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
        )
        result = await backfill_messages(
            client,
            database,
            object(),
            CHAT_ID,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
        )

        self.assertEqual(result.saved, 0)
        self.assertEqual(result.skipped, 1)

    def test_utc_parsing_and_display_name(self):
        self.assertEqual(
            parse_utc_datetime("2024-01-01T08:00:00+08:00"),
            datetime(2024, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(display_name(SimpleNamespace(first_name="Ada", last_name=None, username="ada")), "Ada (@ada)")

    def test_reply_target_rejects_a_different_chat(self):
        reply = SimpleNamespace(
            reply_to_msg_id=1,
            reply_to_peer_id=SimpleNamespace(channel_id=999),
        )
        self.assertIsNone(reply_target(SimpleNamespace(reply_to=reply), CHAT_ID))

    async def test_run_starts_the_client_for_interactive_session_login(self):
        database = FakeRunDatabase()
        args = SimpleNamespace(
            from_utc=datetime(2024, 1, 1, tzinfo=UTC),
            to_utc=datetime(2024, 1, 2, tzinfo=UTC),
            api_id="12345",
            api_hash="api-hash",
            chat="@group",
        )
        with TemporaryDirectory() as temp_dir:
            args.session = str(Path(temp_dir) / "account")
            args.database = str(Path(temp_dir) / "bot.db")
            with (
                patch("app.backfill.Database", return_value=database),
                patch("telethon.TelegramClient", FakeTelegramClient),
                patch("app.backfill.get_group_chat_id", return_value=CHAT_ID),
                patch(
                    "app.backfill.backfill_messages",
                    new=AsyncMock(return_value=BackfillResult(saved=3)),
                ),
            ):
                result = await run(args)

        self.assertEqual(result.saved, 3)
        self.assertTrue(FakeTelegramClient.instance.started)
        self.assertEqual(FakeTelegramClient.instance.session, args.session)
        self.assertEqual(FakeTelegramClient.instance.flood_sleep_threshold, 300)
        self.assertEqual(database.authorized_chat_id, CHAT_ID)
        self.assertTrue(database.connected)
        self.assertTrue(database.closed)


if __name__ == "__main__":
    unittest.main()
