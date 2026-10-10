"""Cleanup proof for isolated Codex container sessions; never a Desktop receipt."""
from __future__ import annotations
import os
import sys

STRATEGY = 'container_session_archive_then_delete'


def enabled() -> bool:
    # Set only on the managed Runner, not derived from the task or its prompt.
    return sys.platform == 'linux' and os.environ.get('AGENTBC_SESSION_STORE') == 'isolated_container'


def proof_valid(verification, commands) -> bool:
    return (all(commands.get(name, {}).get('status') in {'acknowledged', 'confirmed'}
                and commands.get(name, {}).get('checked_at') for name in ('app_server_archive', 'delete'))
            and verification.get('cli', {}).get('status') == 'absent'
            and bool(verification.get('cli', {}).get('checked_at'))
            and commands.get('desktop_archive', {}).get('status') == 'not_applicable')


def cleanup(port, request):
    from .adapters import SessionCleanupResult
    from .codex_session_cleanup import CodexSessionCleanupClient, CodexSessionCleanupError
    from .executors.codex import _cleanup_workspace_root, _cleanup_now, _v5_command
    now = _cleanup_now()
    verification = {side: {'status': 'not_applicable', 'checked_at': now}
                    for side in ('desktop_backend', 'desktop_live')}
    verification['cli'] = {'status': 'unknown', 'checked_at': now}
    commands = {'desktop_archive': _v5_command(status='not_applicable', checked_at=now),
                'app_server_archive': _v5_command(status='not_requested', checked_at=now),
                'delete': _v5_command(status='not_requested', checked_at=now)}
    if not enabled() or str(port.agent_bin) != '/opt/agentbc/bin/codex':
        return SessionCleanupResult('failed', 'supported', STRATEGY, 'container_session_scope_invalid', False)
    try:
        client = CodexSessionCleanupClient(port.agent_bin, cwd=_cleanup_workspace_root(request),
            transport_factory=port.transport_factory,
            transport=port.transport_mode if not isinstance(port.transport_mode, str) else None, timeout_s=15)
        # A bound native Executor receipt may already acknowledge archive.
        # Reuse only that official evidence, never a Desktop acknowledgement.
        observation = client.delete_and_verify(request.session_id,
            archive_acknowledged=request.archive_acknowledged,
            archive_checked_at=request.archive_checked_at)
        verification.update(observation.verification())
        evidence = observation.commands()
        commands.update(app_server_archive=_v5_command(evidence.get('archive')),
                        delete=_v5_command(evidence.get('delete')))
        success = proof_valid(verification, commands)
        return SessionCleanupResult('succeeded' if success else 'failed', 'supported', STRATEGY,
            '' if success else (observation.error_code or 'container_session_absence_unverified'),
            False if success else observation.retryable, verification=verification, commands=commands)
    except CodexSessionCleanupError as exc:
        commands.update(app_server_archive=_v5_command((exc.commands or {}).get('archive')),
                        delete=_v5_command((exc.commands or {}).get('delete')))
        return SessionCleanupResult('failed', 'supported', STRATEGY, exc.code, exc.retryable,
                                    verification=verification, commands=commands)
