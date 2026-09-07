"""Every automatic claim/expiry must use the same unclaimed, wait-eligible snapshot."""
import queue
import uuid

import pytest
from hermes_cli import goals
from hermes_cli.cli_loops_mixin import CLILoopsMixin
from hermes_cli.goal_fencing import GoalFencingMixin
from hermes_state import SessionDB


class Crash(BaseException):
    pass


def readback(sid):
    db = SessionDB()
    try:
        return goals.GoalState.from_json(db.get_meta('goal:' + sid))
    finally:
        db.close()


def cli_for(mgr):
    cli = CLILoopsMixin()
    cli.session_id = mgr.session_id
    cli._goal_manager = mgr
    cli._pending_input = queue.Queue()
    return cli


def crash_after_wait(mgr, monkeypatch):
    original = GoalFencingMixin._save_owned
    def crash(self, decision=None):
        if decision is not None:
            raise Crash()
        return original(self)
    with monkeypatch.context() as patch:
        patch.setattr(goals, 'judge_goal', lambda *a, **k: ('wait', 'cooldown', False, {'seconds': 1}, False))
        patch.setattr(GoalFencingMixin, '_save_owned', crash)
        with pytest.raises(Crash):
            mgr.evaluate_after_turn('work')


@pytest.mark.parametrize('surface', ['evaluation', 'cli', 'manager'])
def test_crashed_wait_owner_cannot_be_expired_automatically(monkeypatch, surface):
    mgr = goals.GoalManager('crashed-wait'); mgr.set('work')
    crash_after_wait(mgr, monkeypatch)
    before = readback(mgr.session_id)
    assert before.evaluation_id and before.waiting_until
    monkeypatch.setattr(goals.time, 'time', lambda: before.waiting_until + 1)
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('automatic judge'))
    monkeypatch.setattr(goals, 'run_gate', lambda *a, **k: pytest.fail('automatic gate'))
    other = goals.GoalManager(mgr.session_id)
    if surface == 'evaluation':
        assert not other.evaluate_after_turn('automatic wake')['should_continue']
    elif surface == 'cli':
        cli = cli_for(other)
        cli._maybe_resume_parked_goal()
        assert cli._pending_input.empty()
    else:
        assert other.is_waiting()
    assert readback(mgr.session_id).to_json() == before.to_json()
    other.resume()
    assert not readback(mgr.session_id).evaluation_id
    assert readback(mgr.session_id).generation == before.generation + 1


@pytest.mark.parametrize('surface', ['evaluation', 'cli', 'manager'])
def test_claim_arriving_during_expiry_cannot_be_cleared(monkeypatch, surface):
    mgr = goals.GoalManager('claim-race'); mgr.set('work'); mgr.wait_on(12345)
    before = readback(mgr.session_id)
    owner = str(uuid.uuid4())
    def process_exited(pid):
        # A different actor's native claim commits after expiry read, before its write.
        state, raw = goals._read_goal(mgr.session_id)
        state.evaluation_id = owner
        state.turns_used += 1
        goals._cas_goal(mgr.session_id, raw, state)
        return False
    monkeypatch.setattr(goals, '_pid_alive', process_exited)
    monkeypatch.setattr(goals, 'judge_goal', lambda *a, **k: pytest.fail('new owner already exists'))
    if surface == 'evaluation':
        assert not mgr.evaluate_after_turn('wake')['should_continue']
    elif surface == 'cli':
        cli = cli_for(mgr); cli._maybe_resume_parked_goal()
        assert cli._pending_input.empty()
    else:
        assert mgr.is_waiting()
    after = readback(mgr.session_id)
    assert after.evaluation_id == owner and after.generation == before.generation
    assert after.waiting_on_pid == 12345 and after.turns_used == 1


@pytest.mark.parametrize('boundary', ['before_claim', 'cas_retry'])
def test_wait_settlement_on_any_claim_snapshot_parks_without_work(monkeypatch, boundary):
    mgr = goals.GoalManager('wait-race'); mgr.set('work'); mgr.add_gate('true')
    calls = []
    def judge(*a, **k):
        calls.append('judge')
        return 'wait', 'cooldown', False, {'seconds': 60}, False
    def gate(*a, **k):
        calls.append('gate')
        return True, 0, 'pass'
    monkeypatch.setattr(goals, 'judge_goal', judge)
    monkeypatch.setattr(goals, 'run_gate', gate)
    interleaved = []
    def settle_wait():
        interleaved.append(True)
        assert goals.GoalManager(mgr.session_id).evaluate_after_turn('first')['verdict'] == 'wait'
    if boundary == 'before_claim':
        original = goals.GoalManager.is_waiting
        def check(self):
            answer = original(self)
            if not interleaved:
                settle_wait()
            return answer
        monkeypatch.setattr(goals.GoalManager, 'is_waiting', check)
    else:
        original = goals._cas_goal
        def cas(sid, raw, state):
            if state.evaluation_id and not interleaved:
                settle_wait()
            return original(sid, raw, state)
        monkeypatch.setattr(goals, '_cas_goal', cas)
    result = mgr.evaluate_after_turn('second')
    assert result['verdict'] == 'waiting' and not result['should_continue']
    assert calls == ['gate', 'judge']
    state = readback(mgr.session_id)
    assert state.turns_used == 1 and state.waiting_until and not state.evaluation_id


def test_unclaimed_expiry_preserves_generation_and_explicit_unwait_revokes_owner(monkeypatch):
    mgr = goals.GoalManager('ordinary'); mgr.set('work'); mgr.wait_for_seconds(1)
    before = readback(mgr.session_id)
    monkeypatch.setattr(goals.time, 'time', lambda: before.waiting_until + 1)
    cli = cli_for(mgr); cli._maybe_resume_parked_goal()
    assert 'work' in cli._pending_input.get_nowait()
    after = readback(mgr.session_id)
    assert after.generation == before.generation and after.turns_used == 0 and not after.waiting_until
    mgr.wait_for_seconds(10)
    state, raw = goals._read_goal(mgr.session_id)
    state.evaluation_id = str(uuid.uuid4())
    goals._cas_goal(mgr.session_id, raw, state)
    assert mgr.stop_waiting()
    after = readback(mgr.session_id)
    assert after.generation == state.generation + 1 and not after.evaluation_id and not after.waiting_until
