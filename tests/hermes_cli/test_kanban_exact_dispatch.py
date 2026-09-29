from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_status(conn, task_id: str, status: str) -> None:
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def _status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


def test_exact_ready_dispatch_spawns_only_selected(
    kanban_home, all_assignees_spawnable,
):
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)

    with kbc.connect() as conn:
        selected = kb.create_task(conn, title="selected", assignee="alice")
        unrelated_ready = kb.create_task(conn, title="other ready", assignee="bob")
        unrelated_review = kb.create_task(conn, title="other review", assignee="carol")
        _set_status(conn, unrelated_review, "review")

        result = kbd.dispatch_once(
            conn, spawn_fn=fake_spawn, task_ids={selected}
        )

        assert spawned == [selected]
        assert [item[0] for item in result.spawned] == [selected]
        assert _status(conn, unrelated_ready) == "ready"
        assert _status(conn, unrelated_review) == "review"


def test_exact_review_dispatch_spawns_selected_review(
    kanban_home, all_assignees_spawnable,
):
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)

    with kbc.connect() as conn:
        selected = kb.create_task(conn, title="selected review", assignee="alice")
        unrelated = kb.create_task(conn, title="other ready", assignee="bob")
        _set_status(conn, selected, "review")

        result = kbd.dispatch_once(
            conn, spawn_fn=fake_spawn, task_ids={selected}
        )

        assert spawned == [selected]
        assert [item[0] for item in result.spawned] == [selected]
        assert _status(conn, unrelated) == "ready"


def test_exact_review_respects_global_in_progress_cap(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        running = kb.create_task(conn, title="running", assignee="alice")
        kb.claim_task(conn, running)
        selected = kb.create_task(conn, title="selected review", assignee="bob")
        _set_status(conn, selected, "review")

        result = kbd.dispatch_once(
            conn,
            dry_run=True,
            max_in_progress=1,
            task_ids={selected},
        )

        assert result.spawned == []
        selected_task = kb.get_task(conn, selected)
        assert selected_task is not None
        assert selected_task.status == "review"


def test_concurrent_exact_dispatch_claims_once(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        selected = kb.create_task(conn, title="selected", assignee="alice")

    def dispatch(_):
        with kbc.connect() as conn:
            return kbd.dispatch_once(
                conn,
                spawn_fn=lambda task, workspace, board=None: None,
                task_ids={selected},
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(dispatch, range(2)))

    assert sum(len(result.spawned) for result in results) == 1


@pytest.mark.parametrize("task_state", ["missing", "done", "archived"])
def test_exact_missing_or_terminal_spawns_zero(
    kanban_home, all_assignees_spawnable, task_state,
):
    with kbc.connect() as conn:
        unrelated = kb.create_task(conn, title="unrelated", assignee="alice")
        if task_state == "missing":
            selected = "t_missing"
        else:
            selected = kb.create_task(conn, title=task_state, assignee="bob")
            _set_status(conn, selected, task_state)

        result = kbd.dispatch_once(
            conn, dry_run=True, task_ids={selected}
        )

        assert result.spawned == []
        assert _status(conn, unrelated) == "ready"


def test_exact_selector_parameterizes_sql_metacharacters(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        unrelated = kb.create_task(conn, title="unrelated", assignee="alice")
        result = kbd.dispatch_once(
            conn,
            dry_run=True,
            task_ids={"t_missing') OR 1=1 --"},
        )

        assert result.spawned == []
        assert _status(conn, unrelated) == "ready"
