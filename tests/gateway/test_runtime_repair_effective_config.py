"""The release harness reads configuration, constructs real runtime seams, and never sends."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('cold_bootstrap', [False, True], ids=['normal', 'cold'])
@pytest.mark.parametrize('drift', [None, 'hard_stops', 'busy_mode'])
def test_offline_acceptance_checks_real_effective_settings_without_profile_writes(tmp_path, drift, cold_bootstrap):
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
    path.write_text(json.dumps(config), encoding='utf-8')
    before = path.read_bytes()
    env = {'HOME': str(tmp_path), 'HERMES_HOME': str(profile),
           'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
           'PYTHONPATH': str(ROOT), 'HERMES_TEST_ISOLATION': '1'}
    command = [sys.executable, str(ROOT / 'scripts/runtime_reliability_acceptance.py'),
               '--profile-home', str(profile)]
    if cold_bootstrap:
        # Force the same cold binary-lookup seam as hosted CI, without a download.
        # Keep process-exit cleanup in TEMP, not the inspected profile.
        env['HERMES_HOME'] = str(tmp_path / 'worker-home')
        command = [sys.executable, '-c', """
import json, sys
from pathlib import Path
from unittest.mock import patch
from scripts.runtime_reliability_acceptance import run_acceptance
with patch('tools.tirith_security._find_local_tirith', return_value=None):
    report = run_acceptance(Path(sys.argv[1]))
print(json.dumps(report))
raise SystemExit(0 if report['ok'] else 1)
""", str(profile)]
    result = subprocess.run(command, env=env, cwd=tmp_path,
                            text=True, capture_output=True, timeout=45)
    assert result.returncode == (0 if drift is None else 1), result.stdout + result.stderr
    assert 'DO-NOT-EMIT-THIS-SENTINEL' not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['ok'] is (drift is None)
    assert report['construction'] == 'temporary fixture, not a live cached agent'
    assert report['network_attempts'] == 0
    # Even deliberately incorrect settings must reach the real agent. A negative
    # profile verdict alone can mask a disconnected runtime configuration path.
    assert report['assertions']['agent_received_discord_config'] is True
    assert report['source_root'] == str(ROOT)
    assert report['assertions']['effective_busy_mode'] is (drift != 'busy_mode')
    assert report['assertions']['steer_does_not_abort_tool'] is True
    assert path.read_bytes() == before
    assert sorted(p.name for p in profile.iterdir()) == ['config.yaml'], [str(p.relative_to(profile)) for p in profile.rglob('*')]
