"""Gateway admission must use durable goal identity, never the visible prompt."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


@pytest.fixture
def context(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(str(home))
    goals._get_session_db()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._run_agent_inner = AsyncMock(return_value={'final_response': 'worked', 'messages': []})
    source = SessionSource(platform=Platform.DISCORD, chat_id='test', user_id='user')
    mgr = goals.GoalManager('session')
    mgr.set('ship safe fixture')
    yield runner, source, mgr
    reset_hermes_home_override(token)


def continuation(source, mgr):
    return MessageEvent(text=mgr.next_continuation_prompt(), source=source,
                        metadata={'hermes_goal': mgr.reserve_continuation()})


async def run(runner, source, event):
    return await runner._run_agent(message=event.text, context_prompt='', history=[],
                                   source=source, session_id='session', goal_event=event)


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['pause', 'clear', 'replace', 'resume'])
async def test_old_goal_event_cannot_enter_agent_after_control(context, control):
    runner, source, mgr = context
    event = continuation(source, mgr)
    if control == 'replace':
        mgr.set('new objective')
    else:
        getattr(mgr, control)()
    await run(runner, source, event)
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
async def test_valid_event_is_consumed_once_but_matching_user_text_is_not_authority(context):
    runner, source, mgr = context
    event = continuation(source, mgr)
    first = await run(runner, source, event)
    await run(runner, source, event)
    assert runner._run_agent_inner.await_count == 1
    assert first['_goal_turn']['goal_id'] == mgr.state.goal_id
    await run(runner, source, MessageEvent(text=event.text, source=source))
    assert runner._run_agent_inner.await_count == 2
    assert not mgr.continuation_pending(event.metadata['hermes_goal'])


@pytest.mark.asyncio
@pytest.mark.parametrize('fence', [None, {}, 'invalid', {'session_id': 'wrong'}])
async def test_known_synthetic_missing_or_malformed_identity_fails_closed(context, fence):
    runner, source, _ = context
    await run(runner, source, MessageEvent(text='old intent', source=source,
                                         metadata={'hermes_goal': fence}))
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('barrier', ['owner', 'wait', 'budget'])
async def test_continuation_claim_rechecks_eligibility_after_preflight(context, monkeypatch, barrier):
    import time
    import uuid
    runner, source, mgr = context
    event = continuation(source, mgr)
    consume = goals.GoalManager.consume_continuation
    def raced(manager, fence, **kwargs):
        state, raw = goals._read_goal('session')
        if barrier == 'owner':
            state.evaluation_id = str(uuid.uuid4())
        elif barrier == 'wait':
            state.waiting_until = time.time() + 60
        else:
            state.turns_used = state.max_turns
        goals._cas_goal('session', raw, state)
        return consume(manager, fence, **kwargs)
    monkeypatch.setattr(goals.GoalManager, 'consume_continuation', raced)
    await run(runner, source, event)
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'corrupt'])
async def test_goal_storage_failure_never_admits_synthetic_work(context, monkeypatch, failure):
    runner, source, mgr = context
    event = continuation(source, mgr)
    if failure == 'unavailable':
        monkeypatch.setattr(goals, '_get_session_db', lambda: None)
    else:
        goals._get_session_db().set_meta('goal:session', '{broken json')
    await run(runner, source, event)
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
async def test_truly_absent_goal_snapshot_cannot_adopt_a_later_goal(context, monkeypatch):
    from unittest.mock import Mock
    runner, source, _ = context
    event = MessageEvent(text='ordinary work', source=source)
    result = await runner._run_agent(message=event.text, context_prompt='', history=[], source=source,
                                     session_id='previously-absent', goal_event=event)
    assert result['_goal_turn']['goal_id'] is None
    mgr = goals.GoalManager('previously-absent')
    mgr.set('later goal')
    judge = Mock(side_effect=AssertionError('old turn must not judge new goal'))
    monkeypatch.setattr(goals, 'judge_goal', judge)
    decision = mgr.evaluate_after_turn('ordinary work completed', expected_goal=result['_goal_turn'])
    assert decision['verdict'] == 'stale'
    judge.assert_not_called()


@pytest.mark.asyncio
async def test_session_migration_preserves_goal_but_invalidates_old_queued_intent(context):
    runner, source, mgr = context
    old_event = continuation(source, mgr)
    assert goals.migrate_goal_to_session('session', 'compressed-session')
    moved = goals.GoalManager('compressed-session')
    assert moved.state.goal == mgr.state.goal
    assert moved.state.max_turns == mgr.state.max_turns
    await runner._run_agent(message=old_event.text, context_prompt='', history=[], source=source,
                             session_id='compressed-session', goal_event=old_event)
    runner._run_agent_inner.assert_not_called()
    current_event = continuation(source, moved)
    await runner._run_agent(message=current_event.text, context_prompt='', history=[], source=source,
                             session_id='compressed-session', goal_event=current_event)
    runner._run_agent_inner.assert_called_once()


@pytest.mark.asyncio
async def test_old_user_turn_cannot_judge_replacement_goal(context, monkeypatch):
    runner, source, mgr = context
    result = await run(runner, source, MessageEvent(text='work', source=source))
    mgr.set('replacement')
    judge = AsyncMock()  # must never be used; real manager rejects before external work
    monkeypatch.setattr(goals, 'judge_goal', judge)
    decision = mgr.evaluate_after_turn('worked', expected_goal=result['_goal_turn'])
    assert decision['should_continue'] is False
    assert decision['message'] == ''
    assert goals.GoalManager('session').state.turns_used == 0
    judge.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('consumer', ['direct', 'recursive'])
async def test_real_turn_consumers_carry_fence_after_awaited_preparation(context, consumer):
    runner, source, mgr = context
    event = continuation(source, mgr)
    # Replace during awaited preparation, rather than only before entering the handler.
    async def prepare(*args, **kwargs):
        mgr.set('replacement during prepare')
        if consumer == 'recursive':
            return event.text
        return runner._PreparedTurn([], '', event.text, None, None, None), None
    runner._clear_session_env = lambda tokens: None
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._hmwa_agent_error_reply = AsyncMock(return_value=None)
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_session_run_current = lambda *args: False
    if consumer == 'direct':
        runner._hmwa_resolve_session = AsyncMock(return_value=(source, SimpleNamespace(session_id='session'), 'key'))
        runner._hmwa_prepare_turn = prepare
        await runner._handle_message_with_agent(event, source, 'key', 1)
    else:
        adapter = SimpleNamespace(_active_sessions={}, send_typing=AsyncMock())
        runner._adapter_for_source = lambda source: adapter
        runner._session_key_for_source = lambda source: 'key'
        runner._run_agent_deliver_first_response = AsyncMock()
        runner._prepare_profile_scoped_inbound_message_text = prepare
        runner._refresh_agent_cache_message_count = AsyncMock()
        ctx = SimpleNamespace(source=source, session_id='session', session_key='key', run_generation=1,
                              _interrupt_depth=0, history=[], _status_thread_metadata=None, context_prompt='')
        await runner._run_agent_queued_followup(ctx, adapter, event.text, event,
                                                {'final_response': 'first'}, {'messages': []}, None)
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
async def test_busy_goal_event_never_steers_or_interrupts_inflight_work(context):
    from unittest.mock import Mock
    runner, source, mgr = context
    agent = Mock()
    adapter = SimpleNamespace(_pending_messages={})
    runner._adapter_for_source = lambda source: adapter
    runner._hm_busy_slash_or_photo = AsyncMock(return_value=(False, None))
    runner._effective_busy_input_mode = lambda source: 'steer'
    runner._hm_busy_telegram_grace_queue = lambda *args: False
    from gateway.session_state import SessionState
    state = SessionState()
    state.turn.agent = agent
    runner._peek_session_state = lambda key: state
    runner._draining = False
    event = continuation(source, mgr)
    mgr.pause()
    await runner._hm_handle_running_session_message(event, source, 'key')
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()
    assert adapter._pending_messages['key'] is event
    await run(runner, source, event)
    runner._run_agent_inner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'replace', 'previously_absent', 'pause_during_notice'])
async def test_post_turn_hook_uses_admitted_goal_not_later_goal(context, monkeypatch, change):
    runner, source, mgr = context
    adapter = SimpleNamespace(_pending_messages={}, send=AsyncMock(), _active_sessions={})
    runner._adapter_for_source = lambda source: adapter
    runner._session_key_for_source = lambda source: 'key'
    entry = SimpleNamespace(session_id='session')
    store = SimpleNamespace(get_or_create_session=AsyncMock(return_value=entry))
    monkeypatch.setattr(GatewayRunner, 'async_session_store', property(lambda self: store))
    runner._post_turn_loop_completion = AsyncMock()
    if change == 'previously_absent':
        mgr.clear()
    event = MessageEvent(text='work', source=source)
    result = await run(runner, source, event)
    event._goal_turn = result['_goal_turn']
    if change in {'replace', 'previously_absent'}:
        mgr.set('replacement')
    if change == 'pause_during_notice':
        async def pause_notice(*args, **kwargs):
            mgr.pause()
        runner._defer_goal_status_notice_after_delivery = pause_notice
    from unittest.mock import Mock
    judge = Mock(return_value=('continue', 'more work', False, None, False))
    monkeypatch.setattr(goals, 'judge_goal', judge)
    await runner._run_post_turn_hooks(agent_result=result, source=source, is_internal=False, event=event)
    if change == 'none':
        judge.assert_called_once()
        queued = adapter._pending_messages['key']
        assert goals.GoalManager('session').continuation_pending(queued.metadata['hermes_goal'])
    elif change == 'pause_during_notice':
        judge.assert_called_once()
        assert adapter._pending_messages == {}
    else:
        judge.assert_not_called()
        assert adapter._pending_messages == {}
        assert goals.GoalManager('session').state.turns_used == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['/goal resume', '/goal /stop'])
async def test_command_producer_and_delayed_notice_keep_committed_fences(context, monkeypatch, command):
    runner, source, mgr = context
    adapter = SimpleNamespace(_pending_messages={}, send=AsyncMock(), _active_sessions={})
    callbacks = []
    adapter.register_post_delivery_callback = lambda key, callback, **kwargs: callbacks.append(callback)
    runner._adapter_and_key_for = lambda event: (adapter, 'key')
    runner._adapter_for_source = lambda source: adapter
    runner._session_key_for_source = lambda source: 'key'
    runner._get_goal_manager_for_event = AsyncMock(return_value=(mgr, SimpleNamespace(session_id='session')))
    await runner._handle_goal_command(MessageEvent(text=command, source=source))
    queued = adapter._pending_messages['key']
    assert mgr.continuation_pending(queued.metadata['hermes_goal'])
    assert queued.allow_gateway_control is False
    assert queued.get_command() is None
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **kw: ('done', 'verified', False, None, False))
    decision = mgr.evaluate_after_turn('done')
    await runner._defer_goal_status_notice_after_delivery(source, decision['message'],
                                                         fence=decision['goal_fence'])
    mgr.set('replacement')
    await callbacks.pop()()
    adapter.send.assert_not_called()


def test_merge_and_cleanup_preserve_real_user_text_even_with_goal_prefix(context):
    from gateway.platforms.base import merge_pending_message_event
    runner, source, mgr = context
    synthetic = continuation(source, mgr)
    real = MessageEvent(text=synthetic.text, source=source, media_urls=['fixture.png'])
    slot = {'key': synthetic}
    merge_pending_message_event(slot, 'key', real)
    assert slot['key'] is real
    merge_pending_message_event(slot, 'key', synthetic)
    assert slot['key'] is real
    assert not runner._is_goal_continuation_event(real)
    assert runner._is_goal_continuation_event(synthetic)


def test_shutdown_spool_retains_synthetic_intent_without_user_transcript_replay(context, tmp_path):
    from gateway.shutdown_flush import _serialise_value, _recover_one_payload
    _, source, mgr = context
    event = continuation(source, mgr)
    value = _serialise_value(event)
    assert value['metadata']['hermes_goal'] == event.metadata['hermes_goal']
    value['session_id'] = 'session'
    class NoAppend:
        def append_message(self, **kwargs):
            pytest.fail('known synthetic intent must require explicit resume')
    assert _recover_one_payload(NoAppend(), tmp_path / 'spool.json',
                                {'session_key': 'key', 'data': value}) is False
