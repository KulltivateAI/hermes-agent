"""Execute the literal attribution workflow in real disposable Git repositories."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/contributor-check.yml"
KNOWN = "mapped@example.test"
NEW = "new@example.test"


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=True,
    ).stdout.strip()


def commit(repo, email):
    git(repo, "-c", "user.name=Fixture", "-c", f"user.email={email}",
        "commit", "--allow-empty", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "commit.gpgsign", "false")
    git(tmp_path, "config", "core.hooksPath", "/dev/null")
    root = commit(tmp_path, KNOWN)
    git(tmp_path, "update-ref", "refs/remotes/origin/main", root)
    mappings = tmp_path / "contributors/emails"
    mappings.mkdir(parents=True)
    (mappings / KNOWN).write_text("fixture\n")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/release.py", tmp_path / "scripts/release.py")
    return tmp_path


def run_workflow(repo, event, name="pull_request"):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["check-attribution"]["steps"]
                if s.get("id") == "check-emails")
    # No reimplementation of the gate/range, interpolation, or mocked Git.
    script = step["run"]
    assert "${{" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    event_path = repo / "event.json"
    event_path.write_text(json.dumps(event))
    output = repo / "outputs"
    output.unlink(missing_ok=True)
    (repo / "review-status.json").unlink(missing_ok=True)
    return subprocess.run(
        ["bash", "-e", "-c", script], cwd=repo, text=True, capture_output=True,
        env={**os.environ, "GITHUB_EVENT_NAME": name,
             "GITHUB_EVENT_PATH": str(event_path), "GITHUB_OUTPUT": str(output)},
    )


def event(base, head):
    return {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}


@pytest.mark.parametrize("non_main", [False, True])
@pytest.mark.parametrize("unmapped_position", [None, 0, 1])
def test_actual_pr_range_excludes_base_and_checkout_but_checks_every_pr_commit(
    repo, non_main, unmapped_position,
):
    if non_main:
        git(repo, "checkout", "-b", "integration")
        commit(repo, "unrelated-history@example.test")
    base = git(repo, "rev-parse", "HEAD")
    head = base
    for position in range(2):
        head = commit(repo, NEW if position == unmapped_position else KNOWN)
    # Recreate a synthetic checkout merge with a base-side commit outside the event.
    git(repo, "checkout", "-b", "checkout-only", base)
    commit(repo, "checkout-only@example.test")
    git(repo, "-c", "user.name=Fixture", "-c", f"user.email={KNOWN}",
        "merge", "--no-ff", head, "-m", "synthetic merge")
    result = run_workflow(repo, event(base, head))
    assert result.returncode == (0 if unmapped_position is None else 1), result.stdout + result.stderr
    if unmapped_position is not None:
        report = (repo / "review-status.json").read_text()
        assert NEW in report
        assert "unrelated-history@" not in report
        assert "checkout-only@" not in report
    else:
        assert (repo / "review-status.json").read_text() == "review_status=[]\n"


@pytest.mark.parametrize("field", ["base", "head"])
@pytest.mark.parametrize("value", [None, "", "main", "$(touch injected)", "a" * 39, ["a" * 40]])
def test_invalid_pr_sha_fails_closed(repo, field, value):
    sha = git(repo, "rev-parse", "HEAD")
    payload = event(sha, sha)
    payload["pull_request"][field] = {} if value is None else {"sha": value}
    result = run_workflow(repo, payload)
    assert result.returncode != 0
    assert not (repo / "injected").exists()
    assert not (repo / "review-status.json").exists()


def test_missing_pr_payload_fails_closed(repo):
    assert run_workflow(repo, {}).returncode != 0
    assert run_workflow(repo, None).returncode != 0


@pytest.mark.parametrize("object_kind", ["unavailable", "blob", "unrelated"])
def test_invalid_commit_objects_fail_closed(repo, object_kind):
    head = git(repo, "rev-parse", "HEAD")
    if object_kind == "unavailable":
        base = "f" * 40
    elif object_kind == "blob":
        base = git(repo, "hash-object", "-w", "scripts/release.py")
    else:
        git(repo, "checkout", "--orphan", "unrelated")
        base = commit(repo, "separate-root@example.test")
        assert base != head
    assert run_workflow(repo, event(base, head)).returncode != 0
    assert not (repo / "review-status.json").exists()


def test_fetches_missing_pr_objects_from_real_origin(repo, tmp_path_factory):
    base = git(repo, "rev-parse", "HEAD")
    upstream = tmp_path_factory.mktemp("upstream")
    git(upstream, "clone", "--bare", str(repo), "origin.git")
    remote = upstream / "origin.git"
    git(repo, "remote", "add", "origin", str(remote))
    writer = upstream / "writer"
    git(upstream, "clone", str(remote), str(writer))
    git(writer, "config", "commit.gpgsign", "false")
    git(writer, "config", "core.hooksPath", "/dev/null")
    head = commit(writer, NEW)
    git(writer, "push", "origin", "HEAD:refs/heads/feature")
    missing = subprocess.run(["git", "cat-file", "-e", head], cwd=repo, capture_output=True)
    assert missing.returncode != 0
    result = run_workflow(repo, event(base, head))
    assert result.returncode == 1, result.stdout + result.stderr
    assert git(repo, "cat-file", "-t", head) == "commit"
    assert NEW in (repo / "review-status.json").read_text()


@pytest.mark.parametrize("caught_up", [False, True])
def test_push_preserves_existing_merge_base_to_checkout_behavior(repo, caught_up):
    head = commit(repo, NEW)
    if caught_up:
        git(repo, "update-ref", "refs/remotes/origin/main", head)
    # No PR fields on push; importantly, event.before must not invent an audit.
    result = run_workflow(repo, {"before": "f" * 40}, name="push")
    assert result.returncode == (0 if caught_up else 1), result.stdout + result.stderr
    if caught_up:
        assert "No new commits to check." in result.stdout
    else:
        assert NEW in (repo / "review-status.json").read_text()


def test_verified_current_author_mapping_passes_real_gate(repo):
    base = git(repo, "rev-parse", "HEAD")
    email = "hello@kulltivate.ai"
    mapping = ROOT / "contributors/emails" / email
    assert mapping.read_text().strip() == "KulltivateAI"
    shutil.copyfile(mapping, repo / "contributors/emails" / email)
    head = commit(repo, email)
    result = run_workflow(repo, event(base, head))
    assert result.returncode == 0, result.stdout + result.stderr
