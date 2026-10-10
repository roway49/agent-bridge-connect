#!/usr/local/bin/python3
"""Mac development image: isolate selected CLI homes/env from Runner and peers."""
import json
import os
import platform
from pathlib import Path
import sys

executor = Path(sys.argv[0]).name
architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(platform.machine())
if architecture is None:
    raise SystemExit('Unsupported Linux runtime architecture')
commands = {
    'codex': '/opt/codex/bin/codex',
    'claude': '/opt/claude/claude',
    'hermes': f'/probe/hermes-{architecture}/bin/hermes',
    'opencode': '/opt/opencode/opencode',
}
if not Path(commands['codex']).is_file() and Path('/opt/cli/bin/codex').is_file():
    commands['codex'] = '/opt/cli/bin/codex'
if executor not in commands:
    raise SystemExit('Unknown executor launcher')
home = Path('/profiles') / executor
home.mkdir(mode=0o700, parents=True, exist_ok=True)
env = dict(os.environ)
env['HOME'] = str(home)
# Do not inherit sibling runtime-location overrides from a shared Runner process.
for key in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'HERMES_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME'):
    env.pop(key, None)
selected_environment = home / 'agentbc-env.json'
if selected_environment.is_file():
    from agent_bridge_connect.docker_profiles import read_environment
    env.update(read_environment(executor, selected_environment))
if executor == 'hermes':
    selected = home / 'probe-env.json'
    if selected.is_file():
        config = json.loads(selected.read_text())
        allowed = {'GLM_API_KEY', 'GLM_BASE_URL', 'ZAI_API_KEY', 'Z_AI_API_KEY'}
        if set(config) - allowed or not all(isinstance(v, str) for v in config.values()):
            raise SystemExit('Unsupported Hermes environment mapping')
        env.update(config)
    env['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
if executor == 'claude':
    selected = home / '.claude/settings.json'
    if selected.is_file():
        settings = json.loads(selected.read_text())
        values = settings.get('env', {})
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise SystemExit('Invalid selected Claude environment')
        for key, value in values.items():
            if key.startswith('ANTHROPIC_') or key == 'CLAUDE_CODE_SUBAGENT_MODEL':
                env[key] = value
os.execve(commands[executor], [commands[executor], *sys.argv[1:]], env)
