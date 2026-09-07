"""Goal control transactions and evaluation/delivery ownership.

No locks span shell/provider work. All retries here recompute persistence only.
Messaging gateway admission adopts these helpers in the sequential gateway slice.
"""
from copy import deepcopy
from functools import wraps
import uuid


def identity(state):
    return (state.goal_id, state.generation) if state else None


def goal_control(method):
    """Stage a pure control on a fresh row, then CAS once per recomputation."""
    @wraps(method)
    def control(self, *args, **kwargs):
        from hermes_cli import goals as g
        self._check_store()
        before = identity(self._state)
        indexed = method.__name__ not in {'set', 'pause', 'clear'}
        try:
            for _ in range(3):
                state, raw = g._read_goal(self.session_id)
                if indexed and identity(state) != before:
                    raise g.GoalConflict('goal control changed; reload before editing indexed state')
                self._state = state
                self._control_candidate = None
                self._staging_control = True
                try:
                    result = method(self, *args, **kwargs)
                finally:
                    self._staging_control = False
                candidate = self._control_candidate
                if candidate is None:
                    return result
                candidate.goal_id = (candidate.goal_id if method.__name__ == 'set' else
                                     (state.goal_id if state else '')) or str(uuid.uuid4())
                # state may have been mutated by the method, but not its control generation.
                candidate.generation = (g.GoalState.from_json(raw).generation if raw is not None else 0) + 1
                candidate.evaluation_id = candidate.continuation_id = candidate.notice_id = ''
                try:
                    g._cas_goal(self.session_id, raw, candidate)
                    return result
                except g.GoalConflict:
                    # Only evaluation/delivery churn may be recomputed, never a different edit.
                    current, _ = g._read_goal(self.session_id)
                    baseline = identity(g.GoalState.from_json(raw)) if raw is not None else None
                    if identity(current) != baseline:
                        raise
            raise g.GoalConflict('goal busy after three conditional writes; retry control')
        except Exception:
            self._state = g.load_goal(self.session_id)
            raise
    return control


class GoalFencingMixin:
    def _check_store(self):
        from hermes_cli import goals as g
        if self._store != g._store_identity():
            raise g.GoalConflict('manager belongs to another profile store')

    def _owned_row(self):
        from hermes_cli import goals as g
        self._check_store()
        current, raw = g._read_goal(self.session_id)
        if not current or (*identity(current), current.evaluation_id) != self._evaluation_owner:
            raise g.GoalConflict('evaluation superseded by a control or another owner')
        return current, raw

    def _save_owned(self, decision=None):
        from hermes_cli import goals as g
        candidate = deepcopy(self._state)
        for _ in range(3):
            latest, raw = self._owned_row()
            # Only delivery consumption can coexist with this owner. Never resurrect its tokens.
            candidate.continuation_id = latest.continuation_id
            candidate.notice_id = latest.notice_id
            if decision is not None:
                candidate.evaluation_id = ''
                candidate.continuation_id = str(uuid.uuid4()) if decision['should_continue'] else ''
                candidate.notice_id = str(uuid.uuid4()) if decision['message'] else ''
            try:
                g._cas_goal(self.session_id, raw, candidate)
            except g.GoalConflict:
                continue
            self._state.__dict__.update(candidate.__dict__)
            if decision is not None:
                decision['goal_fence'] = self._fence(candidate)
            return self._state
        # Distinguish delivery churn from lost ownership (the latter must be silent/inert).
        self._owned_row()
        raise g.GoalSettlementBusy('evaluation settlement busy after three CAS attempts')

    def _fence(self, state):
        return dict(session_id=self.session_id, goal_id=state.goal_id, generation=state.generation,
                    continuation_id=state.continuation_id, notice_id=state.notice_id)

    def _matches_fence(self, state, fence, token):
        return (isinstance(fence, dict) and state is not None and
                fence.get('session_id') == self.session_id and
                isinstance(fence.get('goal_id'), str) and bool(fence['goal_id']) and
                type(fence.get('generation')) is int and
                identity(state) == (fence['goal_id'], fence['generation']) and
                isinstance(fence.get(token), str) and bool(fence[token]) and
                getattr(state, token) == fence[token] and
                (token != 'continuation_id' or state.status == 'active'))

    def continuation_pending(self, fence):
        from hermes_cli import goals as g
        try:
            self._check_store()
            state, _ = g._read_goal(self.session_id)
            return self._matches_fence(state, fence, 'continuation_id')
        except g.GoalPersistenceError:
            return False

    def _consume_delivery(self, fence, token):
        from hermes_cli import goals as g
        self._check_store()
        for _ in range(3):
            state, raw = g._read_goal(self.session_id)
            if not self._matches_fence(state, fence, token):
                return False
            setattr(state, token, '')
            try:
                g._cas_goal(self.session_id, raw, state)
                return True
            except g.GoalConflict:
                continue
        raise g.GoalPersistenceError('delivery claim busy; no admission, explicit resume required')

    def consume_continuation(self, fence):
        """True admits exactly one event; False is stale/duplicate/malformed. Errors fail closed."""
        return self._consume_delivery(fence, 'continuation_id')

    def consume_notice(self, fence):
        """Claim immediately before send. An ambiguous send is never retried."""
        return self._consume_delivery(fence, 'notice_id')

    def reserve_continuation(self):
        from hermes_cli import goals as g
        self._check_store()
        if not self.is_active():
            return None
        candidate = deepcopy(self._state)
        provenance = getattr(candidate, '_goal_provenance', None)
        if not provenance or provenance[:2] != (self._store, self.session_id):
            return None
        candidate.continuation_id = str(uuid.uuid4())
        try:
            g._cas_goal(self.session_id, provenance[2], candidate)
        except g.GoalConflict:
            return None
        self._state = candidate
        return self._fence(candidate)

    def _run_fenced_evaluation(self, last_response, **kwargs):
        from hermes_cli import goals as g
        self._check_store()
        worker = g.GoalManager(self.session_id, default_max_turns=self.default_max_turns)
        worker._evaluation_owner = None
        try:
            state, _ = g._read_goal(self.session_id)
            worker._state = state
            if not worker.is_active():
                return g._decision(state.status if state else None, False, None, 'inactive', 'no active goal', '')
            if worker.is_waiting():
                return worker._waiting_decision(worker.state)
            after_wait = identity(worker.state)
            state, raw = g._read_goal(self.session_id)
            worker._state = state
            if identity(state) != after_wait:
                raise g.GoalConflict('goal changed while resolving wait')
            if not worker.is_active():
                raise g.GoalConflict('goal changed while resolving wait')
            expected = identity(state)
            for _ in range(3):
                if identity(state) != expected:
                    raise g.GoalConflict('goal changed before evaluation claim')
                if state.evaluation_id:
                    return g._decision(state.status, False, None, 'claimed', 'evaluation already claimed; explicit resume recovers interruption', '')
                state.goal_id = state.goal_id or str(uuid.uuid4())
                state.evaluation_id = str(uuid.uuid4())
                if state.turns_used < state.max_turns:
                    state.turns_used += 1
                    state.last_turn_at = g.time.time()
                    exhausted = False
                else:
                    exhausted = True
                try:
                    g._cas_goal(self.session_id, raw, state)
                    worker._state = state
                    worker._evaluation_owner = (*identity(state), state.evaluation_id)
                    break
                except g.GoalConflict:
                    state, raw = g._read_goal(self.session_id)
            else:
                raise g.GoalConflict('evaluation claim lost')
            decision = (worker._budget_pause(state, 'budget', 'turn allowance already exhausted') if exhausted else
                        worker._evaluate_claimed(last_response, **kwargs))
            worker._save_owned(decision)
            return decision
        except g.GoalConflict:
            return g._decision(None, False, None, 'stale', 'goal changed', '')
        except g.GoalPersistenceError as exc:
            if not isinstance(exc, g.GoalSettlementBusy):
                return g._decision('unknown', False, None, 'interrupted', str(exc),
                    f'Goal persistence interrupted/unknown: {exc}. Claim may remain; inspect status before explicit resume.')
            return worker._interrupt_evaluation(exc)
        except Exception as exc:
            return worker._interrupt_evaluation(exc)
        finally:
            self._state = g.load_goal(self.session_id)

    def _interrupt_evaluation(self, exc):
        from hermes_cli import goals as g
        worker = self
        # Release only our own claim after an ordinary exception. A crash retains it.
        message = f'Goal evaluation interrupted: {exc}. Inspect status; explicit /goal resume required.'
        decision = g._decision('paused', False, None, 'interrupted', str(exc), message)
        if worker._evaluation_owner:
            try:
                worker._owned_row()
                worker._state.status = 'paused'
                worker._state.paused_reason = message
                worker._save_owned(decision)
            except g.GoalConflict:
                return g._decision(None, False, None, 'stale', 'goal changed', '')
            except g.GoalPersistenceError as error:
                decision['status'] = 'unknown'
                decision['message'] += f' Persistence unknown; claim may remain: {error}'
        return decision
