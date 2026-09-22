"""Invariant test: a completed webhook delivery closes its session.

Regression guard for the ghost-session leak.  Webhook deliveries create a
unique one-shot session (``delivery_id`` baked into the session key), but the
adapter historically fired ``handle_message`` without ever ending the session.
``SessionDB.prune_sessions`` only reaps rows where ``ended_at IS NOT NULL``, so
every webhook session stayed unprunable and state.db grew without bound (this
was the primary driver of the SQLite lock-contention gateway outage).

The invariant asserted here is a *behavior contract*, not a snapshot: once a
webhook delivery's agent run completes, the session row for that delivery must
have ``ended_at`` set — mirroring how a cron run closes its session with
``end_session(..., "cron_complete")``.

CRITICAL: these tests go through the REAL ``handle_message`` →
``_process_message_background`` → ``on_processing_complete`` pipeline (only the
runner-side ``_message_handler`` is stubbed, exactly the seam the live gateway
injects).  ``handle_message`` is fire-and-forget — it spawns the background
task and returns before the run starts — so any close bolted around
``handle_message`` itself runs BEFORE the session row exists and silently
no-ops.  A test that fakes ``handle_message`` to create the row synchronously
masks exactly that bug (the first version of this fix shipped that way).
"""

import asyncio
from contextlib import contextmanager, nullcontext

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH
from gateway.session import SessionSource, SessionStore


def _make_adapter(routes, **extra_kw) -> WebhookAdapter:
    extra = {"host": "127.0.0.1", "port": 0, "routes": routes}
    extra.update(extra_kw)
    config = PlatformConfig(enabled=True, extra=extra)
    return WebhookAdapter(config)


class _FakeRunner:
    """Minimal gateway runner surface the webhook close path depends on.

    Wires a real ``SessionStore`` (which owns a real ``SessionDB``) and reuses
    that same ``SessionDB`` as ``_session_db`` so the row created at routing
    time is the row the close path ends — exactly the wiring the live gateway
    has (``self.session_store`` + ``self._session_db``).
    """

    def __init__(self, store: SessionStore):
        self.session_store = store
        self._session_db = store._db

    def _session_key_for_source(self, source: SessionSource) -> str:
        return self.session_store._generate_session_key(source)

    def _profile_scope_for_source(self, _source: SessionSource):
        return nullcontext()


def _make_store(tmp_path) -> SessionStore:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    config = GatewayConfig(
        platforms={Platform.WEBHOOK: PlatformConfig(enabled=True)}
    )
    store = SessionStore(sessions_dir=sessions_dir, config=config)
    assert store._db is not None, "test requires a real SessionDB"
    return store


def _make_event(adapter: WebhookAdapter, delivery_id: str, text: str) -> MessageEvent:
    session_chat_id = f"webhook:alerts:{delivery_id}"
    source = adapter.build_source(
        chat_id=session_chat_id,
        chat_name="webhook/alerts",
        chat_type="webhook",
        user_id="webhook:alerts",
        user_name="alerts",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"message": text},
        message_id=delivery_id,
    )


async def _drain_background_tasks(adapter: WebhookAdapter, timeout: float = 5.0) -> None:
    """Wait for the adapter's spawned processing task(s) to finish."""
    deadline = asyncio.get_event_loop().time() + timeout
    while adapter._background_tasks and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
    # One extra tick for done-callbacks to run.
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_completed_webhook_delivery_closes_its_session(tmp_path, monkeypatch):
    """After a webhook run finishes (REAL dispatch path), ended_at is set."""
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)

    adapter = _make_adapter(
        {
            "alerts": {
                "secret": _INSECURE_NO_AUTH,
                "prompt": "Alert: {message}",
                "deliver": "log",
            }
        }
    )
    adapter.gateway_runner = runner
    lifecycle_calls = []
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook_checked",
        lambda hook_name, **kwargs: (lifecycle_calls.append((hook_name, kwargs)), ([], True))[1],
    )

    # Stub the RUNNER-side handler (the seam the live gateway injects) — the
    # adapter's own handle_message / _process_message_background pipeline runs
    # for real, including the fire-and-forget task spawn and the
    # on_processing_complete hook.  The handler creates the session row, just
    # like GatewayRunner._handle_message does at routing time.
    created = {}

    async def _message_handler(event: MessageEvent):
        entry = store.get_or_create_session(event.source)
        created["session_id"] = entry.session_id
        return ""  # webhook deliver=log — nothing to send back

    adapter._message_handler = _message_handler

    event = _make_event(adapter, "alert-close-001", "Alert: server on fire")

    # Exactly what _handle_webhook schedules.
    await adapter.handle_message(event)
    # handle_message is fire-and-forget: the session must NOT be expected to
    # exist yet.  (Guards against reintroducing a close wrapped around
    # handle_message itself, which ran before the row existed and no-op'd.)
    await _drain_background_tasks(adapter)

    session_id = created["session_id"]
    row = store._db.get_session(session_id)
    assert row is not None

    # INVARIANT: a completed webhook session must be closed so prune can reap it.
    assert row["ended_at"] is not None, (
        "webhook session was never closed — ended_at is NULL, so "
        "prune_sessions can never reap it (the ghost-session leak)"
    )
    assert row["end_reason"] == "webhook_complete"
    assert lifecycle_calls == [(
        "on_session_end",
        {
            "session_id": session_id,
            "completed": True,
            "interrupted": False,
            "reason": "webhook_complete",
            "platform": "webhook",
            "outcome": "success",
        },
    )]
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    assert len(lifecycle_calls) == 1

    # And the closed row is actually prunable, unlike the pre-fix leak.
    pruned = store._db.prune_sessions(older_than_days=0, source="webhook")
    assert pruned >= 1
    store._db.close()


@pytest.mark.asyncio
async def test_profile_routed_webhook_finalizer_uses_event_profile_scope(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)
    adapter = _make_adapter({"alerts": {"secret": _INSECURE_NO_AUTH, "deliver": "log"}})
    adapter.gateway_runner = runner
    event = _make_event(adapter, "alert-profile-001", "profile scoped")
    event.source.profile = "am-target"
    session_id = store.get_or_create_session(event.source).session_id
    active_profiles = []
    observed = []

    @contextmanager
    def profile_scope(source):
        active_profiles.append(source.profile)
        try:
            yield
        finally:
            active_profiles.pop()

    monkeypatch.setattr(runner, "_profile_scope_for_source", profile_scope)

    def checked(hook_name, **kwargs):
        observed.append((hook_name, active_profiles[-1], kwargs["session_id"]))
        return [], True

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook_checked", checked)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert observed == [("on_session_end", "am-target", session_id)]
    store._db.close()


@pytest.mark.asyncio
async def test_failed_webhook_delivery_fires_session_end_for_claim_finalizers(tmp_path, monkeypatch):
    """A failed model turn still gives trusted plugins a fenced cleanup boundary."""
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)
    adapter = _make_adapter({
        "alerts": {
            "secret": _INSECURE_NO_AUTH,
            "prompt": "Alert: {message}",
            "deliver": "log",
        }
    })
    adapter.gateway_runner = runner
    lifecycle_calls = []
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook_checked",
        lambda hook_name, **kwargs: (lifecycle_calls.append((hook_name, kwargs)), ([], True))[1],
    )
    created = {}

    async def _caught_failure_handler(event: MessageEvent):
        entry = store.get_or_create_session(event.source)
        created["session_id"] = entry.session_id
        event._hermes_turn_failed = True
        return "A safe error occurred"

    adapter._message_handler = _caught_failure_handler
    await adapter.handle_message(_make_event(adapter, "alert-fail-001", "fail safely"))
    await _drain_background_tasks(adapter)

    row = store._db.get_session(created["session_id"])
    assert row is not None
    assert row["ended_at"] is not None
    assert lifecycle_calls == [(
        "on_session_end",
        {
            "session_id": created["session_id"],
            "completed": False,
            "interrupted": False,
            "reason": "webhook_complete",
            "platform": "webhook",
            "outcome": "failure",
        },
    )]
    store._db.close()


@pytest.mark.asyncio
async def test_cancelled_webhook_delivery_reports_interrupted_once(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)
    adapter = _make_adapter({"alerts": {"secret": _INSECURE_NO_AUTH, "deliver": "log"}})
    adapter.gateway_runner = runner
    calls = []
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook_checked",
        lambda hook_name, **kwargs: (calls.append((hook_name, kwargs)), ([], True))[1],
    )
    event = _make_event(adapter, "alert-cancel-001", "cancel")
    session_id = store.get_or_create_session(event.source).session_id

    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    assert calls == [(
        "on_session_end",
        {
            "session_id": session_id,
            "completed": False,
            "interrupted": True,
            "reason": "webhook_complete",
            "platform": "webhook",
            "outcome": "cancelled",
        },
    )]
    store._db.close()


@pytest.mark.asyncio
async def test_incomplete_lifecycle_callback_remains_retryable(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    runner = _FakeRunner(store)
    adapter = _make_adapter({"alerts": {"secret": _INSECURE_NO_AUTH, "deliver": "log"}})
    adapter.gateway_runner = runner
    event = _make_event(adapter, "alert-retry-001", "retry")
    session_id = store.get_or_create_session(event.source).session_id
    completions = iter((False, True))
    calls = []

    def checked(hook_name, **kwargs):
        calls.append((hook_name, kwargs))
        return [], next(completions)

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook_checked", checked)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
    assert session_id not in adapter._finalized_sessions
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    assert len(calls) == 2
    assert session_id in adapter._finalized_sessions
    store._db.close()


