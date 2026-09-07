"""Delivery-only races cannot replay work or steal/revive evaluations."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest
from hermes_cli import goals
from hermes_state import SessionDB


def test_notice_consumption_during_gate_then_judge_preserves_owner(monkeypatch):
    mgr = goals.GoalManager('tokens')
    mgr.set('work')
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: ('continue','next',False,None,False))
    first = mgr.evaluate_after_turn('one')['goal_fence']
    # Add gate via control, then obtain a new pair of tokens for that generation.
    mgr.add_gate('true')
    first = mgr.evaluate_after_turn('two')['goal_fence']
    entered, release = threading.Event(), threading.Event()
    calls = []
    def gate(g):
        calls.append('gate'); entered.set()
        assert release.wait(10)
        return False, 7, 'real outcome'
    monkeypatch.setattr(goals,'run_gate',gate)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(mgr.evaluate_after_turn,'three')
        try:
            assert entered.wait(10)
            consumer = goals.GoalManager('tokens')
            owner = consumer.state.evaluation_id
            assert owner
            assert consumer.consume_notice(first)
            assert consumer.consume_continuation(first)
            assert goals.load_goal('tokens').evaluation_id == owner
        finally:
            release.set()
        result = pending.result(10)
    with _readback() as db:
        state = goals.GoalState.from_json(db.get_meta('goal:tokens'))
    assert calls == ['gate']
    assert result['should_continue'] and state.evaluation_id == ''
    assert state.gates[0].attempts == 1 and state.gates[0].last_exit_code == 7
    assert state.continuation_id != first['continuation_id']
    assert mgr.consume_notice(result['goal_fence'])
    assert not mgr.consume_notice(first)


from contextlib import contextmanager
@contextmanager
def _readback():
    db = SessionDB()
    try:
        yield db
    finally:
        db.close()


def test_delivery_raw_conflict_reconciles_without_second_provider_call(monkeypatch):
    mgr = goals.GoalManager('settle')
    mgr.set('work')
    monkeypatch.setattr(goals,'judge_goal',lambda *a,**k: ('continue','next',False,None,False))
    previous = mgr.evaluate_after_turn('first')['goal_fence']
    db = goals._get_session_db()
    real_cas = db.compare_and_set_meta
    collided = []
    calls = []
    def cas(updates, **kwargs):
        expected, replacement = updates['goal:settle']
        incoming = json.loads(replacement)
        if incoming['last_reason'] == 'second' and not collided:
            collided.append(1)
            # A genuinely committed independent consumer lands between the evaluator read and CAS.
            with _readback() as consumer:
                latest = consumer.get_meta('goal:settle')
                row = json.loads(latest)
                row['notice_id'] = ''
                assert consumer.compare_and_set_meta({'goal:settle':(latest,json.dumps(row))})
        return real_cas(updates, **kwargs)
    def judge(*a, **k):
        calls.append(1)
        return 'continue','second',False,None,False
    monkeypatch.setattr(db,'compare_and_set_meta',cas)
    monkeypatch.setattr(goals,'judge_goal',judge)
    result = mgr.evaluate_after_turn('second')
    assert result['should_continue'] and calls == [1] and collided == [1]
    assert goals.load_goal('settle').evaluation_id == ''
    assert not mgr.consume_notice(previous)


def test_control_while_gate_running_discards_result_and_stops_downstream(monkeypatch):
    mgr = goals.GoalManager('gate-control')
    mgr.set('work'); mgr.add_gate('first'); mgr.add_gate('second')
    calls = []
    def gate(g):
        calls.append(g.command)
        goals.GoalManager('gate-control').pause()
        return True,0,'finished after pause'
    monkeypatch.setattr(goals,'run_gate',gate)
    monkeypatch.setattr(goals,'judge_goal',lambda *a,**k: pytest.fail('stale judge'))
    result = mgr.evaluate_after_turn('work')
    assert result['verdict'] == 'stale' and result['message'] == ''
    assert calls == ['first']
    state = goals.load_goal('gate-control')
    assert state.status == 'paused' and state.gates[0].last_exit_code is None


def test_legacy_raw_upgrade_crash_claim_resume_and_store_provenance(monkeypatch, tmp_path):
    db = goals._get_session_db()
    raw = '{ "goal" : "legacy", "status": "active" }'
    db.set_meta('goal:legacy',raw)
    a, b = goals.load_goal('legacy'), goals.load_goal('legacy')
    assert a.goal_id == b.goal_id == '' and db.get_meta('goal:legacy') == raw
    goals.save_goal('legacy',a)
    with pytest.raises(goals.GoalConflict):
        goals.save_goal('legacy',b)
    class Crash(BaseException): pass
    monkeypatch.setattr(goals,'judge_goal',lambda *a,**k: (_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):
        goals.GoalManager('legacy').evaluate_after_turn('work')
    claim = goals.load_goal('legacy').evaluation_id
    assert claim
    assert goals.GoalManager('legacy').evaluate_after_turn('retry')['verdict'] == 'claimed'
    assert goals.load_goal('legacy').evaluation_id == claim
    manager = goals.GoalManager('legacy')
    manager.resume()
    assert goals.load_goal('legacy').evaluation_id == ''
    state = goals.load_goal('legacy')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path / 'other-profile'))
    with pytest.raises(goals.GoalConflict): goals.save_goal('legacy',state)
    with pytest.raises(goals.GoalConflict): manager.set('wrong store')
    assert Path(goals.__file__).resolve().parent.parent == Path(__file__).resolve().parents[2]


def test_error_and_ambiguous_commit_never_report_success(monkeypatch):
    mgr = goals.GoalManager('errors'); mgr.set('work')
    db = goals._get_session_db()
    real_cas = db.compare_and_set_meta
    def commit_then_error(updates, **kwargs):
        accepted = real_cas(updates, **kwargs)
        if accepted: raise OSError('ambiguous after commit')
        return accepted
    monkeypatch.setattr(db,'compare_and_set_meta',commit_then_error)
    with pytest.raises(goals.GoalPersistenceError): mgr.pause()
    assert goals.load_goal('errors').status == 'paused'
    monkeypatch.setattr(db,'compare_and_set_meta',real_cas)
    mgr.resume()
    monkeypatch.setattr(db,'compare_and_set_meta',commit_then_error)
    result = mgr.evaluate_after_turn('work')
    assert not result['should_continue'] and result['status'] == 'unknown'
    assert 'unknown' in result['message']
    assert goals.load_goal('errors').evaluation_id


@pytest.mark.parametrize('collisions', [3, 6])
def test_settlement_exhaustion_releases_or_explicitly_reports_retained_claim(monkeypatch, collisions):
    mgr = goals.GoalManager('churn'); mgr.set('work')
    calls = []
    def judge(*a, **k):
        calls.append(1)
        return 'continue','finished',False,None,False
    monkeypatch.setattr(goals,'judge_goal',judge)
    original = goals._cas_goal
    remaining = [collisions]
    injecting = [False]
    def competing_reservation(session_id, raw, state):
        if not injecting[0] and state.last_reason == 'finished' and remaining[0]:
            injecting[0] = True
            try:
                remaining[0] -= 1
                assert goals.GoalManager(session_id).reserve_continuation()
            finally:
                injecting[0] = False
        return original(session_id,raw,state)
    monkeypatch.setattr(goals,'_cas_goal',competing_reservation)
    result = mgr.evaluate_after_turn('work')
    state = goals.load_goal('churn')
    assert calls == [1] and not result['should_continue']
    if collisions == 3:
        assert state.status == 'paused' and not state.evaluation_id
    else:
        assert result['status'] == 'unknown' and 'claim may remain' in result['message']
        assert state.evaluation_id


def test_reservation_race_cannot_attach_old_prompt_to_replacement(monkeypatch):
    from hermes_cli.goal_command import dispatch_goal_command
    mgr = goals.GoalManager('reserve')
    original = mgr.reserve_continuation
    def replacement():
        goals.GoalManager('reserve').set('new goal')
        return original()
    monkeypatch.setattr(mgr,'reserve_continuation',replacement)
    result = dispatch_goal_command(mgr,'old goal',authorize_gate=lambda:None)
    assert result.error and result.prompt is None
    assert goals.load_goal('reserve').goal == 'new goal'
    assert not goals.load_goal('reserve').continuation_id


def test_migration_conflict_has_no_partial_archive(monkeypatch):
    mgr = goals.GoalManager('migrate'); mgr.set('original')
    db = goals._get_session_db()
    real = db.compare_and_set_meta
    def race(updates, **kwargs):
        if len(updates) == 2:
            goals.GoalManager('destination').set('existing destination')
        return real(updates, **kwargs)
    monkeypatch.setattr(db,'compare_and_set_meta',race)
    assert not goals.migrate_goal_to_session('migrate','destination')
    assert goals.load_goal('migrate').status == 'active'
    assert goals.load_goal('destination').goal == 'existing destination'


def test_two_competing_delivery_consumers_have_one_winner():
    mgr = goals.GoalManager('duplicate'); mgr.set('work')
    fence = mgr.reserve_continuation()
    barrier = threading.Barrier(2)
    def consume(_):
        other = goals.GoalManager('duplicate')
        barrier.wait(timeout=10)
        return other.consume_continuation(fence)
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(consume,range(2))) == [False,True]
    for malformed in (None, {}, {'session_id':'duplicate'}, dict(fence,generation=True),dict(fence,session_id='other')):
        assert not mgr.continuation_pending(malformed)
        assert not mgr.consume_continuation(malformed)
