"""Completion routing must preserve its recorded owner, including after reconnect."""
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


class Sink:
    supports_async_delivery = True

    def __init__(self, fronts=()):
        self.fronts = fronts
        self.events = []
        self.order = []

    def fronts_platform(self, platform):
        return platform in self.fronts

    def prime_routing_cache(self, event):
        self.order.append('prime')

    async def handle_message(self, event):
        self.order.append('handle')
        self.events.append(event)


def runner_fixture(monkeypatch, tmp_path):
    import gateway.run as gateway_run
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    runner: Any = GatewayRunner(GatewayConfig())
    runner._primary_profile_name = 'operator'
    primary, secondary = Sink(), Sink()
    relay = Sink((Platform.DISCORD, Platform.SLACK))
    runner.adapters = {Platform.DISCORD: primary, Platform.RELAY: relay}
    runner._profile_adapters = {'alpha': {Platform.DISCORD: secondary}}
    runner._self_post_api_server = AsyncMock()
    return runner, primary, secondary, relay


@pytest.mark.asyncio
@pytest.mark.parametrize('case', [
    'stored', 'cached', 'cold', 'key-only', 'group-suffix', 'named-primary',
    'primary-relay', 'disabled-native', 'disabled-no-relay', 'missing', 'empty',
    'wrong-platform', 'profile-conflict', 'api-downgrade', 'transport-owner',
    'stale-owner', 'trusted-relay', 'slack-explicit', 'slack-ambiguous',
])
async def test_completion_keeps_recorded_profile_and_transport(case, monkeypatch, tmp_path):
    runner, primary, secondary, relay = runner_fixture(monkeypatch, tmp_path)
    try:
        profile = 'operator' if case == 'named-primary' else 'alpha'
        if case in {'primary-relay', 'disabled-native', 'disabled-no-relay'}:
            profile = None
        platform = Platform.SLACK if case.startswith('slack-') or case == 'primary-relay' else Platform.DISCORD
        source = SessionSource(platform=platform, chat_id='123', chat_type='dm',
                               thread_id='42', user_id='owner', scope_id='scope', profile=profile)
        if case == 'group-suffix':
            source.chat_type, source.thread_id = 'group', None
        key = build_session_key(source, profile=profile)
        evt = {'type': 'completion', 'session_id': 'proc-fixture', 'session_key': key,
               'platform': platform.value, 'chat_id': '123', 'chat_type': source.chat_type,
               'scope_id': 'scope', 'user_id': 'owner'}
        expected = primary if case == 'named-primary' else secondary
        result = True
        if case in {'stored', 'transport-owner', 'stale-owner', 'trusted-relay'}:
            runner.session_store._entries[key] = SimpleNamespace(origin=source)
        if case == 'cached':
            runner._cache_session_source(key, source)
        if case == 'key-only':
            evt = {'type': 'completion', 'session_id': 'proc-fixture', 'session_key': key}
        if case == 'primary-relay':
            evt['thread_id'] = '42'
            expected = relay
        if case in {'disabled-native', 'disabled-no-relay'}:
            runner.config.platforms[Platform.DISCORD] = PlatformConfig(enabled=False)
            expected = relay
            if case == 'disabled-no-relay':
                runner.adapters.pop(Platform.RELAY)
                expected, result = None, None
        if case in {'missing', 'empty', 'wrong-platform'}:
            runner._profile_adapters = {} if case == 'missing' else {'alpha': {}}
            if case == 'wrong-platform':
                runner._profile_adapters['alpha'][Platform.TELEGRAM] = Sink()
            expected, result = None, False
        if case == 'profile-conflict':
            evt['profile'] = 'other'
            expected, result = None, False
        if case == 'api-downgrade':
            evt['session_key'] = 'agent:alpha:not_a_platform:dm:123'
            evt['platform'] = 'not_a_platform'
            evt['origin_session_id'] = 'raw-api-session'
            runner.adapters[Platform.API_SERVER] = SimpleNamespace(supports_async_delivery=False)
            expected, result = None, False
        if case in {'transport-owner', 'stale-owner'}:
            transport = Sink()
            setattr(source, "_transport_adapter_ref", weakref.ref(transport))
            if case == 'transport-owner':
                runner._profile_adapters['transport'] = {Platform.DISCORD: transport}
                expected = transport
        if case == 'trusted-relay':
            source.delivered_via_upstream_relay = True
            expected = relay
        if case.startswith('slack-'):
            runner._profile_adapters['alpha'] = {Platform.SLACK: secondary}
            evt['thread_id'] = '42'
            if case == 'slack-ambiguous':
                evt = {'type': 'completion', 'session_key': key}
                expected, result = None, False
        assert await runner._inject_watch_notification('fixture result', evt) is result
        runner._self_post_api_server.assert_not_awaited()
        if expected is not None:
            assert expected.order == ['prime', 'handle']
            event = expected.events[0]
            assert event.internal is True
            assert event.source.profile == profile
            assert event.source.chat_id == '123'
            assert event.source.thread_id == source.thread_id
        for sink in (primary, secondary, relay):
            if sink is not expected:
                assert not sink.events
    finally:
        runner._shutdown_executor()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['completion', 'async_delegation'])
async def test_unavailable_secondary_is_retryable_and_acknowledged_only_after_acceptance(kind, monkeypatch, tmp_path):
    import tools.async_delegation as delegation
    runner, primary, secondary, relay = runner_fixture(monkeypatch, tmp_path)
    claim, acknowledge, release = Mock(return_value=True), Mock(), Mock()
    monkeypatch.setattr(delegation, 'claim_completion_delivery', claim)
    monkeypatch.setattr(delegation, 'complete_completion_delivery', acknowledge)
    monkeypatch.setattr(delegation, 'release_completion_delivery', release)
    try:
        evt = {'type': kind, 'session_id': 'proc-fixture', 'started_at': 1.0,
               'session_key': 'agent:alpha:discord:dm:123', 'delegation_id': 'deleg-fixture'}
        runner._enrich_async_delegation_routing(evt)
        assert evt['platform'] == 'discord'
        assert evt['chat_id'] == '123'
        assert evt['profile'] == 'alpha'
        runner._profile_adapters['alpha'] = {}
        assert await runner._deliver_completion_notification('fixture', evt) is False
        acknowledge.assert_not_called()
        assert not primary.events and not relay.events
        if kind == 'async_delegation':
            release.assert_called_once()
        runner._profile_adapters['alpha'][Platform.DISCORD] = secondary
        secondary.handle_message = AsyncMock(side_effect=RuntimeError('transport unavailable'))
        assert await runner._deliver_completion_notification('fixture', evt) is False
        acknowledge.assert_not_called()
        secondary.handle_message = AsyncMock()
        assert await runner._deliver_completion_notification('fixture', evt) is True
        assert secondary.handle_message.await_count == 1
        assert await runner._deliver_completion_notification('fixture', evt) is None
        assert secondary.handle_message.await_count == 1
        if kind == 'async_delegation':
            acknowledge.assert_called_once()
    finally:
        runner._shutdown_executor()


@pytest.mark.asyncio
@pytest.mark.parametrize('origin_location', ['stored', 'cached'])
@pytest.mark.parametrize('multiplex', [False, True])
async def test_real_session_namespace_preserves_its_named_primary(origin_location, multiplex, monkeypatch, tmp_path):
    runner, primary, _, _ = runner_fixture(monkeypatch, tmp_path)
    try:
        runner.config.multiplex_profiles = multiplex
        runner.session_store.config = runner.config
        source = SessionSource(platform=Platform.DISCORD, chat_id='123', profile='operator')
        key = runner.session_store._generate_session_key(source)
        if origin_location == 'stored':
            runner.session_store._entries[key] = SimpleNamespace(origin=source)
        else:
            runner._cache_session_source(key, source)
        assert await runner._inject_watch_notification('owned result', {'session_key': key}) is True
        assert primary.events[0].source.profile == 'operator'
        primary.events.clear()
        assert await runner._inject_watch_notification('conflict', {'session_key': key, 'profile': 'alpha'}) is False
        assert not primary.events
    finally:
        runner._shutdown_executor()


@pytest.mark.parametrize('case', [
    'group-participant', 'channel-participant', 'thread-participant',
    'slack-direct', 'slack-enriched', 'slack-thread-direct', 'slack-thread-enriched',
    'bad-namespace-space', 'bad-namespace-empty', 'bad-chat-empty',
    'conflicting-chat', 'conflicting-platform', 'conflicting-type', 'conflicting-thread',
])
def test_cold_route_preserves_identity_or_refuses(case, monkeypatch, tmp_path):
    runner, primary, secondary, relay = runner_fixture(monkeypatch, tmp_path)
    try:
        source = SessionSource(platform=Platform.DISCORD, chat_id='123', chat_type='dm', profile='alpha')
        if case.endswith('participant'):
            source.chat_type = case.split('-')[0]
            source.user_id = 'owner'
            if source.chat_type == 'thread':
                runner.config.thread_sessions_per_user = True
                source.thread_id = '42'
            key = build_session_key(source, profile='alpha', thread_sessions_per_user=runner.config.thread_sessions_per_user)
            evt = {'session_key': key}
            recovered = runner._build_process_event_source(evt)
            assert recovered is None or build_session_key(recovered, profile='alpha',
                thread_sessions_per_user=runner.config.thread_sessions_per_user) == key
            # Explicit participant/thread metadata must still recover the original route.
            evt.update(platform='discord', chat_type=source.chat_type, chat_id='123', user_id='owner')
            if source.thread_id:
                evt['thread_id'] = source.thread_id
            recovered = runner._build_process_event_source(evt)
            assert recovered is not None
            assert recovered.user_id == source.user_id and recovered.thread_id == source.thread_id
        elif case.startswith('slack-'):
            source = SessionSource(platform=Platform.SLACK, chat_id='D123', scope_id='T123',
                                   chat_type='dm', thread_id='42' if 'thread' in case else None)
            evt = {'session_key': build_session_key(source), 'platform': 'slack', 'chat_type': 'dm',
                   'chat_id': 'D123', 'scope_id': 'T123'}
            if source.thread_id:
                evt['thread_id'] = source.thread_id
            if case.endswith('enriched'):
                runner._enrich_async_delegation_routing(evt)
            recovered = runner._build_process_event_source(evt)
            assert recovered is not None
            assert (recovered.chat_id, recovered.scope_id, recovered.thread_id) == ('D123', 'T123', source.thread_id)
        else:
            evt = {'session_key': build_session_key(source, profile='alpha'),
                   'platform': 'discord', 'chat_type': 'dm', 'chat_id': '123'}
            changes = {
                'bad-namespace-space': {'session_key': 'agent: alpha:discord:dm:123'},
                'bad-namespace-empty': {'session_key': 'agent::discord:dm:123'},
                'bad-chat-empty': {'session_key': 'agent:alpha:discord:dm:'},
                'conflicting-chat': {'chat_id': '999'},
                'conflicting-platform': {'platform': 'telegram'},
                'conflicting-type': {'chat_type': 'group'},
                'conflicting-thread': {'thread_id': '42'},
            }
            evt.update(changes[case])
            assert runner._build_process_event_source(evt) is None
            runner._enrich_async_delegation_routing(evt)
            assert runner._build_process_event_source(evt) is None
            assert not primary.events and not secondary.events and not relay.events
    finally:
        runner._shutdown_executor()
