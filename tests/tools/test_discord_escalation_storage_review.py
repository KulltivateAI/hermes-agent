"""R2 independent edge probes; public registry and real SQLite, no sender mocks."""
import copy
import json
import sqlite3

import pytest

from tests.tools.test_discord_escalation import Remote, call, config, receipt, phase_of, DB_NAME
from tools import discord_tool as dt


@pytest.fixture
def wire(tmp_path, monkeypatch):
    config(tmp_path)
    api = Remote()
    monkeypatch.setattr(dt, '_discord_request', api.request)
    return api


def update(home, sql, params=()):
    conn = sqlite3.connect(home / DB_NAME)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def adopt(wire):
    wire.threads['9000'] = dict(id='9000', type=11, parent_id='100', guild_id='999',
                               thread_metadata={'archived': False, 'locked': False})
    return dict(name='', summary=None, body=None, existing_thread_id='9000')


def test_adopt_stored_thread_must_match_immutable_input(tmp_path, wire):
    args = adopt(wire)
    assert call(tmp_path, **args)['status'] == 'thread_ready'
    wire.threads['9001'] = {**copy.deepcopy(wire.threads['9000']), 'id': '9001'}
    update(tmp_path, 'UPDATE escalation_receipts SET thread_id=?', ('9001',))
    before = receipt(tmp_path)
    start = len(wire.calls)
    result = call(tmp_path, **args)
    print('ADOPT_MISMATCH_RESULT', json.dumps(result, sort_keys=True))
    print('ADOPT_MISMATCH_CALLS', wire.calls[start:])
    assert result.get('error_code') == 'storage_failed', result
    assert wire.calls[start:] == []
    assert receipt(tmp_path) == before


@pytest.mark.parametrize('stage', ['anchor_pending', 'thread_pending', 'members_pending', 'body_pending'])
def test_proven_body_in_prebody_stage_is_corruption(tmp_path, wire, stage):
    first = call(tmp_path)
    assert first['status'] == 'delivered'
    update(tmp_path, 'UPDATE escalation_receipts SET stage=?', (stage,))
    before = receipt(tmp_path)
    start = len(wire.calls)
    result = call(tmp_path)
    print('STAGE_CONTRADICTION', stage, json.dumps(result, sort_keys=True))
    print('STAGE_POSTS', [c for c in wire.calls[start:] if c[0] == 'POST'])
    assert result.get('error_code') == 'storage_failed', result
    assert wire.calls[start:] == []
    assert receipt(tmp_path) == before


@pytest.mark.parametrize("mode", ["create", "adopt"])
def test_cross_field_corruption_on_cas_reload_stops_without_further_io(tmp_path, wire, monkeypatch, mode):
    args = adopt(wire) if mode == "adopt" else {}
    assert call(tmp_path, **args)["success"]
    original = wire.request
    corrupted = []
    def race(method, path, token, **kw):
        response = original(method, path, token, **kw)
        if not corrupted and method == "GET" and path.endswith("/thread-members/44"):
            if mode == "adopt":
                update(tmp_path, "UPDATE escalation_receipts SET revision=revision+1,thread_id='9001'")
            else:
                update(tmp_path, "UPDATE escalation_receipts SET revision=revision+1,stage='body_pending'")
            corrupted.append((receipt(tmp_path), len(wire.calls)))
        return response
    monkeypatch.setattr(dt, "_discord_request", race)
    result = call(tmp_path, **args)
    assert corrupted
    assert result["error_code"] == "storage_failed", result
    assert not result["success"] and not result["body_delivered"]
    assert wire.calls[corrupted[0][1]:] == []
    assert receipt(tmp_path) == corrupted[0][0]


@pytest.mark.parametrize('phase', ['anchor', 'thread', 'body'])
def test_cap_rejects_before_mutation_each_rejected_phase(tmp_path, wire, phase):
    config(tmp_path, observers=tuple(range(50, 63)))
    def reject(method, path, token, body):
        if phase_of(method, path) == phase:
            raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
    wire.hook = reject
    assert call(tmp_path)['stage'] == phase + '_rejected'
    before = receipt(tmp_path)
    config(tmp_path, observers=(99,))
    wire.hook = None
    start = len(wire.calls)
    result = call(tmp_path, retry=True)
    assert result['error_code'] == 'invalid_policy'
    assert receipt(tmp_path) == before
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[start:])


@pytest.mark.parametrize('mode', ['create', 'adopt'])
def test_initial_completion_cannot_use_stale_union(tmp_path, wire, monkeypatch, mode):
    args = adopt(wire) if mode == 'adopt' else {}
    original = wire.request
    entered = False
    nested = []
    def racing(method, path, token, **kw):
        nonlocal entered
        if not entered and method == 'GET' and '/thread-members/' in path:
            entered = True
            config(tmp_path, observers=(33, 66))
            def fail_new(m, p, t, body):
                if m == 'PUT' and p.endswith('/66'):
                    raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
            wire.hook = fail_new
            nested.append(call(tmp_path, **args))
            wire.hook = None
        return original(method, path, token, **kw)
    monkeypatch.setattr(dt, '_discord_request', racing)
    result = call(tmp_path, **args)
    assert nested[0]['error_code'] == 'membership_failed'
    assert not result['success'] and result['error_code'] == 'concurrent_update'
    assert '66' in result['required_member_ids']
    assert not any(phase_of(c[0], c[1]) == 'body' for c in wire.calls)
    assert call(tmp_path, **args)['error_code'] == 'retry_required'
    good = call(tmp_path, retry=True, **args)
    assert good['success'] and '66' in good['verified_member_ids']


@pytest.mark.parametrize('phase', ['anchor', 'thread', 'body'])
def test_real_ambiguous_candidate_receipt_is_valid(tmp_path, wire, monkeypatch, phase):
    original = wire.request
    def ambiguous(method, path, token, **kw):
        obj = original(method, path, token, **kw)
        if phase_of(method, path) == phase:
            if phase == 'thread':
                obj['type'] = 12
            else:
                obj['author']['id'] = '55'
        return obj
    monkeypatch.setattr(dt, '_discord_request', ambiguous)
    first = call(tmp_path)
    assert first['status'] == 'reconciliation_required'
    row = receipt(tmp_path)
    assert row['stage'] == phase + '_ambiguous'
    start = len(wire.calls)
    again = call(tmp_path, retry=True)
    assert again['error_code'] == 'ambiguous_post', again
    assert receipt(tmp_path) == row
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[start:])


@pytest.mark.parametrize('column,value', [
    ('retry_not_before', b'bad'), ('created_at', float('inf')),
    ('updated_at', b'bad'), ('revision', 0.5), ('last_http_status', 600),
    ('body_message_id', b'1002'), ('required_members_json', '[null]'),
    ('input_json', '[]'), ('stage', b'complete'),
])
def test_additional_scalar_corruption_no_io(tmp_path, wire, column, value):
    assert call(tmp_path)['success']
    conn = sqlite3.connect(tmp_path / DB_NAME)
    try:
        conn.execute('PRAGMA ignore_check_constraints=ON')
        conn.execute(f'UPDATE escalation_receipts SET {column}=?', (value,))
        conn.commit()
    finally:
        conn.close()
    before = receipt(tmp_path)
    start = len(wire.calls)
    result = call(tmp_path)
    assert result.get('error_code') == 'storage_failed', result
    assert not result['success'] and not result['body_delivered']
    assert wire.calls[start:] == []
    assert receipt(tmp_path) == before
