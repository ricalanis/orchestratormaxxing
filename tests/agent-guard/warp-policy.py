#!/usr/bin/env python3
"""Root-owned policy contract through the real CLI, isolated HOME."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / 'bin/agent-guard'
DEFAULTS = ['bash', 'fish', 'pwsh', 'sh', 'zsh', 'curl', 'eval', 'exec', 'source',
            'wget', 'dig', 'nslookup', 'host', 'ssh', 'scp', 'rsync', 'telnet', 'rm']
with tempfile.TemporaryDirectory() as td:
    home = Path(td)
    env = dict(os.environ, HOME=td, XDG_CONFIG_HOME=str(home / '.config'),
               WARP_MODEL_PIN_LAYOUT='darwin', ORCHESTRATORMAXXING_GUARD_DIR=str(ROOT / 'deploy/agent-guard'))
    def run(*args):
        return subprocess.run([str(TOOL), *args], env=env, capture_output=True, text=True, timeout=15)
    assert json.loads(run('status', '--json').stdout)['hosts']['warp']['state'] == 'absent'
    assert not (home / '.warp').exists()
    assert run('warp', '--apply', '--settings', str(home / 'nonexistent.toml')).returncode == 2
    desktop = home / '.warp/settings.toml'
    cli = home / '.warp_cli/settings.toml'
    fixture = '# user comment\n[agents.execution_profiles.default]\ncommand_denylist = []\nuser_value = 47\n'
    for path in (desktop, cli):
        path.parent.mkdir(parents=True)
        path.write_text(fixture)
    assert run('warp', '--check').returncode == 1
    assert 'pattern(s) missing' in run('warp', '--check').stdout
    assert 'must be false' in run('warp', '--check').stdout
    r = run('warp', '--apply')
    assert r.returncode == 0, r.stderr
    for path in (desktop, cli):
        d = tomllib.loads(path.read_text())
        assert d['agents']['warp_agent']['other']['auto_approve_bypasses_command_denylist'] is False
        profile = d['agents']['execution_profiles']['default']
        assert profile['user_value'] == 47
        assert all(x + r'(\s.*)?' in profile['command_denylist'] for x in DEFAULTS)
    before = {p: p.read_bytes() for p in (desktop, cli)}
    assert run('warp', '--apply').returncode == 0
    assert all(p.read_bytes() == b for p, b in before.items())
    cli.write_text(cli.read_text().replace('auto_approve_bypasses_command_denylist = false',
                                           'auto_approve_bypasses_command_denylist = true # preserve this comment'))
    assert run('warp', '--check').returncode == 1, 'CLI-only drift must affect aggregate'
    assert json.loads(run('status', '--json').stdout)['hosts']['warp']['state'] == 'missing'
    assert run('warp', '--check', '--settings', str(desktop)).returncode == 0
    assert run('warp', '--apply').returncode == 0
    assert 'false # preserve this comment' in cli.read_text()
    assert json.loads(run('status', '--json').stdout)['hosts']['warp']['state'] == 'wired'
    for tail in ['\n[agents.warp_agent.other]\nauto_approve_bypasses_command_denylist = true\n',
                 '\n[agents.execution_profiles.default]\ncommand_denylist = []\n']:
        desktop.write_bytes(before[desktop] + tail.encode())
        original = desktop.read_bytes()
        assert run('warp', '--apply', '--settings', str(desktop)).returncode == 2
        assert desktop.read_bytes() == original
    # Existing target table without the managed key, both terminated and EOF.
    for suffix in ['\n[agents.warp_agent.other]\nother = 9\n', '\n[agents.warp_agent.other]']:
        desktop.write_text(fixture + suffix)
        assert run('warp', '--apply', '--settings', str(desktop)).returncode == 0
        assert tomllib.loads(desktop.read_text())['agents']['warp_agent']['other']['auto_approve_bypasses_command_denylist'] is False
    desktop.write_text(fixture.rstrip('\n'))
    assert run('warp', '--apply', '--settings', str(desktop)).returncode == 0
    tomllib.loads(desktop.read_text())
    bad_data = home / 'bad-data'
    bad_data.mkdir()
    (bad_data / 'warp-default-patterns.json').write_bytes((ROOT / 'deploy/agent-guard/warp-default-patterns.json').read_bytes())
    env['ORCHESTRATORMAXXING_GUARD_DIR'] = str(bad_data)
    old = desktop.read_bytes()
    assert run('warp', '--apply', '--settings', str(desktop)).returncode == 2
    assert desktop.read_bytes() == old
    env['ORCHESTRATORMAXXING_GUARD_DIR'] = str(ROOT / 'deploy/agent-guard')
    # A write failure must return the documented refusal, not escape as an error.
    import importlib.machinery
    from unittest.mock import patch
    import argparse
    module = importlib.machinery.SourceFileLoader('warp_policy_contract', str(TOOL)).load_module()
    desktop.write_text(fixture)
    with patch.dict(os.environ, env), patch.object(module, 'atomic_write_text', side_effect=OSError('contract write failure')):
        assert module.cmd_warp(argparse.Namespace(settings=str(desktop), apply=True)) == 2
    assert desktop.read_text() == fixture
    # Same documented CLI path on Linux, with XDG override.
    env['WARP_MODEL_PIN_LAYOUT'] = 'linux'
    linux = home / '.config/warp-terminal/cli/settings.toml'
    linux.parent.mkdir(parents=True)
    linux.write_text(fixture)
    assert run('warp', '--apply').returncode == 0
    assert run('warp', '--check').returncode == 0
print('PASS vendor defaults, bypass policy, dual settings, absence, refusal, XDG CLI')
