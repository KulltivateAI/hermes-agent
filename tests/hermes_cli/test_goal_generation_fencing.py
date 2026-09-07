"""Persisted ownership beats slow work and stale control objects (real TEMP DB)."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import threading

import pytest
from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
from hermes_state import SessionDB


def test_stale_public_save_and_cross_session_are_rejected():
    mgr = goals.GoalManager('one')
    mgr.set('original')
    stale = goals.load_goal('one')
    mgr.pause()
    stale.goal = 'stale'
    with pytest.raises(RuntimeError):
        goals.save_goal('one', stale)
    with pytest.raises(RuntimeError):
        goals.save_goal('two', goals.load_goal('one'))
    assert goals.load_goal('one').status == 'paused'
    assert goals.load_goal('two') is None
    loaded = goals.load_goal('one')
    loaded.last_reason = 'compatible loaded-state save'
    goals.save_goal('one', loaded)
    loaded.last_reason = 'second save'
    goals.save_goal('one', loaded)
    assert goals.load_goal('one').last_reason == 'second save'
    assert not any(k.startswith('_') for k in asdict(loaded))


def test_stale_indexed_edit_conflicts_but_simple_pause_refreshes():
    mgr = goals.GoalManager('edit')
    mgr.set('one')
    mgr.add_subgoal('first')
    stale = goals.GoalManager('edit')
    mgr.remove_subgoal(1)
    mgr.add_subgoal('different')
    with pytest.raises(RuntimeError):
        stale.remove_subgoal(1)
    stale.pause()
    state = goals.load_goal('edit')
    assert state.status == 'paused' and state.subgoals == ['different']


@pytest.mark.parametrize('control', ['pause', 'clear', 'set', 'resume', 'edit', 'wait'])
def test_control_during_judge_wins_without_stale_notice(monkeypatch, control):
    mgr = goals.GoalManager('race')
    mgr.set('same text')
    entered, release = threading.Event(), threading.Event()
    def judge(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return 'done', 'old verdict', False, None, False
    monkeypatch.setattr(goals, 'judge_goal', judge)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(mgr.evaluate_after_turn, 'work')
        try:
            assert entered.wait(10)
            other = goals.GoalManager('race')
            operations = {'pause':other.pause, 'clear':other.clear, 'set':lambda:other.set('same text'),
                          'resume':other.resume, 'edit':lambda:other.add_subgoal('new'),
                          'wait':lambda:other.wait_for_seconds(60)}
            operations[control]()
            accepted = goals.load_goal('race').to_json()
        finally:
            release.set()
        decision = future.result(10)
    assert not decision['should_continue'] and decision['message'] == '' and decision['verdict'] == 'stale'
    assert goals.load_goal('race').to_json() == accepted


def test_competing_evaluator_is_inert(monkeypatch):
    mgr = goals.GoalManager('owned')
    mgr.set('work')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def judge(*args, **kwargs):
        calls.append(1); entered.set()
        assert release.wait(10)
        return 'continue', 'next', False, None, False
    monkeypatch.setattr(goals, 'judge_goal', judge)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(mgr.evaluate_after_turn, 'one')
        try:
            assert entered.wait(10)
            second = goals.GoalManager('owned').evaluate_after_turn('two')
            assert not second['should_continue']
        finally:
            release.set()
        assert first.result(10)['should_continue']
    assert calls == [1]
    assert goals.load_goal('owned').turns_used == 1


def test_migration_invalidates_both_keys_and_keeps_paused_budget():
    mgr = goals.GoalManager('old')
    mgr.set('keep')
    mgr.add_gate('false')
    mgr.pause()
    old = goals.load_goal('old')
    assert goals.migrate_goal_to_session('old','new')
    moved = goals.load_goal('new')
    assert moved.status == 'paused' and moved.gates[0].command == 'false'
    assert moved.generation > old.generation
    with pytest.raises(RuntimeError):
        goals.save_goal('old', old)
    assert goals.load_goal('old').status == 'cleared'
    assert not goals.migrate_goal_to_session('new','old')


def test_command_reserves_fence_and_duplicate_consumer_loses():
    mgr = goals.GoalManager('delivery')
    result = dispatch_goal_command(mgr, 'work', authorize_gate=lambda:None)
    fence = result.goal_fence
    assert result.prompt and mgr.continuation_pending(fence)
    assert mgr.consume_continuation(fence)
    assert not mgr.consume_continuation(fence)
    mgr.resume()
    assert not mgr.continuation_pending(fence)
