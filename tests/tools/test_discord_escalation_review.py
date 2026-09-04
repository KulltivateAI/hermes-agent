"""Independent C2 review: public registry calls + actual SQLite, wire boundary only."""
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
    with sqlite3.connect(home / DB_NAME) as conn:
        conn.execute(sql, params)
    conn.close()


def test_malformed_rate_storage_returns_structured_storage_failure(tmp_path, wire):
    assert call(tmp_path)['status'] == 'delivered'
    update(tmp_path, 'UPDATE escalation_receipts SET retry_not_before=?', ('invalid',))
    before = len(wire.calls)
    result = call(tmp_path)
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[before:])
    assert result.get('error_code') == 'storage_failed', result


def test_malformed_stored_body_id_cannot_claim_reused_delivery(tmp_path, wire):
    assert call(tmp_path)['status'] == 'delivered'
    update(tmp_path, 'UPDATE escalation_receipts SET body_message_id=?', ('not-a-snowflake',))
    before = len(wire.calls)
    result = call(tmp_path)
    assert result.get('error_code') == 'storage_failed', result
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[before:])


def test_adoption_replay_inactive_preserves_known_receipt_and_retry_gate(tmp_path, wire):
    wire.threads['9000'] = dict(id='9000', type=11, parent_id='100', guild_id='999',
                                thread_metadata={'archived': False, 'locked': False})
    args = dict(name='', summary=None, body=None, existing_thread_id='9000')
    assert call(tmp_path, **args)['status'] == 'thread_ready'
    wire.threads['9000']['thread_metadata']['archived'] = True
    result = call(tmp_path, **args)
    assert result['error_code'] == 'thread_inactive'
    failed_row = receipt(tmp_path)
    wire.threads['9000']['thread_metadata']['archived'] = False
    unapproved_resume = call(tmp_path, **args)
    assert result['thread_id'] == '9000' and result['stage'] == 'complete' and result['receipt_persisted'], (result, failed_row, unapproved_resume)
    assert failed_row['last_error_code'] == 'membership_failed'
    assert unapproved_resume.get('error_code') == 'retry_required', unapproved_resume


def test_overflow_observer_union_does_not_consume_rejection_retry(tmp_path, wire):
    config(tmp_path, observers=tuple(range(50, 63)))  # 16 incl sender/target/requester
    def reject(method, path, token, body):
        if phase_of(method, path) == 'body':
            raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
    wire.hook = reject
    assert call(tmp_path)['stage'] == 'body_rejected'
    before = receipt(tmp_path)
    config(tmp_path, observers=(99,))  # individual policy valid, retained union >16
    wire.hook = None
    start = len(wire.calls)
    result = call(tmp_path, retry=True)
    assert result['error_code'] == 'invalid_policy', result
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[start:])
    assert receipt(tmp_path) == before, (before, receipt(tmp_path))


def test_complete_replay_does_not_succeed_with_stale_obligations(tmp_path, wire, monkeypatch):
    assert call(tmp_path)['status'] == 'delivered'
    original = wire.request
    nested = []
    entered = False
    def racing(method, path, token, **kwargs):
        nonlocal entered
        if not entered and method == 'GET' and '/thread-members/' in path:
            entered = True
            config(tmp_path, observers=(33, 66))
            def fail_new_observer(m, p, t, body):
                if m == 'PUT' and p.endswith('/66'):
                    raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
            wire.hook = fail_new_observer
            nested.append(call(tmp_path))
            wire.hook = None
        return original(method, path, token, **kwargs)
    monkeypatch.setattr(dt, '_discord_request', racing)
    result = call(tmp_path)
    assert nested[0]['error_code'] == 'membership_failed', nested
    retained = set(json.loads(receipt(tmp_path)['required_members_json']))
    print('STALE_REPLAY_RESULT', json.dumps(result, sort_keys=True))
    print('CURRENT_RECEIPT', json.dumps(receipt(tmp_path), sort_keys=True))
    assert not result['success'] or retained <= set(result['verified_member_ids']), (result, receipt(tmp_path))




@pytest.mark.parametrize("damage", ["archived", "locked", "deleted", "forbidden"])
def test_existing_adopt_failure_and_explicit_repair(tmp_path, wire, damage):
    thread = dict(id="9000", type=11, parent_id="100", guild_id="999",
                  thread_metadata={"archived": False, "locked": False})
    wire.threads["9000"] = thread
    args = dict(name="", summary=None, body=None, existing_thread_id="9000")
    assert call(tmp_path, **args)["status"] == "thread_ready"
    if damage in ("archived", "locked"):
        thread["thread_metadata"][damage] = True
    elif damage == "deleted":
        del wire.threads["9000"]
    else:
        def denied(method, path, token, body):
            if method == "GET" and path == "/channels/9000":
                raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
        wire.hook = denied
    before = len(wire.calls)
    failed = call(tmp_path, **args)
    assert not failed["success"] and not failed["body_delivered"]
    assert failed["stage"] == "complete" and failed["thread_id"] == "9000"
    assert failed["receipt_persisted"]
    assert receipt(tmp_path)["last_error_code"] == "membership_failed"
    assert not any(c[0] in ("PUT", "POST") for c in wire.calls[before:])
    thread["thread_metadata"] = {"archived": False, "locked": False}
    wire.threads["9000"] = thread
    wire.hook = None
    before = len(wire.calls)
    assert call(tmp_path, **args)["error_code"] == "retry_required"
    assert not any(c[0] in ("PUT", "POST") for c in wire.calls[before:])
    repaired = call(tmp_path, retry=True, **args)
    assert repaired["status"] == "thread_ready"
    assert repaired["required_member_ids"] == repaired["verified_member_ids"]
    assert not any(c[0] == "POST" for c in wire.calls)


def test_adopt_complete_replay_rejects_stale_observations(tmp_path, wire, monkeypatch):
    wire.threads["9000"] = dict(id="9000", type=11, parent_id="100", guild_id="999",
                                thread_metadata={"archived": False, "locked": False})
    args = dict(name="", summary=None, body=None, existing_thread_id="9000")
    assert call(tmp_path, **args)["status"] == "thread_ready"
    original = wire.request
    entered = False
    def racing(method, path, token, **kwargs):
        nonlocal entered
        if not entered and method == "GET" and "/thread-members/" in path:
            entered = True
            config(tmp_path, observers=(33, 66))
            def failed(method, path, token, body):
                if method == "PUT" and path.endswith("/66"):
                    raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
            wire.hook = failed
            assert call(tmp_path, **args)["error_code"] == "membership_failed"
            wire.hook = None
        return original(method, path, token, **kwargs)
    monkeypatch.setattr(dt, "_discord_request", racing)
    result = call(tmp_path, **args)
    assert result["status"] == "pending" and result["error_code"] == "concurrent_update"
    assert not result["success"] and "66" in result["required_member_ids"]
    assert "66" not in result["verified_member_ids"]
    # Opposite order: after the failed update, plain retry remains gated; an
    # explicit retry verifies the new union and may safely claim readiness.
    assert call(tmp_path, **args)["error_code"] == "retry_required"
    assert call(tmp_path, retry=True, **args)["status"] == "thread_ready"
    assert not any(c[0] == "POST" for c in wire.calls)


@pytest.mark.parametrize("column,value", [
    ("body_message_id", "bad"), ("body_message_id", "0001002"),
    ("retry_not_before", "invalid"), ("retry_not_before", float("inf")),
    ("created_at", "invalid"), ("updated_at", -1),
    ("revision", "invalid"), ("last_http_status", "invalid"),
    ("last_error_code", "invalid"),
])
def test_operational_corruption_is_structured_before_remote_io(tmp_path, wire, column, value):
    original = call(tmp_path)
    update(tmp_path, f"UPDATE escalation_receipts SET {column}=?", (value,))
    row = receipt(tmp_path)
    before = len(wire.calls)
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed", result
    assert set(original) <= set(result)
    assert not result["success"] and not result["body_delivered"]
    assert result["stage"] == "complete" and result["thread_id"] == original["thread_id"]
    assert result["anchor_message_id"] == original["anchor_message_id"]
    assert result["next_action"] == "repair_storage"
    assert wire.calls[before:] == []
    assert receipt(tmp_path) == row


def test_corrupt_cas_reload_is_structured(tmp_path, wire, monkeypatch):
    assert call(tmp_path)["status"] == "delivered"
    original = wire.request
    changed = False
    def corrupt(method, path, token, **kw):
        nonlocal changed
        if not changed and method == "GET" and "/thread-members/" in path:
            changed = True
            update(tmp_path, "UPDATE escalation_receipts SET revision=revision+1,retry_not_before='bad'")
        return original(method, path, token, **kw)
    monkeypatch.setattr(dt, "_discord_request", corrupt)
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed", result
    assert not result["success"] and not result["body_delivered"]
    assert result["stage"] == "complete"


@pytest.mark.parametrize('phase', ['anchor', 'thread', 'body'])
def test_real_sql_rejection_persistence_failure_keeps_claim(tmp_path, wire, phase):
    def fault(method, path, token, body):
        if phase_of(method, path) == phase:
            row = receipt(tmp_path)
            assert row['stage'] == phase + '_inflight'  # separate connection sees committed claim
            update(tmp_path, "CREATE TRIGGER deny_rejection BEFORE UPDATE ON escalation_receipts "
                   "WHEN NEW.stage LIKE '%_rejected' BEGIN SELECT RAISE(ABORT, 'disk fault'); END")
            raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
    wire.hook = fault
    first = call(tmp_path)
    assert first['status'] == 'reconciliation_required' and not first['retryable'], first
    assert first['error_code'] == 'storage_failed' and not first['receipt_persisted']
    assert receipt(tmp_path)['stage'] == phase + '_inflight'
    update(tmp_path, 'DROP TRIGGER deny_rejection')
    wire.hook = None
    before = len(wire.calls)
    again = call(tmp_path, retry=True)
    assert again['status'] == 'reconciliation_required', again
    assert not any(c[0] in ('POST', 'PUT') for c in wire.calls[before:])
