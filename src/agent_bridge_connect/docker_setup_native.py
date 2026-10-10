"""Native setup questions with a Docker-specific configuration transport."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import load_config, toml_dumps
from .installation import InstallationError

# These host integrations cannot be executed faithfully inside the container.
# Only remove explicitly identified integration keys, never permission settings.
CODEX_HOST_KEYS = {'mcp_servers', 'notify', 'hooks', 'plugins'}
CLAUDE_HOST_KEYS = {'hooks', 'mcpServers', 'enabledPlugins', 'extraKnownMarketplaces',
                    'plugin', 'plugins', 'statusLine'}


def _codex_catalogs(value: dict, home: Path, files: list) -> None:
    """Copy explicitly referenced model metadata, never arbitrary integrations."""
    import hashlib
    sections = [value, *[v for v in value.get('profiles', {}).values() if isinstance(v, dict)]]
    for section in sections:
        reference = section.get('model_catalog_json')
        if reference is None:
            continue
        if not isinstance(reference, str) or not reference.strip():
            raise InstallationError('Codex model_catalog_json must name a JSON file')
        source = Path(reference).expanduser()
        if not source.is_absolute():
            source = home / source
        # Resolve only ordinary files under the selected Codex home. A separate
        # location must be copied there explicitly before importing credentials.
        try:
            source.relative_to(home)
            checked = [source, *[p for p in source.parents if p == home or home in p.parents]]
            if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in checked):
                raise ValueError('linked path')
            source.resolve().relative_to(home.resolve())
            if source.stat().st_size > 4 * 1024 * 1024:
                raise ValueError('oversized catalog')
            body = source.read_bytes()
            if not isinstance(json.loads(body), dict):
                raise ValueError('catalog must be an object')
        except (OSError, ValueError) as exc:
            raise InstallationError('Codex model catalog is missing or unsupported; place a regular JSON catalog inside the selected Codex folder') from exc
        relative = '.codex/agentbc-model-catalog-' + hashlib.sha256(body).hexdigest()[:16] + '.json'
        if not any(name == relative for name, _ in files):
            files.append((relative, body))
        section['model_catalog_json'] = '/profiles/codex/' + relative


def portable_files(executor: str) -> tuple[list[tuple[str, bytes]], list[str]]:
    from .setup import _active_hermes_home
    from .docker_install_setup import PROFILE_FILES
    from .docker_profiles import ENVIRONMENT_KEYS
    home = Path.home()
    roots = {
        '.codex': Path(os.environ.get('CODEX_HOME', home / '.codex')).expanduser(),
        '.claude': Path(os.environ.get('CLAUDE_CONFIG_DIR', home / '.claude')).expanduser(),
        '.hermes': _active_hermes_home(),
        '.config': Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')).expanduser(),
        '.local/share': Path(os.environ.get('XDG_DATA_HOME', home / '.local/share')).expanduser(),
    }
    files, excluded = [], []
    for relative in PROFILE_FILES[executor]:
        source = home / relative
        for prefix, root in roots.items():
            if relative.startswith(prefix + '/'):
                source = root / relative[len(prefix) + 1:]
                break
        if not source.exists():
            continue
        if source.is_symlink() or source.stat().st_size > 4 * 1024 * 1024:
            raise InstallationError(f'Unsupported configuration file: {source}')
        body = source.read_bytes()
        if executor == 'codex' and relative.endswith('config.toml'):
            value = load_config(source)
            _codex_catalogs(value, roots['.codex'], files)
            for key in CODEX_HOST_KEYS:
                if key in value:
                    excluded.append(key)
                    del value[key]
            # Host project trust is not transferable to a different filesystem.
            if 'projects' in value:
                excluded.append('host project trust')
                del value['projects']
            body = toml_dumps(value).encode()
        elif executor == 'claude' and relative.endswith(('settings.json', '.claude.json')):
            value = json.loads(body)
            if not isinstance(value, dict):
                raise InstallationError('Claude settings must be a JSON object')
            for key in CLAUDE_HOST_KEYS:
                if key in value:
                    excluded.append(key)
                    del value[key]
            if 'projects' in value:
                excluded.append('host project settings')
                del value['projects']
            body = json.dumps(value).encode()
        files.append((relative, body))
    environment = {key: os.environ[key] for key in ENVIRONMENT_KEYS[executor] if key in os.environ}
    if environment:
        files.append(('agentbc-env.json', json.dumps(environment).encode()))
    return files, sorted(set(excluded))


def configure(profile: dict, config: dict, *, interactive: bool, available: list[str] | None = None) -> tuple[dict, dict]:
    """Reuse Native selection functions; prepare all imports before writing."""
    from . import setup as native
    agents = native.scan_all_agents()
    from .docker_setup_guidance import print_scan
    print_scan(agents, profile, available)
    profile['setup_connection_actions'] = {}
    actions = profile['setup_connection_actions']
    config.setdefault('permissions', {})['mode'] = native._select_permission_mode(
        config, explicit_mode=None, interactive=interactive)
    imports = {}
    if interactive:
        print('Existing Docker connections are kept unless you choose to replace their settings.')
        print('Original files stay unchanged. Host-only integrations are listed before confirmation.')
    for agent in agents:
        name = agent['name']
        if name not in ('codex', 'claude', 'hermes', 'opencode'):
            continue
        if available is not None and name not in available:
            print(f"{agent['display']}: runtime is not included in this preview image; connection skipped.")
            continue
        if not interactive:
            continue  # Non-interactive setup never silently imports credentials.
        try:
            files, excluded = portable_files(name)
        except (OSError, ValueError):
            print(f"{agent['display']}: configuration could not be read; existing connection kept.")
            continue
        existing = name in profile.get('executor_imports', {})
        if not agent['found'] and not files and not existing:
            continue
        prompt = (f"Review {agent['display']} connection settings? [Y/n] " if existing else
                  f"Link detected {agent['display']} settings to Docker? [Y/n] ")
        if not native._confirm(prompt, default=True, eof_default=False):
            actions[name] = 'Existing Docker settings kept.' if existing else 'Skipped; no new settings linked.'
            continue
        if not files and not existing:
            print(f"{agent['display']}: no login/API settings found. Configure the original CLI, then run agentbc setup.")
            continue
        if files and existing:
            if not native._confirm('Replace existing Docker settings with detected host settings? [y/N] ', default=False, eof_default=False):
                files = []
        if files:
            if excluded:
                print(f"{agent['display']}: the imported copy excludes host-only integrations: {', '.join(excluded)}.")
            imports[name] = files
            actions[name] = 'Settings imported; connection not tested.'
        elif existing:
            actions[name] = 'Existing Docker settings kept.'
        current = config.setdefault('executors', {}).setdefault(name, {})
        current['command'] = '/opt/agentbc/bin/' + name
        if name == 'claude':
            current['max_budget_usd'] = native._select_claude_budget(config, interactive=True)[0]
        elif name == 'hermes':
            current['max_turns'] = native._select_hermes_max_turns(
                config, command=agent.get('path') or 'hermes', interactive=True)[0]
    config.setdefault('sessions', {})['retain_executor_sessions'] = native._select_session_retention(
        config, interactive=interactive)[0]
    return config, imports
