"""Poll/forwarded bodies must fall through to ordinary native Discord admission."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import socket

import discord  # Real SDK: tests run per-file, before any fake SDK fixtures.
import pytest

from discord_escalation_protocol import (
    build_busy_notice, build_escalation_anchor, is_trusted_nonconversational_message,
)
from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter

SENDER, PEER, CHANNEL = 42, 99, 777


def user(uid):
    return {"id": str(uid), "username": "Fixture", "discriminator": "0", "avatar": None, "bot": True}


def envelope(subtype, body):
    payload = build_busy_notice("notice") if subtype == "busy" else build_escalation_anchor("navigation")
    raw = {
        "id": "123456789012345678", "channel_id": str(CHANNEL), "author": user(SENDER),
        "type": 0, "attachments": [], "components": [], "mentions": [],
        "mention_roles": [], "edited_timestamp": None, "pinned": False, "tts": False,
        **payload,
    }
    if body == "poll":
        raw[body] = {
            "question": {"text": "Which action should we take?"},
            "answers": [{"answer_id": 1, "poll_media": {"text": "Review"}},
                        {"answer_id": 2, "poll_media": {"text": "Retry"}}],
            "expiry": "2026-09-05T20:00:00+00:00", "allow_multiselect": False, "layout_type": 1,
        }
    elif body == "message_snapshots":
        raw[body] = [{"message": {
            "type": 0, "content": "Forwarded substantive instructions", "embeds": [],
            "attachments": [], "timestamp": "2026-09-04T20:00:00+00:00",
            "edited_timestamp": None, "flags": 0,
        }}]
    elif body == "empty":
        raw.update(poll=None, message_snapshots=[])
    return raw


@pytest.mark.parametrize("subtype", ["busy", "anchor"])
@pytest.mark.parametrize("body", [None, "empty", "poll", "message_snapshots"])
def test_dict_extra_body_classification(subtype, body):
    assert is_trusted_nonconversational_message(envelope(subtype, body), {str(SENDER)}) is (
        body in (None, "empty")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["busy", "anchor"])
@pytest.mark.parametrize("body", [None, "poll", "message_snapshots"])
@pytest.mark.parametrize("authorized", [True, False])
async def test_native_extra_body_uses_normal_authorization(monkeypatch, subtype, body, authorized):
    def deny_network(*args, **kwargs):
        raise AssertionError("external IO denied")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    for key, value in {
        "DISCORD_ALLOW_BOTS": "all" if authorized else "none",
        "DISCORD_REQUIRE_MENTION": "false", "DISCORD_AUTO_THREAD": "false",
        "DISCORD_ALLOW_ALL_USERS": "true", "DISCORD_IGNORE_NO_MENTION": "false",
    }.items():
        monkeypatch.setenv(key, value)
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection
    state.user = discord.ClientUser(state=state, data=user(PEER))
    guild = discord.Guild(state=state, data={"id": "1", "name": "Fixture", "roles": [], "emojis": [], "stickers": []})
    channel = discord.TextChannel(state=state, guild=guild, data={
        "id": str(CHANNEL), "name": "fixture", "type": 0, "position": 0,
    })
    msg = discord.Message(state=state, channel=channel, data=envelope(subtype, body))
    if body:
        assert getattr(msg, body)  # Prove the SDK actually retained the body.
    adapter = DiscordAdapter(PlatformConfig(enabled=True, extra={"nonconversational_sender_ids": [SENDER]}))
    adapter._client = SimpleNamespace(user=state.user)
    adapter._ready_event.set()
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    adapter._record_discord_message_seen = MagicMock()

    expected = authorized and body is not None
    assert await adapter._dispatch_discord_message(msg) is expected
    assert is_trusted_nonconversational_message(msg, {str(SENDER)}) is (body is None)
    if expected:
        adapter.handle_message.assert_awaited_once()
        if body == "message_snapshots":
            assert "Forwarded substantive instructions" in adapter.handle_message.await_args.args[0].text
    else:
        adapter.handle_message.assert_not_awaited()
    if body is None:
        assert not adapter._dedup.contains(str(msg.id))
        adapter._record_discord_message_seen.assert_not_called()
