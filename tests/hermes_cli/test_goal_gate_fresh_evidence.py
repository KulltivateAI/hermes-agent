"""Native goal gates must execute fresh evidence, not replay git-status receipts."""

import subprocess
import sys

import pytest

from hermes_cli.goals import GoalManager


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
    ).stdout


@pytest.mark.parametrize("other_worktree", [False, True])
def test_gate_reruns_after_repeated_dirty_edits(tmp_path, monkeypatch, other_worktree):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    gate_script = repo / "gate.py"
    gate_script.write_text(
        "from pathlib import Path\n"
        "value = Path(__file__).with_name('value').read_text().strip()\n"
        "print('actual gate value=' + value)\n"
        "raise SystemExit(int(value))\n"
    )
    (repo / "value").write_text("0")
    git(repo, "add", "gate.py", "value")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "fixture")
    target = repo
    if other_worktree:
        target = tmp_path / "target"
        git(repo, "worktree", "add", "--detach", str(target), "HEAD")
    monkeypatch.chdir(repo)
    mgr = GoalManager(session_id="gate-fresh-evidence")
    mgr.set("fixture goal")
    mgr.add_gate(f'"{sys.executable}" "{target / "gate.py"}"')

    (target / "value").write_text("1")
    first_status = git(repo, "status", "--porcelain")
    assert mgr._check_gates()["verdict"] == "gate_failed"
    gate = mgr.state.gates[0]
    assert gate.last_exit_code == 1
    assert "actual gate value=1" in gate.last_output_tail

    # Same dirty-path listing and HEAD, different bytes (or different worktree).
    (target / "value").write_text("2")
    assert git(repo, "status", "--porcelain") == first_status
    assert mgr._check_gates()["verdict"] == "gate_failed"
    assert gate.last_exit_code == 2, "replayed stale failure instead of executing gate"
    assert "actual gate value=2" in gate.last_output_tail

    (target / "value").write_text("0")
    assert mgr._check_gates() is None
    assert gate.last_exit_code == 0
    assert "actual gate value=0" in gate.last_output_tail
    assert gate.attempts == 0
