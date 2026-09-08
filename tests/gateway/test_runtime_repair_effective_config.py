"""The release harness reads configuration, constructs real runtime seams, and never sends."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('drift', [None, 'hard_stops', 'busy_mode'])
def test_offline_acceptance_checks_real_effective_settings_without_profile_writes(tmp_path, drift):
    profile = tmp_path / 'profile'
    profile.mkdir()
    config = {
        'tool_loop_guardrails': {
            'hard_stop_enabled': True, 'non_interactive_hard_stop_enabled': True,
            'warnings_enabled': True,
            'warn_after': {'exact_failure': 2, 'same_tool_failure': 3, 'idempotent_no_progress': 2},
            'hard_stop_after': {'exact_failure': 5, 'same_tool_failure': 8, 'idempotent_no_progress': 5},
            'loop_caps': {'max_web_searches': 50, 'max_subagents': 50},
        },
        'compression': {'hygiene_hard_message_limit': 5000},
        'memory': {'nudge_interval': 0}, 'skills': {'creation_nudge_interval': 0},
        'display': {'busy_input_mode': 'steer'},
        'unrelated_private_setting': 'DO-NOT-EMIT-THIS-SENTINEL',
    }
    if drift == 'hard_stops':
        config['tool_loop_guardrails']['hard_stop_enabled'] = False
        config['tool_loop_guardrails']['non_interactive_hard_stop_enabled'] = False
    if drift == 'busy_mode':
        config['display']['busy_input_mode'] = 'interrupt'
    path = profile / 'config.yaml'
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(profile),
           'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(ROOT), 'HERMES_TEST_ISOLATION': '1'}
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/runtime_reliability_acceptance.py'),
                             '--profile-home', str(profile)], env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == (0 if drift is None else 1), result.stdout + result.stderr
    assert 'DO-NOT-EMIT-THIS-SENTINEL' not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['ok'] is (drift is None)
    assert report['construction'] == 'temporary fixture, not a live cached agent'
    assert report['network_attempts'] == 0
    assert report['source_root'] == str(ROOT)
    assert report['assertions']['effective_busy_mode'] is (drift != 'busy_mode')
    assert report['assertions']['steer_does_not_abort_tool'] is True
    assert path.read_bytes() == before
    assert sorted(p.name for p in profile.iterdir()) == ['config.yaml'], [str(p.relative_to(profile)) for p in profile.rglob('*')]
