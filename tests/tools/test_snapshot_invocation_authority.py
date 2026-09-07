"""Invocation identity wins over legacy shared snapshots, including absent values."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from gateway.session_context import _VAR_MAP
from tools.environments import base_session_env as shell

NAMES = set(_VAR_MAP) | {"HERMES_DELEGATED_CHILD_CONTEXT", "AI_AGENT", "HERMES_AGENT", "PROFILE_TEST"}


def invoke(tmp_path, snap, incoming):
    command = (f'{shlex.quote(sys.executable)} -c ' + shlex.quote(
        'import os,json,subprocess,sys; from agent.delegation_context import is_delegated_child_process_context; '
        'import tools.environments.base_session_env as s; '
        'descendant = subprocess.check_output([sys.executable, "-c", '
        '"from agent.delegation_context import is_dispatcher_owned_worker_context; print(is_dispatcher_owned_worker_context())"], text=True).strip(); '
        'assert (descendant == "False") == is_delegated_child_process_context(); '
        f'print(json.dumps([{{k:os.environ.get(k) for k in {sorted(NAMES)!r}}}, '
        'is_delegated_child_process_context(), s.__file__]))'))
    script = shell._wrap_command_script(command, quoted_cwd=shlex.quote(str(tmp_path)),
        quoted_snap=shlex.quote(str(snap)), snap_tmp_template=shlex.quote(str(snap)+'.tmp.XXXXXXXXXX'),
        passthrough_names=['PROFILE_TEST'], snapshot_ready=True, cwd_marker='__TEST_CWD__')
    env = {k:v for k,v in os.environ.items() if k not in NAMES}
    env.update(incoming)
    result = subprocess.run(['bash','-c',script], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.splitlines()[0])


@pytest.mark.parametrize('legacy_marker', ['1', ''])
def test_incoming_authority_presence_and_child_parent_sequence(tmp_path, legacy_marker):
    snap = tmp_path / 'snapshot'
    legacy = ''.join(f'export {name}={shlex.quote(legacy_marker if name == "HERMES_DELEGATED_CHILD_CONTEXT" else "legacy")}\n' for name in NAMES)
    for incoming in ({}, {name:'' for name in NAMES}, {name:'incoming\nvalue' for name in NAMES},
                     {'HERMES_DELEGATED_CHILD_CONTEXT':'1'}, {}):
        snap.write_text(legacy)
        values, child, source = invoke(tmp_path, snap, incoming)
        expected = {name:incoming.get(name) for name in NAMES}
        for name, default in [('AI_AGENT','hermes-agent'),('HERMES_AGENT','true')]:
            expected[name] = incoming.get(name) or default
        assert values == expected
        assert child == bool(incoming.get('HERMES_DELEGATED_CHILD_CONTEXT'))
        assert Path(source).resolve() == Path(shell.__file__).resolve()
        assert not any(f' {name}=' in snap.read_text() for name in NAMES)


def test_concurrent_legacy_readers_and_bootstrap_dump(tmp_path):
    snap = tmp_path / 'snapshot'
    snap.write_text('export HERMES_DELEGATED_CHILD_CONTEXT=1\nexport ORDINARY=kept\n')
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: invoke(tmp_path, snap, {}), range(4)))
    assert all(not child and values['HERMES_DELEGATED_CHILD_CONTEXT'] is None for values,child,_ in results)
    script = shell._snapshot_bootstrap_script(quoted_cwd=shlex.quote(str(tmp_path)),
        quoted_snap=shlex.quote(str(snap)), snap_tmp_template=shlex.quote(str(snap)+'.tmp.XXXXXXXXXX'),
        excluded_names=['PROFILE_TEST'], cwd_marker='__TEST_CWD__')
    env = dict(os.environ, **{name:'multiline\nvalue' for name in NAMES})
    result = subprocess.run(['bash','-c','export ORDINARY=kept; public_fn() { :; }; alias public_alias=true;\n'+script],
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0
    text = snap.read_text()
    assert not any(f' {name}=' in text for name in NAMES)
    assert 'ORDINARY=' in text and 'public_fn' in text and 'public_alias' in text
    assert snap.stat().st_mode & 0o777 == 0o600
    assert str(tmp_path) in result.stdout
