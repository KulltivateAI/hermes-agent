"""Invocation identity/authority must win over current and legacy snapshots."""

from concurrent.futures import ThreadPoolExecutor

from pathlib import Path
import json
import shlex
import sys
import threading

import pytest

from agent.delegation_context import delegated_child_context, DELEGATED_CHILD_ENV_MARKER
from gateway.session_context import scoped_current_session_id
from tools.environments.local import LocalEnvironment


@pytest.fixture
def shell(tmp_path, monkeypatch):
    # Never source or rewrite a live snapshot, nor read the user's shell rc.
    monkeypatch.setattr(LocalEnvironment, "get_temp_dir", lambda self: str(tmp_path))
    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    Path(env._snapshot_path).write_text("export ORDINARY_SHELL_STATE=preserved\n")
    env._snapshot_ready = True
    try:
        yield env
    finally:
        env.cleanup()


def run_as(shell, child, sid, barrier=None):
    with delegated_child_context(sid) if child else scoped_current_session_id(sid):
        if barrier:
            barrier.wait(timeout=10)
        # Actual subprocess guard, not a locally reimplemented permission rule.
        code = (
            "import os,json; from argparse import Namespace; "
            "from hermes_cli.kanban import _is_delegated_child_cli_mutation as denied; "
            "print(json.dumps({'marker':os.getenv('HERMES_DELEGATED_CHILD_CONTEXT'),"
            "'sid':os.getenv('HERMES_SESSION_ID'),"
            "'state':os.getenv('ORDINARY_SHELL_STATE'),"
            "'denied':denied(Namespace(kanban_action='comment'))}))"
        )
        result = shell.execute(f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}")
        assert result["returncode"] == 0, result
        return json.loads(result["output"].strip())


def assert_identity(result, child, sid):
    assert result["marker"] == ("1" if child else None), result
    assert result["denied"] is child, result
    assert result["sid"] == sid, result
    assert result["state"] == "preserved", result


@pytest.mark.parametrize("order", [(True, False), (False, True)])
def test_sequential_authority_is_invocation_local(shell, order):
    for index, child in enumerate(order):
        sid = f"session-{index}"
        assert_identity(run_as(shell, child, sid), child, sid)
    snapshot = Path(shell._snapshot_path).read_text()
    assert DELEGATED_CHILD_ENV_MARKER not in snapshot
    assert "HERMES_SESSION_ID" not in snapshot


@pytest.mark.parametrize("child", [False, True])
def test_legacy_snapshot_cannot_override_incoming_authority(shell, child):
    with open(shell._snapshot_path, "a") as stream:
        stream.write(
            f'export HERMES_DELEGATED_CHILD_CONTEXT={"" if child else "1"}\n'
            'export HERMES_SESSION_ID=stale-session\n'
        )
    assert_identity(run_as(shell, child, "current-session"), child, "current-session")


def test_concurrent_child_parent_share_snapshot_not_authority(shell):
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        parent = pool.submit(run_as, shell, False, "parent", barrier)
        child = pool.submit(run_as, shell, True, "child", barrier)
        assert_identity(parent.result(timeout=30), False, "parent")
        assert_identity(child.result(timeout=30), True, "child")


def test_inherited_child_lineage_remains_denied(shell, monkeypatch):
    # A real delegated subprocess inherits the marker rather than ContextVars.
    monkeypatch.setenv(DELEGATED_CHILD_ENV_MARKER, "1")
    assert_identity(run_as(shell, False, "inherited-child"), True, "inherited-child")
