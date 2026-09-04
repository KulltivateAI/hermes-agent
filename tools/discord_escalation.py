"""Opt-in requester-owned Discord threads; profile-local receipts, not an outbox.

Every POST needs a committed compare-and-swap claim. Inflight work is never
expired/reclaimed: an interrupted caller needs positive operator reconciliation.
Network calls never run inside a receipt transaction.
"""
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from discord_escalation_protocol import (
    build_escalation_anchor, is_trusted_nonconversational_message,
)

logger = logging.getLogger(__name__)
DB_NAME = "discord_escalation_receipts.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS escalation_receipts (
 parent_channel_id TEXT NOT NULL, issue_key TEXT NOT NULL,
 mode TEXT NOT NULL CHECK (mode IN ('create','adopt')),
 sender_id TEXT NOT NULL, guild_id TEXT NOT NULL, input_json TEXT NOT NULL,
 body_sha256 TEXT, required_members_json TEXT NOT NULL,
 anchor_message_id TEXT, thread_id TEXT, body_message_id TEXT,
 stage TEXT NOT NULL CHECK (stage IN (
 'anchor_pending','anchor_inflight','anchor_rejected','anchor_ambiguous',
 'thread_pending','thread_inflight','thread_rejected','thread_ambiguous',
 'members_pending','body_pending','body_inflight','body_rejected','body_ambiguous','complete')),
 revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
 last_error_code TEXT, last_http_status INTEGER, retry_not_before REAL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 PRIMARY KEY (parent_channel_id,issue_key),
 CHECK ((mode='create' AND body_sha256 IS NOT NULL AND length(body_sha256)=64)
     OR (mode='adopt' AND body_sha256 IS NULL)),
 CHECK (mode!='adopt' OR (anchor_message_id IS NULL AND body_message_id IS NULL
     AND thread_id IS NOT NULL AND stage IN ('members_pending','complete'))),
 CHECK (mode!='create' OR stage!='complete' OR (anchor_message_id IS NOT NULL
     AND thread_id IS NOT NULL AND body_message_id IS NOT NULL)),
 CHECK (mode!='create' OR thread_id IS NULL OR
     (anchor_message_id IS NOT NULL AND thread_id=anchor_message_id))
)
"""


class Failure(Exception):
    def __init__(self, code, message, action="correct_input", status="failed", retryable=False):
        super().__init__(message)
        self.code, self.action, self.status, self.retryable = code, action, status, retryable


class ConcurrentUpdate(Exception):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def snowflake(value, *, policy=False):
    if policy and type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("IDs must be decimal snowflake strings, not mentions or numbers")
    if len(value.lstrip("0")) > 20 or not 0 < int(value) < 2**64:
        raise ValueError("snowflake out of range")
    return str(int(value))


def text(value):
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    value.encode("utf-8", errors="strict")
    if "\x00" in value:
        raise ValueError("text must not contain NUL")
    return value


def observer_ids(policy):
    if not isinstance(policy, dict):
        raise Failure("invalid_policy", "discord.escalation_threads must be an object", "correct_config")
    if "enabled" in policy and type(policy["enabled"]) is not bool:
        raise Failure("invalid_policy", "discord.escalation_threads.enabled must be a boolean", "correct_config")
    if policy.get("enabled") is not True:
        raise Failure("disabled", "discord.escalation_threads is disabled; Ops must verify receivers before enablement", "correct_config")
    try:
        values = policy.get("observer_ids")
        if not isinstance(values, list) or not values:
            raise ValueError("enabled escalation threads require nonempty observer_ids")
        return {snowflake(v, policy=True) for v in values}
    except ValueError as exc:
        raise Failure("invalid_policy", str(exc), "correct_config") from exc


def open_receipt_db(home):
    from hermes_state import apply_database_pragmas, apply_wal_with_fallback

    path = Path(home) / DB_NAME
    # O_EXCL preserves permissions/ownership of existing files and races safely.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
            raise sqlite3.DatabaseError("unknown receipt schema version")
        apply_wal_with_fallback(conn, db_label=DB_NAME)
        apply_database_pragmas(conn, db_label=DB_NAME)
        conn.execute("PRAGMA synchronous=FULL")
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() not in ("wal", "delete"):
            raise sqlite3.DatabaseError("unsafe receipt journal mode")
        conn.execute("BEGIN IMMEDIATE")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute(SCHEMA)
            conn.execute("PRAGMA user_version=1")
        elif version != 1:
            raise sqlite3.DatabaseError("unknown receipt schema version")
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def load_receipt(conn, parent, issue):
    row = conn.execute("SELECT * FROM escalation_receipts WHERE parent_channel_id=? AND issue_key=?",
                       (parent, issue)).fetchone()
    return dict(row) if row else None


def load_or_insert_receipt(conn, values):
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = ",".join(values)
        conn.execute(f"INSERT INTO escalation_receipts ({columns}) VALUES ({','.join('?' for _ in values)}) "
                     "ON CONFLICT(parent_channel_id,issue_key) DO NOTHING", tuple(values.values()))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return load_receipt(conn, values["parent_channel_id"], values["issue_key"])


def advance_receipt(conn, row, **changes):
    conn.execute("BEGIN IMMEDIATE")
    try:
        assignments = ",".join(f"{key}=?" for key in changes)
        count = conn.execute(
            f"UPDATE escalation_receipts SET {assignments},revision=revision+1,updated_at=? "
            "WHERE parent_channel_id=? AND issue_key=? AND revision=? AND stage=?",
            (*changes.values(), time.time(), row["parent_channel_id"], row["issue_key"], row["revision"], row["stage"]),
        ).rowcount
        if count != 1:
            raise ConcurrentUpdate()
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**row, **changes, "revision": row["revision"] + 1}


class _Attempt:
    """Invocation-local state only; no token, home, or connection shared across callers."""
    def __init__(self, token, request, home, mode, parent, issue):
        self.token, self.request, self.home = token, request, home
        self.mode, self.parent, self.issue = mode, parent, issue
        self.conn = self.row = None
        self.sender = self.guild = self.body_hash = None
        self.required, self.verified, self.known = [], [], {}
        self.persisted = True
        self.post_unrecorded = False
        self.http_status = None

    def result(self, status, failure=None):
        row = self.row or {}
        complete = row.get("stage") == "complete" and row.get("mode") == "create"
        obligations = json.loads(row["required_members_json"]) if row else self.required
        result = dict(
            success=status in ("delivered", "reused", "thread_ready"), status=status,
            mode=self.mode, issue_key=self.issue, parent_channel_id=self.parent,
            stage=row.get("stage"), sender_id=row.get("sender_id", self.sender),
            guild_id=row.get("guild_id", self.guild), body_sha256=row.get("body_sha256", self.body_hash),
            body_delivered=complete, required_member_ids=obligations,
            verified_member_ids=sorted(self.verified), receipt_persisted=bool(row) and self.persisted,
            retryable=False, next_action=None, http_status=self.http_status,
            retry_after_seconds=max(0, row["retry_not_before"] - time.time()) if row.get("retry_not_before") else None,
        )
        for key in ("anchor_message_id", "thread_id", "body_message_id"):
            result[key] = self.known.get(key, row.get(key))
        if failure:
            result.update(error=str(failure), error_code=failure.code, next_action=failure.action,
                          retryable=failure.retryable)
        return result

    def remote(self, method, path, **kw):
        try:
            return self.request(method, path, self.token, **kw)
        except Exception as exc:
            if method == "GET":
                self.http_status = getattr(exc, "status", None)
                raise Failure("remote_read_failed", "Discord read failed; verify permissions and exact IDs",
                              "retry", retryable=True) from exc
            raise

    def advance(self, **changes):
        self.row = advance_receipt(self.conn, self.row, **changes)

    def transition(self, stage, **changes):
        self.advance(stage=stage, last_error_code=None, last_http_status=None, retry_not_before=None, **changes)

    def message(self, obj, channel, *, anchor=False, expected_id=None):
        if not isinstance(obj, dict):
            raise ValueError("message acknowledgment must be an object")
        mid = snowflake(obj.get("id"))
        if expected_id is not None and mid != expected_id:
            raise ValueError("message ID mismatch")
        if snowflake(obj.get("channel_id")) != channel:
            raise ValueError("message channel mismatch")
        author = obj.get("author")
        if not isinstance(author, dict) or author.get("bot") is not True or snowflake(author.get("id")) != self.sender:
            raise ValueError("message author mismatch")
        if obj.get("webhook_id") is not None or type(obj.get("type")) is not int or obj["type"] != 0:
            raise ValueError("webhook/non-default message")
        if any(obj.get(k) for k in ("message_reference", "referenced_message", "message_snapshots",
                                   "attachments", "components", "stickers", "sticker_items", "poll")):
            raise ValueError("message has extra authored content")
        if anchor:
            if not is_trusted_nonconversational_message(obj, {self.sender}) or obj.get("content") != "":
                raise ValueError("not the C1 anchor")
            expected = self.anchor["embeds"][0]
            if any(obj["embeds"][0].get(k) != v for k, v in expected.items()):
                raise ValueError("anchor title/discriminator mismatch")
        elif obj.get("content") != self.content or hashlib.sha256(text(obj["content"]).encode()).hexdigest() != self.body_hash:
            raise ValueError("body does not exactly match")
        return mid

    def thread(self, obj, tid, *, active=False):
        if not isinstance(obj, dict) or type(obj.get("type")) is not int or obj["type"] != 11:
            raise Failure("invalid_thread", "Expected a public guild thread")
        if (snowflake(obj.get("id")) != tid or snowflake(obj.get("parent_id")) != self.parent
                or snowflake(obj.get("guild_id")) != self.guild):
            raise Failure("invalid_thread", "Thread ID/parent/guild mismatch")
        if active:
            meta = obj.get("thread_metadata")
            if not isinstance(meta, dict) or meta.get("archived") is not False or meta.get("locked") is not False:
                raise Failure("thread_inactive", "Thread must be active and unlocked; correct it manually", "retry", retryable=True)
        return obj

    def post(self, phase, path, payload, validate, next_stage, id_key):
        self.transition(phase + "_inflight")  # durable ownership before invoking transport
        self.post_unrecorded = True
        try:
            obj = self.remote("POST", path, body=payload)
            if isinstance(obj, dict):
                try:
                    self.known[id_key] = snowflake(obj.get("id"))
                except ValueError:
                    pass
            mid = validate(obj)
        except Exception as exc:
            status = getattr(exc, "status", None)
            self.http_status = status if type(status) is int else None
            raw = getattr(exc, "body", None)
            try:
                error = json.loads(raw) if isinstance(raw, str) else None
            except (ValueError, TypeError):
                error = None
            is_error = isinstance(error, dict) and type(error.get("code")) is int and isinstance(error.get("message"), str)
            is_rate = isinstance(error, dict) and status == 429 and isinstance(error.get("message"), str)
            rejected = type(status) is int and 400 <= status < 500 and status != 408 and (is_error or is_rate)
            if rejected:
                wait = None
                if status == 429:
                    delay = error.get("retry_after", 60)
                    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 0:
                        delay = 60
                    wait = time.time() + delay
                self.advance(stage=phase + "_rejected", last_error_code="remote_rejected",
                             last_http_status=status, retry_not_before=wait)
                self.post_unrecorded = False
                raise Failure("remote_rejected", f"Discord rejected {phase}; correct permissions/input conditions then retry identical input",
                              "retry", retryable=True) from exc
            # A valid candidate ID is useful for operators, not proof of success.
            candidate = self.known.get(id_key)
            ids = {id_key: candidate} if candidate else {}
            if phase == "thread" and candidate != self.row["anchor_message_id"]:
                ids = {}
            self.advance(stage=phase + "_ambiguous", last_error_code="ambiguous_post",
                         last_http_status=self.http_status, **ids)
            self.post_unrecorded = False
            raise Failure("ambiguous_post", f"Uncertain {phase} POST; reconcile its exact ID, never blindly retry",
                          "reconcile", "reconciliation_required") from exc
        self.transition(next_stage, **{id_key: mid})
        self.post_unrecorded = False
        return obj

    def reconcile_proofs(self, proofs):
        if self.mode == "adopt":
            raise Failure("reconcile_mismatch", "Adoption has no POST uncertainty; use membership retry")
        stage = self.row["stage"]
        phases = ("anchor", "thread", "body")
        keys = ("anchor_message_id", "thread_id", "body_message_id")
        next_stages = ("thread_pending", "members_pending", "complete")
        # A stored candidate ID in *_ambiguous is not yet a proven ID.
        rank = 3 if stage == "complete" else 2 if stage == "members_pending" else phases.index(stage.split("_")[0])
        uncertain = stage.endswith(("_inflight", "_ambiguous"))
        ids = {key: self.row[key] for key in keys}
        changes = {}
        try:
            for index, (phase, key) in enumerate(zip(phases, keys)):
                if key not in proofs:
                    continue
                proposed = proofs[key]
                if index > rank or (index == rank and not uncertain):
                    raise ValueError("proof refers to a never-attempted POST")
                if ids[key] is not None and ids[key] != proposed:
                    raise ValueError("proof conflicts with a recorded ID")
                if index == 0:
                    obj = self.remote("GET", f"/channels/{self.parent}/messages/{proposed}")
                    self.message(obj, self.parent, anchor=True, expected_id=proposed)
                elif index == 1:
                    if ids["anchor_message_id"] is None or proposed != ids["anchor_message_id"]:
                        raise ValueError("thread proof must equal the proven anchor ID")
                    self.thread(self.remote("GET", f"/channels/{proposed}"), proposed)
                else:
                    tid = ids["thread_id"]
                    if tid is None:
                        raise ValueError("body proof requires a proven thread")
                    obj = self.remote("GET", f"/channels/{tid}/messages/{proposed}")
                    self.message(obj, tid, expected_id=proposed)
                ids[key] = proposed
                changes[key] = proposed
                if index == rank and uncertain:
                    changes.update(stage=next_stages[index], last_error_code=None,
                                   last_http_status=None, retry_not_before=None)
        except (ValueError, TypeError, Failure) as exc:
            raise Failure("reconcile_mismatch", "Proof does not match the attempted operation and exact stored input") from exc
        # All GET proofs validated before the single atomic revision-guarded write.
        self.advance(**changes)
        result = self.result("pending")
        result["next_action"] = "resume"
        return result

    def memberships(self, thread_obj=None):
        tid = self.row["thread_id"]
        try:
            self.thread(thread_obj if thread_obj is not None else self.remote("GET", f"/channels/{tid}"), tid, active=True)
            for member in self.required:
                suffix = "@me" if member == self.sender else member
                if self.remote("PUT", f"/channels/{tid}/thread-members/{suffix}") is not None:
                    raise ValueError("membership PUT did not return 204")
                obj = self.remote("GET", f"/channels/{tid}/thread-members/{member}")
                if (not isinstance(obj, dict) or snowflake(obj.get("id")) != tid
                        or snowflake(obj.get("user_id")) != member):
                    raise ValueError("membership GET did not prove thread/user")
                self.verified.append(member)
        except Exception as exc:
            self.http_status = getattr(exc, "status", None)
            self.advance(last_error_code="membership_failed")
            if isinstance(exc, Failure) and exc.code in ("invalid_thread", "thread_inactive"):
                raise
            raise Failure("membership_failed", "Explicit membership PUT/GET failed; correct permissions and retry",
                          "retry", retryable=True) from exc
        if self.row.get("last_error_code") == "membership_failed":
            self.advance(last_error_code=None)

    def run(self, policy, name, recipient, issue, summary, severity, body, requester, existing, archive, retry, reconcile):
        try:
            self.parent = snowflake(self.parent)
            recipient = snowflake(recipient)
            requester = snowflake(requester) if requester is not None else None
            existing = snowflake(existing) if existing is not None else None
            if not isinstance(issue, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", issue):
                raise ValueError("issue_key must be a stable ASCII key of 1..128 characters")
            if type(retry) is not bool or type(archive) is not int or archive not in (60, 1440, 4320, 10080):
                raise ValueError("retry must be boolean and archive duration a supported integer")
            if reconcile is not None:
                if (retry or not isinstance(reconcile, dict) or not reconcile
                        or set(reconcile) - {"anchor_message_id", "thread_id", "body_message_id"}):
                    raise ValueError("reconcile needs exact proof IDs only and cannot combine with retry")
                reconcile = {k: snowflake(v) for k, v in reconcile.items()}
            if existing is not None:
                if name or any(v is not None for v in (summary, severity, body)) or archive != 1440:
                    raise ValueError("adopt requires no name, summary, severity, body or custom archive duration")
            else:
                text(name)
                if not name.strip() or len(name) > 100 or any(v in name for v in ("\n", "\r", "<@", "@everyone", "@here")):
                    raise ValueError("name must be 1..100, nonblank, single-line and mention-free")
                text(summary)
                severity = "warning" if severity is None else severity
                self.anchor = build_escalation_anchor(summary, severity)
                text(body)
                self.content = f"<@{recipient}>\n{body}"
                if not body.strip() or len(self.content) > 2000:
                    raise ValueError("body must be nonblank; mention plus body must fit 2000 characters")
                self.body_hash = hashlib.sha256(self.content.encode()).hexdigest()
            observers = observer_ids(policy)
        except (ValueError, TypeError) as exc:
            raise Failure("invalid_input", str(exc)) from exc
        user = self.remote("GET", "/users/@me")
        if not isinstance(user, dict) or user.get("bot") is not True:
            raise Failure("invalid_input", "Authenticated token must identify a bot")
        self.sender = snowflake(user.get("id"))
        if recipient == self.sender:
            raise Failure("self_target", "Recipient must differ from authenticated requestor")
        self.required = sorted({self.sender, recipient, *observers} | ({requester} if requester else set()))
        if len(self.required) > 16:
            raise Failure("invalid_policy", "At most 16 required members are supported", "correct_config")
        parent = self.remote("GET", f"/channels/{self.parent}")
        if (not isinstance(parent, dict) or type(parent.get("type")) is not int or parent["type"] != 0
                or snowflake(parent.get("id")) != self.parent):
            raise Failure("unsupported_parent", "Parent must be a matching GUILD_TEXT channel")
        self.guild = snowflake(parent.get("guild_id"))
        inputs = dict(mode=self.mode, parent=self.parent, guild=self.guild, sender=self.sender,
                      recipient=recipient, requester=requester)
        if existing is None:
            inputs.update(name=name, summary=summary, severity=severity, archive_duration=archive, body_sha256=self.body_hash)
        else:
            inputs["existing_thread_id"] = existing
        encoded = canonical_json(inputs)
        thread_obj = None
        if existing is not None:
            thread_obj = self.thread(self.remote("GET", f"/channels/{existing}"), existing, active=True)
        self.conn = open_receipt_db(self.home)
        self.row = load_receipt(self.conn, self.parent, issue)
        if self.row is None:
            if reconcile is not None:
                raise Failure("receipt_missing", "Reconciliation requires an existing receipt")
            now = time.time()
            self.row = load_or_insert_receipt(self.conn, dict(
                parent_channel_id=self.parent, issue_key=issue, mode=self.mode, sender_id=self.sender,
                guild_id=self.guild, input_json=encoded, body_sha256=self.body_hash,
                required_members_json=canonical_json(self.required), thread_id=existing,
                stage="members_pending" if existing else "anchor_pending", created_at=now, updated_at=now,
            ))
        try:
            required_columns = {"parent_channel_id", "issue_key", "mode", "sender_id", "guild_id",
                "input_json", "body_sha256", "required_members_json", "anchor_message_id", "thread_id",
                "body_message_id", "stage", "revision", "last_error_code", "last_http_status",
                "retry_not_before", "created_at", "updated_at"}
            if not required_columns <= self.row.keys():
                raise ValueError("incomplete receipt schema")
            stored = json.loads(self.row["input_json"])
            members = json.loads(self.row["required_members_json"])
            if not isinstance(stored, dict) or not isinstance(members, list) or not members:
                raise ValueError("malformed receipt")
            mode_fields = ({"name", "summary", "severity", "archive_duration", "body_sha256"}
                           if self.row["mode"] == "create" else {"existing_thread_id"})
            if set(stored) != {"mode", "parent", "guild", "sender", "recipient", "requester"} | mode_fields:
                raise ValueError("malformed immutable receipt input")
            for input_key, column in (("mode", "mode"), ("parent", "parent_channel_id"),
                                      ("guild", "guild_id"), ("sender", "sender_id")):
                if stored[input_key] != self.row[column]:
                    raise ValueError("receipt identity columns disagree")
            promised = {snowflake(stored[k]) for k in ("sender", "recipient")}
            if stored["requester"] is not None:
                promised.add(snowflake(stored["requester"]))
            if not promised <= set(members):
                raise ValueError("receipt lost immutable member obligations")
            if self.row["mode"] == "create" and stored["body_sha256"] != self.row["body_sha256"]:
                raise ValueError("receipt body hash columns disagree")
            if members != sorted({snowflake(v) for v in members}) or len(members) > 16:
                raise ValueError("malformed member obligations")
        except (ValueError, TypeError) as exc:
            # Do not let a corrupt array re-enter the result serializer.
            self.row = None
            raise sqlite3.DatabaseError("malformed receipt input/obligations") from exc
        if self.row["sender_id"] != self.sender:
            raise Failure("identity_conflict", "This issue belongs to another authenticated sender", status="conflict")
        if self.row["input_json"] != encoded:
            raise Failure("input_conflict", "Issue key already has different immutable input/mode", status="conflict")
        if reconcile is not None:
            return self.reconcile_proofs(reconcile)
        stage = self.row["stage"]
        if stage.endswith(("_inflight", "_ambiguous")):
            raise Failure("ambiguous_post", "Prior POST may exist or still be running; provide positive ID reconciliation",
                          "reconcile", "reconciliation_required")
        wait = self.row["retry_not_before"]
        if wait is not None and wait > time.time():
            raise Failure("rate_limited", "Wait for retry_after_seconds, then retry identical input", "retry", retryable=True)
        if stage.endswith("_rejected") or self.row["last_error_code"] == "membership_failed":
            if not retry:
                raise Failure("retry_required", "Correct the failure then explicitly retry identical input", "retry", retryable=True)
            if stage.endswith("_rejected"):
                self.transition(stage.removesuffix("_rejected") + "_pending")
        self.required = sorted(set(members) | set(self.required))
        if len(self.required) > 16:
            raise Failure("invalid_policy", "New policy exceeds retained 16-member obligations", "correct_config")
        if self.required != members:
            self.advance(required_members_json=canonical_json(self.required))
        if self.row["stage"] == "anchor_pending":
            self.post("anchor", f"/channels/{self.parent}/messages", self.anchor,
                      lambda obj: self.message(obj, self.parent, anchor=True), "thread_pending", "anchor_message_id")
        if self.row["stage"] == "thread_pending":
            anchor = self.row["anchor_message_id"]
            def validate_thread(obj):
                self.thread(obj, anchor)
                return anchor
            thread_obj = self.post("thread", f"/channels/{self.parent}/messages/{anchor}/threads",
                                   {"name": name, "auto_archive_duration": archive}, validate_thread,
                                   "members_pending", "thread_id")
            metadata = thread_obj.get("thread_metadata")
            if not isinstance(metadata, dict) or not {"archived", "locked"} <= set(metadata):
                thread_obj = None
        self.memberships(thread_obj)
        if self.row["stage"] == "complete":
            return self.result("reused" if self.mode == "create" else "thread_ready")
        if self.mode == "adopt":
            self.transition("complete")
            return self.result("thread_ready")
        if self.row["stage"] == "members_pending":
            self.transition("body_pending")
        if self.row["stage"] == "body_pending":
            self.post("body", f'/channels/{self.row["thread_id"]}/messages',
                      {"content": self.content, "allowed_mentions": {"parse": [], "users": [recipient], "replied_user": False}},
                      lambda obj: self.message(obj, self.row["thread_id"]), "complete", "body_message_id")
            return self.result("delivered")
        raise Failure("concurrent_update", "Receipt changed; inspect before resuming", "resume", "pending")


def create_agent_thread(*, token: str, request: Callable, home: Path, policy: dict,
                        channel_id: str, name: str = '', for_agent: str, issue_key: str,
                        summary: str | None = None, severity: str | None = None,
                        body: str | None = None, requester_id: str | None = None,
                        existing_thread_id: str | None = None, auto_archive_duration: int = 1440,
                        retry: bool = False, reconcile: dict | None = None) -> dict:
    attempt = _Attempt(token, request, home, "adopt" if existing_thread_id is not None else "create", channel_id, issue_key)
    try:
        return attempt.run(policy, name, for_agent, issue_key, summary, severity, body, requester_id,
                           existing_thread_id, auto_archive_duration, retry, reconcile)
    except ConcurrentUpdate:
        attempt.row = load_receipt(attempt.conn, attempt.parent, attempt.issue)
        attempt.known = {}
        uncertain = attempt.row and attempt.row["stage"].endswith(("_inflight", "_ambiguous"))
        failure = Failure("concurrent_update", "Another invocation advanced this receipt; no action was reclaimed",
                          "reconcile" if uncertain else "resume", "reconciliation_required" if uncertain else "pending")
    except (sqlite3.Error, OSError) as exc:
        attempt.persisted = False
        failure = Failure("storage_failed", "Receipt storage failed; repair storage and inspect exact known IDs",
                          "repair_storage", "reconciliation_required" if attempt.post_unrecorded else "failed")
    except Failure as exc:
        failure = exc
        if failure.code in ("invalid_policy", "disabled"):
            logger.warning("Discord escalation sender policy rejected: %s", failure.code)
    except Exception:
        failure = Failure("remote_read_failed", "Preflight/read response failed validation; inspect permissions and supplied IDs", "retry", retryable=True)
    finally:
        if attempt.conn is not None:
            attempt.conn.close()
    return attempt.result(failure.status, failure)
