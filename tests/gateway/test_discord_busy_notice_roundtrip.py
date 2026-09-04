"""Real busy-handler -> inherited retry -> Discord wire -> peer receiver path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, build_session_key
from gateway.session import SessionSource
from tests.gateway.test_busy_session_ack import _make_runner
from tests.gateway.test_discord_escalation_anchor import load_adapter
from tests.gateway.test_discord_free_response import (
    adapter, busy_wire,  # noqa: F401 — pytest fixtures
    FakeHistoryChannel, FakeThread, make_message,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interrupt", "redirect", "queue", "steer"])
@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("peer_active", [False, True])
async def test_busy_ack_wire_roundtrip(tmp_path, monkeypatch, busy_wire, mode, human, peer_active):
    import gateway.run as gateway_run
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOW_BOTS", "all")
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "false")
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "true")
    monkeypatch.setenv("DISCORD_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true")
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED", "true")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr("agent.onboarding.is_seen", lambda *_: False)
    mark_seen = MagicMock()
    monkeypatch.setattr("agent.onboarding.mark_seen", mark_seen)
    sender = load_adapter(tmp_path, monkeypatch, "sender", {"discord": {
        "nonconversational_sender_ids": [999], "nonconversational_wire_channels": [777],
    }})
    peer = load_adapter(tmp_path, monkeypatch, "peer", {"discord": {
        "nonconversational_sender_ids": [999],
    }})
    transport, _ = busy_wire
    sender._client = SimpleNamespace(user=SimpleNamespace(id=999, bot=True), get_channel=lambda _: transport)
    sender._last_self_message_id = {"777": "100"}
    peer._client = SimpleNamespace(user=SimpleNamespace(id=888, bot=True))
    peer._ready_event.set()
    peer._text_batch_delay_seconds = 0
    peer_runner, _ = _make_runner()
    peer_runner.adapters[Platform.DISCORD] = peer
    peer_runner._busy_input_mode = "steer"
    peer._client.get_channel = lambda _: transport
    async def receive_event(event):
        peer_key = build_session_key(event.source)
        if peer_active:
            peer_runner._running_agents[peer_key] = MagicMock()
        return await peer_runner._handle_active_session_busy_message(event, peer_key)
    peer.handle_message = AsyncMock(side_effect=receive_event)
    peer._record_discord_message_seen = MagicMock()

    source = SessionSource(platform=Platform.DISCORD, chat_id="700", thread_id="777", chat_type="thread", user_id="42" if human else "888")
    event = MessageEvent(text="Please check the failing gate", source=source, message_type=MessageType.TEXT, message_id="50")
    runner, _ = _make_runner()
    runner._busy_input_mode = "interrupt" if mode == "redirect" else mode
    runner.adapters[Platform.DISCORD] = sender
    agent = MagicMock()
    agent._supports_active_turn_redirect = mode == "redirect"
    agent.redirect.return_value = True
    agent.steer.return_value = True
    agent.get_activity_summary.return_value = {"api_call_count": 1, "max_iterations": 60}
    key = build_session_key(source)
    runner._running_agents[key] = agent
    assert await runner._handle_active_session_busy_message(event, key)
    assert transport.send.await_count > 0
    mark_seen.assert_called_once()
    if mode == "redirect":
        agent.redirect.assert_called_once_with(event.text)
    elif mode == "steer":
        agent.steer.assert_called_once_with(event.text)
    elif mode == "interrupt":
        agent.interrupt.assert_called_once_with(event.text)
    else:
        assert sender._pending_messages[key].text == event.text
        agent.interrupt.assert_not_called()

    channel = FakeHistoryChannel([], channel_id=777)
    channel.guild.id = 1
    thread = FakeThread(channel_id=778, parent=channel)
    channel.create_thread = AsyncMock(return_value=thread)
    notices = []
    for index, call in enumerate(transport.send.await_args_list):
        kwargs = call.kwargs
        assert kwargs["reference"].message_id == 50
        # Replay the actual wire shape even if serialization/tagging is mutated away.
        # Assert peer non-activation before structural assertions: catch the real wake.
        notice = make_message(channel=channel, content=kwargs["content"], msg_type=19, mentions=[peer._client.user])
        notice.id = 201 + index
        notice.author = SimpleNamespace(id=999, bot=True, display_name="Sender", name="sender")
        notice.reference = kwargs["reference"]
        notice.embeds = [kwargs["embed"]] if "embed" in kwargs else []
        notices.append(notice)
        assert await peer._dispatch_discord_message(notice) is False
        await peer._handle_message(notice)
        assert not peer._dedup.contains(str(notice.id))
        assert await peer._dispatch_recovered_message(notice) is False
        assert kwargs["content"] == ""
        embed = kwargs["embed"].to_dict()
        assert embed["url"] == "https://hermes-agent.nousresearch.com/busy-notice/v1"
        assert {"interrupt": "Interrupting", "redirect": "Redirected", "queue": "Queued", "steer": "Steered"}[mode] in embed["description"]
    peer.handle_message.assert_not_awaited()
    assert transport.send.await_count == len(notices)  # No reciprocal ACK.
    peer._record_discord_message_seen.assert_not_called()
    channel.create_thread.assert_not_awaited()
    assert sender._last_self_message_id == {"777": "100"}

    # Peer and own-author cold history must ignore the ACK before self-boundaries.
    earlier = make_message(channel=channel, content="human before")
    earlier.id = 150
    later = make_message(channel=channel, content="human after")
    later.id = 250
    channel._history_messages = [earlier, *notices, later]
    for history_reader in (peer, sender):
        history_reader._last_self_message_id.clear()
        context = await history_reader._fetch_channel_context(channel, before=SimpleNamespace(id=300))
        assert "human before" in context and "human after" in context
        assert "Interrupting" not in context and "Steered" not in context
    # Original request has no DB/reaction/active-claim completion; own reply is only an ACK.
    original = make_message(channel=channel, content="original unanswered request")
    original.id = 50
    original.reactions = []
    channel._history_messages = notices
    assert await sender._should_backfill_discord_message(original) is True
    assert not sender._discord_message_is_persistently_complete("50")

    # Real AM handoff remains a substantive in-thread event, even with an ACK prefix.
    handoff = make_message(channel=thread, content="⚡ Review the actual evidence", mentions=[])
    handoff.id = 400
    handoff.author = SimpleNamespace(id=999, bot=True, display_name="AM", name="am")
    await peer._handle_message(handoff)
    peer.handle_message.assert_awaited_once()
    assert "Review the actual evidence" in peer.handle_message.await_args.args[0].text
    # Ensure the test never replaced the production send or retry methods.
    assert sender.send.__func__ is DiscordAdapter.send


@pytest.mark.asyncio
async def test_retry_accumulates_initial_veto_even_if_next_result_defaults_on(adapter, monkeypatch):
    from gateway.platforms.base import SendResult
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    initial = SendResult(success=False, error="ConnectionError", allow_formatting_fallback=False)
    permanent = SendResult(success=False, error="Missing Permissions")
    adapter.send = AsyncMock(side_effect=[initial, permanent, SendResult(success=True)])
    result = await adapter._send_with_retry("777", "full original ACK", base_delay=0)
    assert result is permanent
    assert adapter.send.await_count == 2
