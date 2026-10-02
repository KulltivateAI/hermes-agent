from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_dispatch_task_parser_accepts_exact_id():
    from hermes_cli import kanban as cli

    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["kanban", "dispatch-task", "t_exact", "--json"])
    assert args.task_id == "t_exact"
    assert args.json is True


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _status(conn, task_id: str) -> str:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task.status


def test_exact_dispatch_task_spawns_only_selected_without_board_sweep(
    kanban_home, all_assignees_spawnable,
):
    spawned: list[str] = []
    with kbc.connect() as conn:
        selected = kb.create_task(conn, title="selected", assignee="alice")
        unrelated = kb.create_task(conn, title="unrelated", assignee="bob")
        stale = kb.create_task(conn, title="stale running", assignee="carol")
        kb.claim_task(conn, stale, ttl_seconds=1)

        result = kbd.dispatch_task(
            conn, selected,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 123,
        )

        assert spawned == [selected]
        assert [item[0] for item in result.spawned] == [selected]
        assert _status(conn, unrelated) == "ready"
        assert _status(conn, stale) == "running"


def test_exact_dispatch_task_retry_does_not_spawn_twice(
    kanban_home, all_assignees_spawnable,
):
    spawned: list[str] = []
    with kbc.connect() as conn:
        selected = kb.create_task(conn, title="selected", assignee="alice")
        first = kbd.dispatch_task(
            conn, selected,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 123,
        )
        second = kbd.dispatch_task(
            conn, selected,
            spawn_fn=lambda task, workspace, board=None: spawned.append(task.id) or 456,
        )

        assert len(first.spawned) == 1
        assert second.spawned == []
        assert spawned == [selected]
        runs = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (selected,)).fetchone()[0]
        assert runs == 1


@pytest.mark.parametrize("state", ["missing", "done", "archived"])
def test_exact_dispatch_task_missing_or_terminal_is_noop(
    kanban_home, all_assignees_spawnable, state,
):
    with kbc.connect() as conn:
        selected = "t_missing"
        if state != "missing":
            selected = kb.create_task(conn, title=state, assignee="alice")
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (state, selected))
        result = kbd.dispatch_task(conn, selected, dry_run=True)
        assert result.spawned == []
