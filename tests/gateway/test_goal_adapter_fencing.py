"""Adapter and result-path regressions exposed by independent goal-fencing review."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class Adapter(BasePlatformAdapter):
    async def connect(self): pass
    async def disconnect(self): pass
    async def get_chat_info(self, chat_id): return {}
    async def send(self, *args, **kwargs): return NS(success=True, message_id='fixture')
    async def send_typing(self, *args, **kwargs): pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_ACK_ENABLED', 'false')
    token = set_hermes_home_override(str(tmp_path))
    import socket
    monkeypatch.setattr(socket.socket, 'connect', Mock(side_effect=AssertionError('network forbidden')))
    goals._get_session_db()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = NS(multiplex_profiles=False)
    runner._draining = False
    source = SessionSource(platform=Platform.DISCORD, chat_id='fixture', user_id='tester')
    adapter = Adapter(PlatformConfig(enabled=True), Platform.DISCORD)
    runner._adapter_for_source = lambda s: adapter
    runner._session_key_for_source = lambda s: 'key'
    mgr = goals.GoalManager('session')
    mgr.set('old fixture objective')
    event = MessageEvent(text=mgr.next_continuation_prompt(), source=source,
                         metadata={'hermes_goal': mgr.reserve_continuation()})
    yield runner, source, adapter, mgr, event
    reset_hermes_home_override(token)


@pytest.mark.asyncio
@pytest.mark.parametrize('word', ['/stop', '/status'])
async def test_generated_slash_goal_reaches_native_drain_and_admission(env, word):
    runner, source, adapter, mgr, _ = env
    runner._adapter_and_key_for = lambda e: (adapter, 'key')
    runner._get_goal_manager_for_event = AsyncMock(return_value=(mgr, NS(session_id='session')))
    await runner._handle_goal_command(MessageEvent(text='/goal ' + word, source=source))
    queued = adapter._pending_messages['key']
    event, pending = await runner._run_agent_drain_pending(
        {'final_response': 'parent finished', 'messages': []}, adapter, source, 'key')
    assert event is queued
    assert pending == word and event.get_command() is None
    runner._run_agent_inner = AsyncMock(return_value={'final_response': 'goal accepted'})
    result = await runner._run_agent(message=pending, context_prompt='', history=[], source=source,
                                    session_id='session', goal_event=event)
    assert result['final_response'] == 'goal accepted'
    runner._run_agent_inner.assert_awaited_once()
    assert not mgr.continuation_pending(event.metadata['hermes_goal'])


@pytest.mark.asyncio
@pytest.mark.parametrize('eventless', [False, True])
async def test_pending_slash_safety_net_retains_actual_control_protection(env, eventless):
    runner, source, adapter, _, _ = env
    result = {'final_response': 'parent finished'}
    if eventless:
        result['pending_steer'] = '/status'
    else:
        runner._enqueue_fifo('key', MessageEvent(text='/status', source=source), adapter)
    event, pending = await runner._run_agent_drain_pending(result, adapter, source, 'key')
    assert event is None and pending is None


@pytest.mark.asyncio
@pytest.mark.parametrize('authorized', [True, False])
@pytest.mark.parametrize('synthetic', [True, False])
async def test_actual_adapter_busy_route_must_not_steer_stale_goal(env, authorized, synthetic):
    runner, source, adapter, mgr, event = env
    mgr.pause()
    if not synthetic:
        event.metadata.clear()  # a user may legitimately quote the same goal text
    agent = NS(steer=Mock(return_value=True), interrupt=Mock())
    runner._is_user_authorized = lambda s: authorized
    runner._effective_busy_input_mode = lambda s: 'steer'
    runner._effective_busy_text_mode = lambda s: 'steer'
    runner._peek_session_state = lambda k: NS(turn=NS(agent=agent))
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._prepare_busy_steer_text = AsyncMock(side_effect=lambda e: e.text)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    await adapter._handle_message_while_active(event, 'key')
    agent.interrupt.assert_not_called()
    if authorized and not synthetic:
        agent.steer.assert_called_once()
    else:
        agent.steer.assert_not_called()
    assert adapter._pending_messages.get('key') is (event if authorized and synthetic else None)


@pytest.mark.asyncio
async def test_rejected_direct_goal_is_not_delivered_or_persisted_as_user(env, monkeypatch):
    runner, source, adapter, mgr, event = env
    mgr.pause()
    history = [{'role': 'user', 'content': 'real earlier work'},
               {'role': 'assistant', 'content': 'real earlier answer'}]
    entry = NS(session_id='session', session_key='key')
    prepared = runner._PreparedTurn(history, '', event.text, None, None, None)
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, 'key'))
    runner._hmwa_prepare_turn = AsyncMock(return_value=(prepared, None))
    runner.hooks = NS(emit=AsyncMock())
    runner._run_agent_inner = AsyncMock(return_value={'final_response': 'must not run'})
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_session_run_current = lambda *a: True
    runner._clear_session_env = lambda *a: None
    runner._clear_restart_failure_count = AsyncMock()
    runner._hmwa_prepend_reasoning = lambda result, response, *args: response
    runner._hmwa_runtime_footer_line = lambda *a: ''
    runner._hmwa_post_turn_hooks = AsyncMock()
    runner._hmwa_classify_turn_failure = lambda *a: (False, False, False)
    runner._hmwa_compression_exhaustion_reset = AsyncMock(side_effect=lambda result, response, entry, *a: (response, entry))
    runner._session_db = None  # supported non-agent-persisted gateway transcript path
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._should_send_voice_reply = lambda *a, **kw: False
    # Fail visibly if the test itself failed to supply necessary infrastructure.
    runner._hmwa_agent_error_reply = AsyncMock(side_effect=AssertionError('unexpected handler error'))
    store = NS(clear_resume_pending=AsyncMock(), append_to_transcript=AsyncMock(), update_session=AsyncMock())
    monkeypatch.setattr(GatewayRunner, 'async_session_store', property(lambda self: store))
    reply = await runner._handle_message_with_agent(event, source, 'key', 1)
    runner._run_agent_inner.assert_not_called()
    rows = [call.args[1] for call in store.append_to_transcript.await_args_list]
    assert not reply and not rows and not store.clear_resume_pending.await_count


def recursive_setup(env):
    runner, source, adapter, mgr, event = env
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(side_effect=lambda **kw: kw['event'].text)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._deliver_queued_first_response = AsyncMock()
    runner._pop_post_delivery_callback = lambda *a: None
    runner._run_agent_inner = AsyncMock(return_value={'final_response': 'unexpected followup'})
    result = {'final_response': 'Already completed real work', 'messages': [
        {'role': 'user', 'content': 'real work'}, {'role': 'assistant', 'content': 'Already completed real work'}],
        'api_calls': 1, 'completed': True}
    ctx = NS(source=source, session_id='session', session_key='key', run_generation=1,
             _interrupt_depth=0, history=[], _status_thread_metadata=None, context_prompt='',
             stream_consumer_holder=[None], event_message_id=None)
    return runner, adapter, mgr, event, result, ctx


@pytest.mark.asyncio
async def test_inactive_recursive_head_must_not_return_already_delivered_response(env):
    runner, adapter, mgr, event, result, ctx = recursive_setup(env)
    mgr.pause()
    real = MessageEvent(text='following real user work', source=ctx.source, media_urls=['fixture.png'])
    adapter._pending_messages['key'] = real  # native promotion leaves next overflow item here
    sent = []
    runner._deliver_queued_first_response.side_effect = lambda content, **kw: sent.append(content)
    returned = await runner._run_agent_queued_followup(ctx, adapter, event.text, event, result, result, None)
    runner._run_agent_inner.assert_not_called()
    assert adapter._pending_messages['key'] is real  # following work is NOT lost from the slot
    # Exercise downstream delivery plus native pending handoff: the real event
    # survives and drains, but the already-sent answer is sent again.
    import asyncio
    async def record_send(chat_id, content, **kw):
        sent.append(content)
        return NS(success=True, message_id='fixture')
    adapter.send = record_send
    handled_real = []
    async def handler(incoming):
        if incoming is event:
            runner._should_send_voice_reply = lambda *a, **kw: False
            return await runner._hmwa_deliver_turn_response(
                event, ctx.source, NS(session_id='session'), 'key', 1,
                returned, returned['messages'], returned['final_response'], '', False)
        handled_real.append(incoming)
        return 'following real work completed'
    adapter.set_message_handler(handler)
    await adapter._process_message_background(event, 'key')
    for _ in range(5):
        tasks = [task for task in adapter._background_tasks if not task.done()]
        if not tasks:
            break
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert handled_real == [real]
    assert sent == ['Already completed real work', 'following real work completed']


@pytest.mark.asyncio
async def test_rejected_recursive_head_must_not_erase_successful_turn_result(env):
    runner, adapter, mgr, event, result, ctx = recursive_setup(env)
    mgr.set('replacement keeps status active')
    returned = await runner._run_agent_queued_followup(ctx, adapter, event.text, event, result, result, None)
    runner._run_agent_inner.assert_not_called()
    from gateway.run import _normalize_empty_agent_response
    text = _normalize_empty_agent_response(returned, returned.get('final_response') or '')
    assert 'Please send it again' not in text
    assert returned['final_response'] == result['final_response']
    assert returned['messages'] == result['messages']
    assert returned['api_calls'] == result['api_calls']
    assert returned['completed'] is True
    assert returned['already_sent'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['startup_resume', 'startup_restore', 'orphan', 'late_drain'])
async def test_native_lifecycle_preserves_fence_and_following_user_work(env, route):
    import asyncio
    from gateway.session import build_session_key
    runner, source, adapter, mgr, stale = env
    mgr.pause()
    key = build_session_key(source)
    runner._session_key_for_source = lambda source: key
    runner._queued_events = {}
    real = MessageEvent(text='following real work', source=source, media_urls=['fixture.png'])
    entries, received = [], []
    async def provider(message, *args, **kwargs):
        entries.append(message)
        return {'final_response': 'real completed', 'messages': []}
    runner._run_agent_inner = provider
    async def handler(event):
        received.append(event)
        result = await runner._run_agent(message=event.text, context_prompt='', history=[],
                                         source=event.source, session_id='session', goal_event=event)
        return result.get('final_response') or None
    adapter.set_message_handler(handler)
    if route == 'startup_resume':
        await runner._run_startup_resume_event(adapter, stale, key)
        await adapter.handle_message(real)
    elif route == 'startup_restore':
        runner._queue_startup_restore_event(stale)
        runner._queue_startup_restore_event(real)
        assert await runner._drain_startup_restore_queue() == 2
    elif route == 'orphan':
        runner._session_state(key).conversation.queued_events.append(stale)
        rescued, _, _ = runner._hm_rescue_orphaned_fifo(real, source, False, key)
        assert rescued is stale
        await adapter._process_message_background(rescued, key)
    else:
        injected = False
        async def inject_at_cleanup(*args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                adapter._pending_messages[key] = stale
        adapter.stop_typing = inject_at_cleanup
        await adapter._process_message_background(real, key)
    try:
        for _ in range(5):
            tasks = [task for task in adapter._background_tasks if not task.done()]
            if not tasks:
                break
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        assert entries == [real.text]
        assert received.count(real) == 1
        assert stale in received
        assert real.media_urls == ['fixture.png']
    finally:
        await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_ambiguous_notice_send_is_not_retried(env, monkeypatch):
    runner, source, adapter, mgr, _ = env
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'verified', False, None, False))
    decision = mgr.evaluate_after_turn('answer')
    adapter.send = AsyncMock(side_effect=RuntimeError('ambiguous transport failure'))
    with pytest.raises(RuntimeError, match='ambiguous'):
        await runner._send_goal_status_notice(source, decision['message'], fence=decision['goal_fence'])
    await runner._send_goal_status_notice(source, decision['message'], fence=decision['goal_fence'])
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_positive_notice_claim_once_and_stale_profile_rejected(env, tmp_path, monkeypatch):
    runner, source, adapter, mgr, event = env
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'fixture verified', False, None, False))
    decision = mgr.evaluate_after_turn('real answer')
    adapter.send = AsyncMock(return_value=NS(success=True))
    await runner._send_goal_status_notice(source, decision['message'], fence=decision['goal_fence'])
    await runner._send_goal_status_notice(source, decision['message'], fence=decision['goal_fence'])
    assert adapter.send.await_count == 1
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    other = tmp_path / 'other-profile'; other.mkdir()
    token = set_hermes_home_override(str(other))
    try:
        goals._get_session_db()
        other_mgr = goals.GoalManager('session'); other_mgr.set('unrelated profile goal')
        admitted, _ = runner._admit_goal_turn(event, 'session')
        assert admitted is False
        assert other_mgr.state.turns_used == 0
    finally:
        reset_hermes_home_override(token)
