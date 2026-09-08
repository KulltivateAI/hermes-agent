"""Execute the actual contributor workflow shell on real disposable Git histories."""
import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args, email='mapped@example.invalid'):
    env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
               GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email)
    return subprocess.run(['git', '-C', str(repo), *args], env=env, text=True,
                          capture_output=True, check=True).stdout.strip()


def commit(repo, email='mapped@example.invalid'):
    git(repo, 'commit', '--allow-empty', '-m', 'fixture', email=email)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-b', 'main')
    first = commit(repo, 'historical@example.invalid')
    git(repo, 'update-ref', 'refs/remotes/origin/main', first)
    base = commit(repo, 'before-base@example.invalid')
    git(repo, 'checkout', '-b', 'topic')
    head = commit(repo)
    (repo / 'contributors/emails').mkdir(parents=True)
    (repo / 'contributors/emails/mapped@example.invalid').write_text('fixture\n')
    (repo / 'scripts').mkdir()
    (repo / 'scripts/release.py').write_text('LEGACY_AUTHOR_MAP = {}\n')
    return repo, base, head


def execute(repo, event, *, name='pull_request'):
    workflow = yaml.safe_load((ROOT / '.github/workflows/contributor-check.yml').read_text())
    step = next(s for s in workflow['jobs']['check-attribution']['steps'] if s.get('id') == 'check-emails')
    script = step['run']
    event_path, output = repo / 'event.json', repo / 'output'
    event_path.write_text(json.dumps(event)); output.write_text('')
    status = repo / 'review-status.json'
    status.unlink(missing_ok=True)
    env = dict(os.environ, GITHUB_EVENT_NAME=name, GITHUB_EVENT_PATH=str(event_path), GITHUB_OUTPUT=str(output))
    result = subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', script], cwd=repo,
                            env=env, text=True, capture_output=True, timeout=45)
    return result, output.read_text()


def event(base, head):
    return {'pull_request': {'base': {'sha': base}, 'head': {'sha': head}}}


@pytest.mark.parametrize('base_branch', ['main', 'review-base/runtime'])
def test_only_pr_event_head_authors_even_with_base_history_and_synthetic_merge(history, base_branch):
    repo, base, head = history
    git(repo, 'checkout', '-B', base_branch, base)
    advanced_base = commit(repo, 'base-side@example.invalid')
    git(repo, 'merge', '--no-ff', 'topic', '-m', 'synthetic fixture')
    result, output = execute(repo, event(advanced_base, head))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'review_status=[]' in output


def test_unknown_head_author_is_blocking_even_when_checkout_is_base(history):
    repo, base, _ = history
    head = commit(repo, 'unknown@example.invalid')
    git(repo, 'checkout', '--detach', base)
    result, output = execute(repo, event(base, head))
    assert result.returncode == 1
    assert 'unknown@example.invalid' in result.stdout and 'action_required' in output
    assert 'before-base@example.invalid' not in result.stdout


@pytest.mark.parametrize('bad', [None, '', 'a'*39, 'a'*41, '$(touch injected)', 'refs/heads/topic', ['a'*40]])
def test_missing_or_malformed_sha_is_not_a_success(history, bad):
    repo, base, head = history
    (repo / 'contributors/emails/before-base@example.invalid').write_text('fixture\n')
    for payload in ({}, event(bad, head), event(base, bad)):
        result, output = execute(repo, payload)
        assert result.returncode != 0
        assert 'review_status=[]' not in output
        assert not (repo / 'injected').exists()


def test_noncommit_unrelated_and_unavailable_objects_fail_closed(history):
    repo, base, head = history
    blob = git(repo, 'hash-object', '-w', '--stdin')
    git(repo, 'tag', '-a', 'annotated', head, '-m', 'tag')
    tag = git(repo, 'rev-parse', 'annotated')
    git(repo, 'checkout', '--orphan', 'unrelated')
    unrelated = commit(repo)
    git(repo, 'checkout', '--detach', head)
    (repo / 'contributors/emails/before-base@example.invalid').write_text('fixture\n')
    for bad in (blob, tag, unrelated, 'f'*40):
        result, output = execute(repo, event(base, bad))
        assert result.returncode != 0
        assert 'review_status=[]' not in output


def test_missing_commit_is_fetched_from_existing_origin(history, tmp_path):
    repo, base, head = history
    remote = tmp_path / 'remote'
    git(repo, 'clone', '--no-hardlinks', str(repo), str(remote))
    new_head = commit(remote)
    git(repo, 'remote', 'add', 'origin', str(remote))
    assert subprocess.run(['git','-C',str(repo),'cat-file','-e',new_head],capture_output=True).returncode != 0
    result, output = execute(repo, event(base, new_head))
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'review_status=[]' in output
    assert git(repo, 'cat-file', '-t', new_head) == 'commit'


def test_push_preserves_no_new_pr_attribution(history):
    repo, _, _ = history
    commit(repo, 'push-only@example.invalid')
    result, output = execute(repo, {}, name='push')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'review_status=[]' in output
    assert 'push-only@example.invalid' not in result.stdout
