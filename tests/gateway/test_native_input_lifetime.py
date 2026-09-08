"""Native input stamps survive normal work, never consumed replacement input."""
import asyncio
from contextvars import copy_context
from threading import Event, Lock
from types import SimpleNamespace

import pytest

from gateway import session_context as sc


def api():
    assert callable(getattr(sc, 'begin_native_input', None)), 'Native input lifetime is missing'
    return sc


@pytest.mark.asyncio
async def test_stamp_missing_and_old_owned_lifetime_fail_closed():
    ctx = api()
    ctx.reset_session_vars()
    with pytest.raises(ctx.NativeInputExpired):
        ctx.snapshot_native_input()
    ctx.begin_native_input()
    original = ctx.snapshot_native_input()
    ctx.set_session_vars(platform='discord', message_id='A')
    ctx.validate_native_input(original)
    ctx.begin_native_input()
    with pytest.raises(ctx.NativeInputExpired):
        ctx.validate_native_input(original)
    current = ctx.snapshot_native_input()
    ctx.clear_session_vars([])
    with pytest.raises(ctx.NativeInputExpired):
        ctx.validate_native_input(current)


@pytest.mark.asyncio
async def test_consumed_input_invalidates_already_copied_worker():
    ctx = api()
    ctx.begin_native_input()
    stamp = ctx.snapshot_native_input()
    before = copy_context()
    await asyncio.to_thread(lambda: before.run(ctx.validate_native_input, stamp))
    ctx.advance_native_input()
    with pytest.raises(ctx.NativeInputExpired):
        await asyncio.to_thread(lambda: before.run(ctx.validate_native_input, stamp))
    with pytest.raises(ctx.NativeInputExpired):
        ctx.validate_native_input(object())


@pytest.mark.asyncio
async def test_foreign_task_reset_and_cleanup_do_not_revoke_parent():
    ctx = api()
    ctx.begin_native_input()
    parent = ctx.snapshot_native_input()
    async def child():
        ctx.reset_session_vars()
        with pytest.raises(ctx.NativeInputExpired):
            ctx.validate_native_input(parent)
        ctx.begin_native_input()
        return ctx.snapshot_native_input(), copy_context()
    child_stamp, child_context = await asyncio.create_task(child())
    ctx.validate_native_input(parent)
    with pytest.raises(ctx.NativeInputExpired):
        child_context.run(ctx.validate_native_input, child_stamp)


@pytest.mark.asyncio
async def test_local_mutation_and_advance_have_one_ordering_point():
    ctx = api()
    ctx.begin_native_input()
    stamp = ctx.snapshot_native_input()
    entered, release, advancing = Event(), Event(), Event()
    order = []
    def mutate():
        with ctx.native_input_guard(stamp):
            entered.set()
            assert release.wait(3)
            order.append('mutation')
    def advance():
        advancing.set()
        ctx.advance_native_input()
        order.append('advance')
    mutation = asyncio.create_task(asyncio.to_thread(mutate))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        boundary = asyncio.create_task(asyncio.to_thread(advance))
        assert await asyncio.to_thread(advancing.wait, 2)
        assert order == []
        release.set()
        await asyncio.gather(mutation, boundary)
        assert order == ['mutation', 'advance']
        with pytest.raises(ctx.NativeInputExpired):
            with ctx.native_input_guard(stamp):
                order.append('stale mutation')
    finally:
        release.set()
        await asyncio.gather(mutation, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['batch', 'pre_api', 'redirect'])
async def test_real_input_consumption_fences_before_new_model_content(site):
    ctx = api()
    ctx.begin_native_input()
    stamp = ctx.snapshot_native_input()
    messages = [{'role': 'tool', 'content': 'ordinary result'}]
    agent = SimpleNamespace(_pending_steer='B', _pending_steer_lock=Lock(),
                            _drain_pending_steer=lambda: 'B', _strip_think_blocks=lambda value: value)
    if site == 'batch':
        from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
        apply_pending_steer_to_tool_results(agent, messages, 1)
    elif site == 'pre_api':
        from agent.turn_iteration_prep import _inject_steer_into_newest_tool_result
        _inject_steer_into_newest_tool_result(agent, messages, 'B')
    else:
        from agent.conversation_loop import _apply_active_turn_redirect
        _apply_active_turn_redirect(agent, messages, 'B')
    assert 'B' in str(messages)
    with pytest.raises(ctx.NativeInputExpired):
        ctx.validate_native_input(stamp)


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['batch', 'pre_api'])
async def test_requeue_without_target_does_not_consume_authority(site):
    ctx = api()
    ctx.begin_native_input()
    stamp = ctx.snapshot_native_input()
    agent = SimpleNamespace(_pending_steer='', _pending_steer_lock=Lock(), _drain_pending_steer=lambda: 'B')
    messages = [{'role': 'user', 'content': 'A'}]
    if site == 'batch':
        from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
        apply_pending_steer_to_tool_results(agent, messages, 1)
    else:
        from agent.turn_iteration_prep import _inject_steer_into_newest_tool_result
        _inject_steer_into_newest_tool_result(agent, messages, 'B')
    ctx.validate_native_input(stamp)
    assert agent._pending_steer == 'B'
    assert messages == [{'role': 'user', 'content': 'A'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('site', ['batch', 'pre_api', 'redirect'])
async def test_fence_failure_cannot_expose_new_input(site, monkeypatch):
    ctx = api()
    ctx.begin_native_input()
    def fail():
        raise RuntimeError('fixture input fence failed')
    monkeypatch.setattr(ctx, 'advance_native_input', fail)
    messages = [{'role': 'tool', 'content': 'A'}]
    agent = SimpleNamespace(_drain_pending_steer=lambda: 'B', _strip_think_blocks=lambda value: value)
    with pytest.raises(RuntimeError, match='fence failed'):
        if site == 'batch':
            from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
            apply_pending_steer_to_tool_results(agent, messages, 1)
        elif site == 'pre_api':
            from agent.turn_iteration_prep import _inject_steer_into_newest_tool_result
            _inject_steer_into_newest_tool_result(agent, messages, 'B')
        else:
            from agent.conversation_loop import _apply_active_turn_redirect
            _apply_active_turn_redirect(agent, messages, 'B')
    assert messages == [{'role': 'tool', 'content': 'A'}]
