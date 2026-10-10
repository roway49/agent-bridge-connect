"""Explicit per-executor API environment import; no shell sourcing or broad env capture."""
from __future__ import annotations
import json
from pathlib import Path

COMMON = {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
ENVIRONMENT_KEYS = {
    'codex': COMMON | {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_ENDPOINT'},
    'claude': COMMON | {'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'ANTHROPIC_MODEL',
                       'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL'},
    'hermes': COMMON | {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY',
                       'GLM_API_KEY', 'GLM_BASE_URL', 'ZAI_API_KEY', 'Z_AI_API_KEY', 'DEEPSEEK_API_KEY'},
    'opencode': COMMON | {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'ANTHROPIC_API_KEY', 'DEEPSEEK_API_KEY',
                         'OPENROUTER_API_KEY', 'GOOGLE_GENERATIVE_AI_API_KEY'},
}


def read_environment(executor: str, path: Path) -> dict[str, str]:
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('API environment file must be a regular JSON file under 1 MiB')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) - ENVIRONMENT_KEYS[executor]
            or not all(isinstance(item, str) and '\x00' not in item for item in value.values())):
        raise ValueError('Unsupported API environment mapping; no variables were imported')
    return value
