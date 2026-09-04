"""Requester-owned public entrypoint and real profile-local receipt proofs."""
import copy
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from io import BytesIO

import pytest

from agent.secret_scope import reset_secret_scope, set_secret_scope
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import discord_tool as dt
from tools.registry import registry
from discord_escalation_protocol import is_trusted_nonconversational_message

ARGS = dict(action="create_thread", channel_id="100", name="Issue review",
            for_agent="22", issue_key="ticket:c2", summary="Needs review",
            body="  Exact café\nhttps://example.invalid/evidence  ", requester_id="44")
DB_NAME = "discord_escalation_receipts.db"


@contextmanager
def profile(home, token="requestor-token"):
    h = set_hermes_home_override(home)
    s = set_secret_scope({"DISCORD_BOT_TOKEN": token})
    try:
        yield
    finally:
        reset_secret_scope(s)
        reset_hermes_home_override(h)


def config(home, observers=(33,), enabled=True):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(json.dumps({"discord": {
        "escalation_threads": {"enabled": enabled, "observer_ids": list(observers)}
    }}), encoding="utf-8")


def call(home, token="requestor-token", **changes):
    with profile(home, token):
        return json.loads(registry._tools["discord"].handler({**ARGS, **changes}))


def receipt(home):
    with sqlite3.connect(home / DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM escalation_receipts").fetchone())
    conn.close()
    return row


class Remote:
    """Stateful Discord boundary: records actual writes and read-after-write membership."""
    def __init__(self):
        self.calls = []
        self.messages = {}
        self.members = set()
        self.threads = {}
        self.identities = {"requestor-token": "11", "second-token": "55", "rotated-token": "11"}
        self.hook = None
        self.counter = 1000

    def request(self, method, path, token, params=None, body=None, timeout=15):
        self.calls.append((method, path, token, copy.deepcopy(body)))
        if self.hook:
            self.hook(method, path, token, body)
        sender = self.identities[token]
        if path == "/users/@me":
            return {"id": sender, "bot": True}
        if path == "/channels/100":
            return {"id": "100", "guild_id": "999", "type": 0}
        parts = path.split("/")
        if "thread-members" in parts:
            thread, member = parts[2], parts[-1]
            if member == "@me":
                member = sender
            if method == "PUT":
                self.members.add((thread, member))
                return None
            if (thread, member) not in self.members:
                raise dt.DiscordAPIError(404, '{"code":10007,"message":"Unknown Member"}')
            return {"id": thread, "user_id": member}
        if method == "POST" and path.endswith("/threads"):
            # Anchored ONLY. A standalone ordinary call remains testable but differs.
            thread = parts[-2] if "messages" in parts else "8000"
            obj = dict(id=thread, type=11, parent_id="100", guild_id="999",
                       name=body["name"], thread_metadata={"archived": False, "locked": False})
            self.threads[thread] = obj
            return copy.deepcopy(obj)
        if method == "POST" and path.endswith("/messages"):
            self.counter += 1
            obj = dict(id=str(self.counter), channel_id=parts[2], type=0,
                       author={"id": sender, "bot": True}, **copy.deepcopy(body))
            self.messages[(parts[2], obj["id"])] = obj
            return copy.deepcopy(obj)
        if method == "GET" and "messages" in parts:
            return copy.deepcopy(self.messages[(parts[2], parts[-1])])
        if method == "GET" and path.startswith("/channels/"):
            return copy.deepcopy(self.threads[parts[2]])
        raise AssertionError((method, path))

    def urlopen(self, req, timeout):
        token = req.get_header("Authorization").removeprefix("Bot ")
        payload = json.loads(req.data) if req.data else None
        data = self.request(req.method, req.full_url.removeprefix(dt.DISCORD_API_BASE),
                            token, body=payload, timeout=timeout)
        response = BytesIO(json.dumps(data).encode())
        response.status = 204 if data is None else 200
        return response


@pytest.fixture
def remote(tmp_path, monkeypatch):
    config(tmp_path)
    api = Remote()
    monkeypatch.setattr(dt, "_discord_request", api.request)
    return api


def test_public_create_replay_and_requester_owned_wire(tmp_path, monkeypatch):
    api = Remote()
    monkeypatch.setattr(dt.urllib.request, "urlopen", api.urlopen)
    for home, token, sender in [(tmp_path / "first", "requestor-token", "11"),
                                (tmp_path / "second", "second-token", "55")]:
        config(home)
        start = len(api.calls)
        first = call(home, token)
        assert first.get("status") == "delivered", first
        assert first["success"] and first["body_delivered"] and first["receipt_persisted"]
        expected_members = sorted({sender, "22", "33", "44"})
        assert first["verified_member_ids"] == first["required_member_ids"] == expected_members
        writes = [c for c in api.calls[start:] if c[0] == "POST"]
        assert len(writes) == 3
        anchor, thread, body = writes
        assert anchor[1] == "/channels/100/messages"
        assert anchor[3]["content"] == ""
        assert anchor[3]["allowed_mentions"] == {"parse": []}
        captured = api.messages[("100", first["anchor_message_id"])]
        assert is_trusted_nonconversational_message(captured, {sender})
        assert not is_trusted_nonconversational_message(captured, {"untrusted"})
        assert not is_trusted_nonconversational_message({**captured, "author": {"id": sender, "bot": False}}, {sender})
        assert thread[1] == f'/channels/100/messages/{first["anchor_message_id"]}/threads'
        assert thread[3] == {"name": ARGS["name"], "auto_archive_duration": 1440}
        exact = "<@22>\n" + ARGS["body"]
        assert body[1] == f'/channels/{first["thread_id"]}/messages'
        assert body[3] == {"content": exact, "allowed_mentions": {
            "parse": [], "users": ["22"], "replied_user": False}}
        assert not is_trusted_nonconversational_message(api.messages[(first["thread_id"], first["body_message_id"])], {sender})
        assert all(c[2] == token for c in api.calls[start:])
        row = receipt(home)
        assert row["stage"] == "complete" and row["sender_id"] == sender
        assert row["body_sha256"] == hashlib.sha256(exact.encode()).hexdigest()
        assert ARGS["body"] not in json.dumps(row) and token not in json.dumps(row)
        before = len(writes)
        replay = call(home, token)
        assert replay["status"] == "reused" and replay["body_message_id"] == first["body_message_id"]
        assert len([c for c in api.calls[start:] if c[0] == "POST"]) == before




def _process_create(home, phase, entered, release, results):
    # Spawned processes use their own imported public registry and real DB.
    api = Remote()
    def pause(method, path, token, body):
        if phase_of(method, path) == phase:
            entered.set()
            if not release.wait(15):
                raise TimeoutError("test owner wait exceeded")
    api.hook = pause
    dt._discord_request = api.request
    results.put((call(home), api.calls))


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
def test_processes_cannot_steal_committed_inflight_claim(tmp_path, remote, phase):
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    entered, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
    owner = ctx.Process(target=_process_create, args=(tmp_path, phase, entered, release, results))
    owner.start()
    try:
        assert entered.wait(10)
        assert receipt(tmp_path)["stage"] == phase + "_inflight"
        loser = call(tmp_path, retry=True)
        assert loser["status"] == "reconciliation_required", loser
        assert not any(c[0] in ("POST", "PUT") for c in remote.calls)
        # Another issue is not blocked by a transaction spanning network I/O.
        other = call(tmp_path, issue_key="independent")
        assert other["status"] == "delivered", other
        release.set()
        completed, calls = results.get(timeout=10)
        assert completed["status"] == "delivered", completed
        assert len([c for c in calls if phase_of(c[0], c[1]) == phase]) == 1
    finally:
        release.set()
        owner.join(10)
        if owner.is_alive():
            owner.terminate()
            owner.join()
    assert owner.exitcode == 0


def test_thread_ack_with_partial_metadata_gets_confirmed(tmp_path, remote, monkeypatch):
    real = remote.request
    def partial(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if phase_of(method, path) == "thread":
            obj["thread_metadata"] = {"archived": False}
        return obj
    monkeypatch.setattr(dt, "_discord_request", partial)
    result = call(tmp_path)
    assert result["status"] == "delivered", result
    assert ("GET", f'/channels/{result["thread_id"]}') in [(c[0], c[1]) for c in remote.calls]


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
def test_invalid_success_ids_never_mark_delivery(tmp_path, remote, monkeypatch, phase):
    real = remote.request
    def invalid(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if phase_of(method, path) == phase:
            obj["id"] = "999999"
            if phase != "thread":
                obj["author"]["id"] = "66"
        return obj
    monkeypatch.setattr(dt, "_discord_request", invalid)
    result = call(tmp_path)
    assert result["status"] == "reconciliation_required" and not result["body_delivered"]
    before = len(remote.calls)
    assert call(tmp_path, retry=True)["status"] == "reconciliation_required"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])


@pytest.mark.parametrize("mutation", ["author", "webhook", "content", "reference", "poll"])
def test_body_reconcile_all_proofs_atomic(tmp_path, remote, monkeypatch, mutation):
    real = remote.request
    def lost(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if phase_of(method, path) == "body":
            raise TimeoutError()
        return obj
    monkeypatch.setattr(dt, "_discord_request", lost)
    first = call(tmp_path)
    monkeypatch.setattr(dt, "_discord_request", real)
    key = next(k for k in remote.messages if k[0] != "100")
    obj = remote.messages[key]
    if mutation == "author":
        obj["author"]["id"] = "77"
    elif mutation == "webhook":
        obj["webhook_id"] = "12"
    elif mutation == "content":
        obj["content"] += " "
    elif mutation == "reference":
        obj["message_reference"] = {"message_id": "12"}
    else:
        obj["poll"] = {"question": "more"}
    row = receipt(tmp_path)
    before = len(remote.calls)
    result = call(tmp_path, reconcile={"anchor_message_id": first["anchor_message_id"],
        "thread_id": first["thread_id"], "body_message_id": key[1]})
    assert result["error_code"] == "reconcile_mismatch", result
    assert receipt(tmp_path) == row
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])


@pytest.mark.parametrize("field,value", [("required_members_json", "{}"), ("input_json", "[]"), ("input_json", "{}"),
                                         ("required_members_json", '["oops"]')])
def test_corrupt_receipt_is_storage_failure(tmp_path, remote, field, value):
    assert call(tmp_path)["status"] == "delivered"
    with sqlite3.connect(tmp_path / DB_NAME) as conn:
        conn.execute(f"UPDATE escalation_receipts SET {field}=?", (value,))
    conn.close()
    before = len(remote.calls)
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed", result
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])


def test_rate_limit_survives_policy_addition(tmp_path, remote, monkeypatch):
    from tools import discord_escalation as de
    clock = [1000.0]
    monkeypatch.setattr(de.time, "time", lambda: clock[0])
    def reject(method, path, token, body):
        if phase_of(method, path) == "body":
            raise dt.DiscordAPIError(429, '{"message":"limited","retry_after":5}')
    remote.hook = reject
    result = call(tmp_path)
    assert result["retry_after_seconds"] == 5
    row = receipt(tmp_path)
    config(tmp_path, observers=(33, 66))
    remote.hook = None
    before = len(remote.calls)
    assert call(tmp_path, retry=True)["error_code"] == "rate_limited"
    assert receipt(tmp_path) == row
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])
    clock[0] += 6
    assert call(tmp_path, retry=True)["status"] == "delivered"




def test_active_writer_cannot_overwrite_reconciled_complete(tmp_path, remote, monkeypatch):
    real = remote.request
    reconcile_result = []
    def reconcile_during_ack(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if phase_of(method, path) == "body":
            reconcile_result.append(call(tmp_path, reconcile={"body_message_id": obj["id"]}))
        return obj
    monkeypatch.setattr(dt, "_discord_request", reconcile_during_ack)
    result = call(tmp_path)
    assert reconcile_result[0]["status"] == "pending" and reconcile_result[0]["body_delivered"]
    assert result["status"] == "pending" and result["error_code"] == "concurrent_update"
    assert receipt(tmp_path)["stage"] == "complete"
    assert call(tmp_path)["status"] == "reused"
    assert len([c for c in remote.calls if c[0] == "POST"]) == 3


def test_crash_after_claim_before_post_never_reclaims(tmp_path, remote, monkeypatch):
    from tools import discord_escalation as de
    original = de.advance_receipt
    def crash(conn, row, **changes):
        result = original(conn, row, **changes)
        if changes.get("stage") == "anchor_inflight":
            raise SystemExit("simulated process exit after committed claim")
        return result
    monkeypatch.setattr(de, "advance_receipt", crash)
    with pytest.raises(SystemExit):
        call(tmp_path)
    assert receipt(tmp_path)["stage"] == "anchor_inflight"
    assert not any(c[0] == "POST" for c in remote.calls)
    monkeypatch.setattr(de, "advance_receipt", original)
    assert call(tmp_path, retry=True)["status"] == "reconciliation_required"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls)


@pytest.mark.parametrize("stage", ["anchor", "thread", "body"])
def test_failed_rejection_persistence_is_uncertain(tmp_path, remote, monkeypatch, stage):
    from tools import discord_escalation as de
    original = de.advance_receipt
    def broken(conn, row, **changes):
        if changes.get("stage") == stage + "_rejected":
            raise sqlite3.OperationalError("storage rejected update")
        return original(conn, row, **changes)
    monkeypatch.setattr(de, "advance_receipt", broken)
    def reject(method, path, token, body):
        if phase_of(method, path) == stage:
            raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
    remote.hook = reject
    result = call(tmp_path)
    assert result["status"] == "reconciliation_required" and not result["retryable"]
    assert receipt(tmp_path)["stage"] == stage + "_inflight"


@pytest.mark.parametrize("damage", ["unknown_version", "corrupt_bytes", "missing_column"])
def test_bad_storage_never_reinitialized(tmp_path, remote, damage):
    db = tmp_path / DB_NAME
    if damage == "corrupt_bytes":
        db.write_bytes(b"not-a-sqlite-database")
        before = db.read_bytes()
    else:
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version=" + ("99" if damage == "unknown_version" else "1"))
            conn.execute("CREATE TABLE escalation_receipts(parent_channel_id TEXT, issue_key TEXT)")
        conn.close()
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed", result
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls)
    if damage == "corrupt_bytes":
        assert db.read_bytes() == before
    else:
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == (99 if damage == "unknown_version" else 1)
        conn.close()


@pytest.mark.parametrize("sql", ["mode='invalid'", "mode='adopt'", "thread_id='999999'", "revision=-1",
                                  "body_sha256=NULL", "anchor_message_id=NULL", "body_message_id=NULL"])
def test_real_schema_rejects_invalid_rows(tmp_path, remote, sql):
    assert call(tmp_path)["status"] == "delivered"
    conn = sqlite3.connect(tmp_path / DB_NAME)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE escalation_receipts SET " + sql)
    finally:
        conn.close()


@pytest.mark.parametrize("kind", [1, 5, 11, 15, 16])
def test_unsupported_parents(tmp_path, remote, monkeypatch, kind):
    real = remote.request
    def parent(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if path == "/channels/100":
            obj["type"] = kind
        return obj
    monkeypatch.setattr(dt, "_discord_request", parent)
    assert call(tmp_path)["error_code"] == "unsupported_parent"
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls)
    assert not (tmp_path / DB_NAME).exists()


@pytest.mark.parametrize("kind", ["archived", "locked", "missing"])
def test_inactive_thread_does_not_replace_or_send(tmp_path, remote, kind):
    first = call(tmp_path)
    obj = remote.threads[first["thread_id"]]
    if kind == "missing":
        del obj["thread_metadata"]
    else:
        obj["thread_metadata"][kind] = True
    before = len(remote.calls)
    result = call(tmp_path)
    assert result["error_code"] == "thread_inactive"
    assert result["body_delivered"] and not result["success"]
    assert not any(c[0] in ("PUT", "POST", "PATCH") for c in remote.calls[before:])


@pytest.mark.parametrize("member_failure", ["put_timeout", "put_bad_ack", "get_403", "get_wrong_ids"])
def test_initial_member_failure_safe_explicit_repair(tmp_path, remote, monkeypatch, member_failure):
    real = remote.request
    def broken(method, path, token, **kw):
        if "thread-members" in path and path.endswith("/33"):
            if method == "PUT" and member_failure == "put_timeout":
                raise TimeoutError()
            if method == "PUT" and member_failure == "put_bad_ack":
                return {}
            if method == "GET" and member_failure == "get_403":
                raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
            if method == "GET" and member_failure == "get_wrong_ids":
                return {"id": "wrong", "user_id": "33"}
        return real(method, path, token, **kw)
    monkeypatch.setattr(dt, "_discord_request", broken)
    first = call(tmp_path)
    assert first["error_code"] == "membership_failed" and not first["body_delivered"]
    assert not any(phase_of(c[0], c[1]) == "body" for c in remote.calls)
    monkeypatch.setattr(dt, "_discord_request", real)
    assert call(tmp_path, retry=True)["status"] == "delivered"
    assert len([c for c in remote.calls if c[0] == "POST"]) == 3


@pytest.mark.parametrize("delay", [None, -1, True, "invalid", float("inf"), float("nan")])
def test_invalid_rate_delay_defaults_to_sixty(tmp_path, remote, delay):
    def limited(method, path, token, body):
        if method == "POST":
            raise dt.DiscordAPIError(429, json.dumps({"message": "rate limited", "retry_after": delay}))
    remote.hook = limited
    result = call(tmp_path)
    assert 59 <= result["retry_after_seconds"] <= 60
    assert call(tmp_path, retry=True)["error_code"] == "rate_limited"


def test_self_target_and_oversized_member_union(tmp_path, remote):
    assert call(tmp_path, for_agent="11")["error_code"] == "self_target"
    assert not (tmp_path / DB_NAME).exists()
    config(tmp_path, observers=range(50, 68))
    assert call(tmp_path)["error_code"] == "invalid_policy"
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls)


def test_simultaneous_context_local_profiles(tmp_path, remote):
    from concurrent.futures import ThreadPoolExecutor
    homes = [tmp_path / "one", tmp_path / "two"]
    config(homes[0], observers=(33,))
    config(homes[1], observers=(66,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(call, homes[0], "requestor-token")
        two = pool.submit(call, homes[1], "second-token")
        a, b = one.result(), two.result()
    assert a["status"] == b["status"] == "delivered"
    assert a["sender_id"] == "11" and b["sender_id"] == "55"
    assert "33" in a["required_member_ids"] and "66" not in a["required_member_ids"]
    assert "66" in b["required_member_ids"] and "33" not in b["required_member_ids"]
    assert receipt(homes[0])["sender_id"] == "11"
    assert receipt(homes[1])["sender_id"] == "55"




@pytest.mark.parametrize("vulnerable,expected_mode", [(True, "delete"), (False, "wal")])
def test_canonical_sqlite_policy_modes(tmp_path, remote, monkeypatch, vulnerable, expected_mode):
    import hermes_state
    from tools import discord_escalation as de
    monkeypatch.setattr(hermes_state, "is_sqlite_wal_reset_vulnerable", lambda: vulnerable)
    assert call(tmp_path)["status"] == "delivered"
    conn = de.open_receipt_db(tmp_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == expected_mode
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert tables == ["escalation_receipts"]
    finally:
        conn.close()
    assert call(tmp_path)["status"] == "reused"


def test_sdk_free_import_without_receipt_side_effects(tmp_path):
    import os
    import subprocess
    import sys
    code = "import sys; sys.modules['discord'] = None; import tools.discord_tool, tools.discord_escalation; assert 'gateway.run' not in sys.modules; assert 'plugins.platforms.discord.adapter' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "HERMES_HOME": str(tmp_path)},
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / DB_NAME).exists()


@pytest.mark.parametrize("thread_field,value", [("type", 12), ("type", 10), ("parent_id", "101"),
    ("guild_id", "998"), ("thread_metadata", {"archived": True, "locked": False})])
def test_adopt_invalid_thread_fails_without_members_or_receipt(tmp_path, remote, thread_field, value):
    remote.threads["9000"] = dict(id="9000", type=11, parent_id="100", guild_id="999",
                                  thread_metadata={"archived": False, "locked": False})
    remote.threads["9000"][thread_field] = value
    result = call(tmp_path, name="", summary=None, body=None, existing_thread_id="9000")
    assert not result["success"] and result["status"] == "failed"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls)
    assert not (tmp_path / DB_NAME).exists()


def test_adopt_two_creators_independent_and_different_id_conflict(tmp_path, remote):
    for tid in ("9000", "9001"):
        remote.threads[tid] = dict(id=tid, type=11, parent_id="100", guild_id="999", owner_id="88",
                                  thread_metadata={"archived": False, "locked": False})
    other = tmp_path / "other"
    config(other)
    args = dict(name="", summary=None, body=None, existing_thread_id="9000")
    assert call(tmp_path, **args)["status"] == "thread_ready"
    assert call(other, token="second-token", **args)["status"] == "thread_ready"
    assert call(tmp_path, **{**args, "existing_thread_id": "9001"})["status"] == "conflict"
    assert not any(c[0] == "POST" for c in remote.calls)


@pytest.mark.parametrize("code", ["receipt_missing", "unattempted", "conflicting_id", "not_found"])
def test_reconcile_no_negative_proof_or_stage_skip(tmp_path, remote, code):
    if code == "receipt_missing":
        result = call(tmp_path, reconcile={"anchor_message_id": "9999"})
        assert result["error_code"] == "receipt_missing"
        return
    def failed_anchor(method, path, token, body):
        if phase_of(method, path) == "anchor":
            raise dt.DiscordAPIError(403, '{"code":50013,"message":"Denied"}')
    if code == "unattempted":
        remote.hook = failed_anchor
        call(tmp_path)
        remote.hook = None
        proof = {"anchor_message_id": "9999"}
    elif code == "conflicting_id":
        call(tmp_path)
        proof = {"anchor_message_id": "9999"}
    else:
        def timeout(method, path, token, body):
            if phase_of(method, path) == "anchor":
                raise TimeoutError()
        remote.hook = timeout
        call(tmp_path)
        remote.hook = None
        proof = {"anchor_message_id": "9999"}
    row = receipt(tmp_path)
    before = len(remote.calls)
    result = call(tmp_path, reconcile=proof)
    assert result["status"] == "failed", result
    assert receipt(tmp_path) == row
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])


@pytest.mark.parametrize("damage", ["insert", "readonly"])
def test_storage_insert_failure_never_claims_or_posts(tmp_path, remote, monkeypatch, damage):
    from tools import discord_escalation as de
    original = de.open_receipt_db
    conn = original(tmp_path)
    if damage == "insert":
        conn.execute("CREATE TRIGGER deny_insert BEFORE INSERT ON escalation_receipts BEGIN SELECT RAISE(ABORT, 'full disk'); END")
    conn.close()
    if damage == "readonly":
        def readonly(home):
            connection = original(home)
            connection.execute("PRAGMA query_only=ON")
            return connection
        monkeypatch.setattr(de, "open_receipt_db", readonly)
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls)


def test_existing_tool_ordinary_calls_never_enter_escalation(tmp_path, monkeypatch):
    from tools import discord_escalation as de
    monkeypatch.setattr(de, "create_agent_thread", lambda **kw: pytest.fail("ordinary call entered C2"))
    monkeypatch.setattr(dt, "_get_bot_token", lambda: "synthetic")
    monkeypatch.setattr(dt, "_load_allowed_actions_config", lambda: None)
    captures = []
    def remote(method, path, token, **kw):
        captures.append((method, path, kw["body"]))
        return {"id": "901", "name": "Ordinary"}
    monkeypatch.setattr(dt, "_discord_request", remote)
    for extra, suffix in (({}, "/channels/100/threads"), ({"message_id": "700"}, "/channels/100/messages/700/threads")):
        result = json.loads(registry._tools["discord"].handler({"action": "create_thread", "channel_id": "100",
            "name": "Ordinary", "auto_archive_duration": 60, **extra}))
        assert result == {"success": True, "thread_id": "901", "name": "Ordinary"}
        assert captures[-1] == ("POST", suffix, {"name": "Ordinary", "auto_archive_duration": 60, **({"type": 11} if not extra else {})})
    assert len(captures) == 2 and not (tmp_path / DB_NAME).exists()


def phase_of(method, path):
    if method != "POST":
        return None
    return "thread" if path.endswith("/threads") else "anchor" if path == "/channels/100/messages" else "body"


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
def test_uncertain_post_positive_reconcile_then_resume(tmp_path, remote, monkeypatch, phase):
    real = remote.request
    def lost_ack(method, path, token, **kw):
        obj = real(method, path, token, **kw)
        if phase_of(method, path) == phase:
            raise TimeoutError("lost acknowledgment")
        return obj
    monkeypatch.setattr(dt, "_discord_request", lost_ack)
    first = call(tmp_path)
    assert first["status"] == "reconciliation_required"
    assert receipt(tmp_path)["stage"] == phase + "_ambiguous"
    remote_before = len(remote.calls)
    assert call(tmp_path, retry=True)["status"] == "reconciliation_required"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[remote_before:])
    monkeypatch.setattr(dt, "_discord_request", remote.request)
    mid = (next(iter(remote.threads)) if phase == "thread" else
           next(k[1] for k in remote.messages if (k[0] == "100") == (phase == "anchor")))
    proof = {dict(anchor="anchor_message_id", thread="thread_id", body="body_message_id")[phase]: mid}
    before = len(remote.calls)
    repaired = call(tmp_path, reconcile=proof)
    assert repaired["status"] == "pending", repaired
    assert repaired["next_action"] == "resume" and not repaired["success"]
    assert repaired["body_delivered"] is (phase == "body")
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])
    resumed = call(tmp_path)
    assert resumed["status"] == ("reused" if phase == "body" else "delivered"), resumed
    assert len([c for c in remote.calls if phase_of(c[0], c[1]) == phase]) == 1


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 429])
def test_definitive_rejection_requires_explicit_retry(tmp_path, remote, phase, status):
    def reject(method, path, token, body):
        if phase_of(method, path) == phase:
            raise dt.DiscordAPIError(status, json.dumps({"code": 50013, "message": "Rejected", "retry_after": 0}))
    remote.hook = reject
    first = call(tmp_path)
    assert first["status"] == "failed" and first["retryable"], first
    assert receipt(tmp_path)["stage"] == phase + "_rejected"
    before = len(remote.calls)
    remote.hook = None
    blocked = call(tmp_path)
    assert blocked["error_code"] == "retry_required"
    assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])
    result = call(tmp_path, retry=True)
    assert result["status"] == "delivered", result
    assert len([c for c in remote.calls if phase_of(c[0], c[1]) == phase]) == 2
    for earlier in ["anchor", "thread", "body"]:
        if earlier != phase:
            assert len([c for c in remote.calls if phase_of(c[0], c[1]) == earlier]) == 1


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
@pytest.mark.parametrize("status,body", [(408, '{"code":1,"message":"timeout"}'),
    (500, '{"code":1,"message":"bad"}'), (403, 'html failure'), (429, 'not-json'),
    (502, 'response exceeded bounded size')])
def test_uncertainty_never_retries(tmp_path, remote, phase, status, body):
    def reject(method, path, token, payload):
        if phase_of(method, path) == phase:
            raise dt.DiscordAPIError(status, body)
    remote.hook = reject
    first = call(tmp_path)
    assert first["status"] == "reconciliation_required", first
    row = receipt(tmp_path)
    before = len(remote.calls)
    config(tmp_path, observers=(33, 66))
    remote.hook = None
    again = call(tmp_path, retry=True)
    assert again["status"] == "reconciliation_required"
    assert receipt(tmp_path) == row  # uncertainty blocks even additive policy writes
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls[before:])


def test_adoption_replay_and_mode_conflict(tmp_path, remote):
    remote.threads["9000"] = dict(id="9000", type=11, parent_id="100", guild_id="999",
                                  thread_metadata={"archived": False, "locked": False})
    adopt = dict(name="", summary=None, body=None, existing_thread_id="9000")
    first = call(tmp_path, **adopt)
    assert first["status"] == "thread_ready", first
    assert first["body_sha256"] is first["body_message_id"] is first["anchor_message_id"] is None
    assert not first["body_delivered"]
    assert call(tmp_path, **adopt)["status"] == "thread_ready"
    assert not any(c[0] == "POST" for c in remote.calls)
    before = len(remote.calls)
    assert call(tmp_path)["status"] == "conflict"
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls[before:])


@pytest.mark.parametrize("field,value", [("body", "different "), ("summary", "changed"),
    ("name", "Different"), ("for_agent", "66"), ("requester_id", "77"),
    ("severity", "critical"), ("auto_archive_duration", 60)])
def test_immutable_input_conflict(tmp_path, remote, field, value):
    assert call(tmp_path)["status"] == "delivered"
    row = receipt(tmp_path)
    before = len(remote.calls)
    assert call(tmp_path, **{field: value})["status"] == "conflict"
    assert receipt(tmp_path) == row
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls[before:])


def test_observer_repair_and_preserved_delivery(tmp_path, remote):
    first = call(tmp_path)
    config(tmp_path, observers=(66,))
    remote.members.clear()
    def fail(method, path, token, body):
        if method == "PUT" and path.endswith("/66"):
            raise TimeoutError()
    remote.hook = fail
    failed = call(tmp_path)
    assert not failed["success"] and failed["body_delivered"]
    assert failed["body_message_id"] == first["body_message_id"]
    assert failed["error_code"] == "membership_failed"
    assert call(tmp_path)["error_code"] == "retry_required"
    remote.hook = None
    fixed = call(tmp_path, retry=True)
    assert fixed["status"] == "reused"
    assert fixed["required_member_ids"] == ["11", "22", "33", "44", "66"]
    assert fixed["verified_member_ids"] == fixed["required_member_ids"]
    assert len([c for c in remote.calls if c[0] == "POST"]) == 3


@pytest.mark.parametrize("field,value", [("for_agent", ""), ("for_agent", True), ("for_agent", 22),
    ("for_agent", " 22"), ("for_agent", "<@22>"), ("for_agent", "0"),
    ("issue_key", "bad key"), ("body", ""), ("body", "\x00"), ("body", "\ud800"),
    ("body", "x" * 1995), ("summary", "@everyone"), ("summary", "x" * 199),
    ("name", "x" * 101), ("name", "a\nb"), ("retry", 1), ("auto_archive_duration", True),
    ("reconcile", {}), ("reconcile", {"unknown": "12"}), ("message_id", "12")])
def test_validation_no_remote_mutations(tmp_path, remote, field, value):
    result = call(tmp_path, **{field: value})
    assert "error" in result and not result.get("success"), result
    assert not remote.calls
    assert not (tmp_path / DB_NAME).exists()


def test_exact_2000_boundary_and_token_rotation(tmp_path, remote):
    body = "é" * (2000 - len("<@22>\n"))
    first = call(tmp_path, body=body, for_agent="0022")
    assert first["status"] == "delivered"
    assert call(tmp_path, token="rotated-token", body=body)["status"] == "reused"
    before = len(remote.calls)
    assert call(tmp_path, token="second-token", body=body)["error_code"] == "identity_conflict"
    assert not any(c[0] in ("PUT", "POST") for c in remote.calls[before:])


@pytest.mark.parametrize("enabled,observers", [(False, [33]), ("true", [33]), (True, []),
                                               (True, [True]), (True, ["bad"])])
def test_invalid_policy_no_mutations(tmp_path, remote, enabled, observers):
    config(tmp_path, observers=observers, enabled=enabled)
    result = call(tmp_path)
    assert result["error_code"] in ("disabled", "invalid_policy")
    assert not remote.calls and not (tmp_path / DB_NAME).exists()


@pytest.mark.parametrize("phase", ["anchor", "thread", "body"])
@pytest.mark.parametrize("boundary", ["claim", "ack"])
def test_storage_failure_before_claim_or_after_ack(tmp_path, remote, monkeypatch, phase, boundary):
    from tools import discord_escalation as de
    original = de.advance_receipt
    next_stage = {"anchor": "thread_pending", "thread": "members_pending", "body": "complete"}[phase]
    def broken(conn, row, **changes):
        target = phase + "_inflight" if boundary == "claim" else next_stage
        if changes.get("stage") == target:
            raise sqlite3.OperationalError("simulated full disk at commit boundary")
        return original(conn, row, **changes)
    monkeypatch.setattr(de, "advance_receipt", broken)
    result = call(tmp_path)
    assert result["error_code"] == "storage_failed"
    posts = [c for c in remote.calls if phase_of(c[0], c[1]) == phase]
    if boundary == "claim":
        assert not posts and result["status"] == "failed"
    else:
        assert len(posts) == 1 and result["status"] == "reconciliation_required"
        assert result[{"anchor": "anchor_message_id", "thread": "thread_id", "body": "body_message_id"}[phase]]
        assert not result["receipt_persisted"]
        assert receipt(tmp_path)["stage"] == phase + "_inflight"
    monkeypatch.setattr(de, "advance_receipt", original)
    if boundary == "ack":
        before = len(remote.calls)
        assert call(tmp_path, retry=True)["status"] == "reconciliation_required"
        assert not any(c[0] in ("POST", "PUT") for c in remote.calls[before:])
