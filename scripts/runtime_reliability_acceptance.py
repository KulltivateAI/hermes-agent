#!/usr/bin/env python3
"""Offline release check; reads one profile, exercises TEMP objects, never starts a gateway.

Run with the selected source's interpreter before/after external installation.
This is constructed-object evidence, not inspection of a live cached agent.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

# Exact approved operational contract, not a new runtime default.
EXPECTED = {
    'tool_loop_guardrails.hard_stop_enabled': True,
    'tool_loop_guardrails.non_interactive_hard_stop_enabled': True,
    'tool_loop_guardrails.warnings_enabled': True,
    'tool_loop_guardrails.warn_after.exact_failure': 2,
    'tool_loop_guardrails.warn_after.same_tool_failure': 3,
    'tool_loop_guardrails.warn_after.idempotent_no_progress': 2,
    'tool_loop_guardrails.hard_stop_after.exact_failure': 5,
    'tool_loop_guardrails.hard_stop_after.same_tool_failure': 8,
    'tool_loop_guardrails.hard_stop_after.idempotent_no_progress': 5,
    'tool_loop_guardrails.loop_caps.max_web_searches': 50,
    'tool_loop_guardrails.loop_caps.max_subagents': 50,
    'compression.hygiene_hard_message_limit': 5000,
    'memory.nudge_interval': 0,
    'skills.creation_nudge_interval': 0,
    'display.busy_input_mode': 'steer',
}


def selected_config(config):
    """Copy only approved scalar settings; never carry credentials/personality/plugins."""
    selected, actual = {}, {}
    for path in EXPECTED:
        keys = path.split('.')
        value = config
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        actual[path] = value if type(value) in (bool, int, str) else None
        node = selected
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = actual[path]
    return selected, actual


def guardrail_checks(controller):
    """Exercise real observations; no shell, search, or delegation is launched."""
    from agent.tool_guardrails import ToolCallGuardrailController
    assertions = {}
    controller.reset_for_turn()
    continued = True
    for index in range(10):
        args = {'command': f'pytest fixture_{index}.py'}
        continued &= controller.before_call('terminal', args).allows_execution
        decision = controller.after_call('terminal', args, json.dumps({'exit_code': 1}), failed=True)
        continued &= not decision.should_halt
        controller.after_call('patch', {'path': 'fixture.py', 'change': index},
                              json.dumps({'success': True, 'files_modified': ['fixture.py']}), failed=False)
    assertions['different_red_suites_with_edits_continue'] = bool(continued)
    controller.reset_for_turn()
    args = {'command': 'pytest unchanged.py'}
    for _ in range(controller.config.exact_failure_block_after):
        controller.after_call('terminal', args, json.dumps({'exit_code': 1}), failed=True)
    assertions['unchanged_failure_stops'] = controller.before_call('terminal', args).should_halt
    # Independent caps must survive disabling optional loop detection.
    for tool, field in [('web_search', 'max_web_searches'), ('delegate_task', 'max_subagents')]:
        capped = ToolCallGuardrailController(replace(controller.config, hard_stop_enabled=False))
        limit = getattr(capped.config.loop_caps, field)
        allowed = True
        for index in range(limit):
            allowed &= capped.before_call(tool, {'fixture': index}).allows_execution
            capped.after_call(tool, {'fixture': index}, '{}', failed=False)
        assertions[f'{tool}_cap_survives_optional_disable'] = bool(
            allowed and capped.before_call(tool, {'fixture': limit}).should_halt)
    return assertions


async def steering_check(agent, runner):
    """Hold/release a harmless future while the real busy branch calls real agent.steer."""
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent
    from gateway.session import SessionSource
    started, release = asyncio.Event(), asyncio.Event()

    async def harmless_tool():
        started.set()
        await release.wait()
        return 'harmless completed'

    task = asyncio.create_task(harmless_tool())
    agent._executing_tools = True
    source = SessionSource(platform=Platform.DISCORD, chat_id='acceptance', user_id='fixture')
    event = MessageEvent(text='retain the current task and continue', source=source)
    runner._session_state('acceptance').turn.agent = agent
    try:
        await asyncio.wait_for(started.wait(), 5)
        await runner._hm_handle_running_session_message(event, source, 'acceptance')
        receipt = agent._drain_pending_steer()
        return (not task.done() and not agent._interrupt_requested and bool(receipt)
                and event.text in str(receipt))
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
        agent._executing_tools = False


def run_acceptance(profile_home):
    report = {'ok': False, 'source_root': str(SOURCE), 'interpreter': sys.executable,
              'construction': 'temporary fixture, not a live cached agent',
              'network_attempts': 0, 'assertions': {}}

    def deny_network(*args, **kwargs):
        report['network_attempts'] += 1
        import traceback
        if report['network_attempts'] <= 5:
            report.setdefault('network_callers', []).append([
                f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}'
                for frame in traceback.extract_stack(limit=40)[:-1]
                if Path(frame.filename).is_relative_to(SOURCE)
            ])
        raise RuntimeError('Network forbidden in offline acceptance')

    # Keep imports, logs and caches away from the profile being examined.
    with tempfile.TemporaryDirectory(prefix='hermes-runtime-acceptance-') as temporary:
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'HOME': temporary, 'HERMES_HOME': temporary,
                                                        'PYTHONDONTWRITEBYTECODE': '1'}))
            for target in ['socket.socket.connect', 'socket.socket.connect_ex', 'socket.create_connection', 'socket.getaddrinfo']:
                stack.enter_context(patch(target, deny_network))
            stack.enter_context(redirect_stdout(io.StringIO()))
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            from hermes_cli.config import load_config_readonly
            # Read the target's actual file through the canonical loader, while
            # keeping bootstrap/normalization caches in TEMP. The loader's name
            # means shared-object access, not filesystem side-effect freedom.
            # Parsing, defaults, env expansion and machine-managed policy stay real.
            with patch('hermes_cli.config.get_config_path', return_value=profile_home / 'config.yaml'):
                config, actual = selected_config(load_config_readonly())
            report['assertions']['approved_profile_settings'] = all(
                type(actual[key]) is type(value) and actual[key] == value for key, value in EXPECTED.items())
            home = Path(temporary) / 'runtime'
            home.mkdir()
            # Explicit fixture provider metadata avoids unrelated endpoint discovery.
            # None of these construction-only values are applied to the inspected profile.
            config['model'] = {'default': 'openai/gpt-4.1-mini', 'provider': 'custom',
                               'base_url': 'https://offline.invalid/v1', 'context_length': 128000}
            (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
            os.environ['HERMES_HOME'] = str(home)
            token = set_hermes_home_override(str(home))
            stack.callback(reset_hermes_home_override, token)
            # Binary installation is not part of this offline construction check.
            # Do not disable security policy or alter the inspected profile; replace
            # only the unrelated downloader entry point before runtime construction.
            stack.enter_context(patch('tools.tirith_security.ensure_installed', return_value=None))
            report['fixture_substitutions'] = ['provider client', 'tirith binary bootstrap']
            from run_agent import AIAgent
            from gateway.run import GatewayRunner
            from gateway.config import GatewayConfig
            from agent.tool_guardrails import ToolCallGuardrailConfig
            stack.enter_context(patch('agent.process_bootstrap.OpenAI', return_value=MagicMock()))
            agent = AIAgent(api_key='offline-fixture-key', base_url='https://offline.invalid/v1',
                            model='openai/gpt-4.1-mini', provider='custom', platform='discord',
                            session_id='runtime-acceptance-fixture', max_iterations=10,
                            enabled_toolsets=[], quiet_mode=True, skip_context_files=True,
                            skip_memory=True, skip_background_review=True, save_trajectories=False)
            resolved = ToolCallGuardrailConfig.from_mapping(config['tool_loop_guardrails'], platform='discord')
            report['assertions']['agent_received_discord_config'] = agent._tool_guardrails.config == resolved
            report['assertions'].update(guardrail_checks(agent._tool_guardrails))
            runner = GatewayRunner(config=GatewayConfig())
            report['assertions']['effective_busy_mode'] = runner._effective_busy_input_mode(None) == 'steer'
            report['assertions']['steer_does_not_abort_tool'] = asyncio.run(steering_check(agent, runner))
            modules = ['run_agent', 'agent.agent_init', 'agent.tool_guardrails', 'gateway.run',
                       'gateway.run_config_loaders', 'gateway.run_inbound', 'hermes_cli.config']
            report['imports'] = {name: str(Path(sys.modules[name].__file__).resolve()) for name in modules}
            report['assertions']['source_import_identity'] = all(
                Path(path).is_relative_to(SOURCE) for path in report['imports'].values())
    git_env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'],
                            env=git_env, capture_output=True, text=True, timeout=5)
    report['source_sha'] = result.stdout.strip() if result.returncode == 0 else None
    status = subprocess.run(['git', '-C', str(SOURCE), 'status', '--porcelain'],
                            env=git_env, capture_output=True, text=True, timeout=5)
    report['worktree_dirty'] = bool(status.stdout.strip()) if status.returncode == 0 else None
    import hashlib
    report['harness_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report['assertions']['source_commit_identified'] = bool(report['source_sha'])
    report['ok'] = all(report['assertions'].values()) and report['network_attempts'] == 0
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile-home', type=Path, required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker:
        # Keep import-time AND process-exit cleanup inside a disposable process.
        # Restoring the caller's HERMES_HOME before Python exits would let late
        # browser cleanup recreate caches inside the inspected live profile.
        with tempfile.TemporaryDirectory(prefix='hermes-acceptance-worker-') as home:
            env = dict(os.environ, HOME=home, HERMES_HOME=home, PYTHONDONTWRITEBYTECODE='1')
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                         '--worker', '--profile-home', str(args.profile_home.resolve())],
                                        cwd=home, env=env, text=True, capture_output=True, timeout=35)
            except subprocess.TimeoutExpired:
                print(json.dumps({'ok': False, 'error_type': 'WorkerTimeout'}))
                return 1
            # Never forward arbitrary startup/shutdown output or stderr.
            try:
                report = json.loads(result.stdout)
            except (ValueError, TypeError):
                report = {'ok': False, 'error_type': 'InvalidWorkerReport'}
            print(json.dumps(report, sort_keys=True))
            return 0 if result.returncode == 0 and report.get('ok') is True else 1
    try:
        report = run_acceptance(args.profile_home.resolve())
    except Exception as exc:
        # Exception messages can contain runtime settings; emit only a class name.
        report = {'ok': False, 'error_type': type(exc).__name__, 'source_root': str(SOURCE)}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
