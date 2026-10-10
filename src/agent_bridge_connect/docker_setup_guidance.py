"""Human-readable Docker setup outcome; never imply a model call succeeded."""
from __future__ import annotations


def connection_rows(profile: dict, available: list[str] | None = None) -> list[tuple[str, str, str]]:
    if available is None:
        available = profile.get('available_executors') or profile.get('setup_binding', {}).get('executors')
    linked = profile.get('executor_imports', {})
    return [(name, 'Included' if available is not None and name in available else
             ('Not included' if available is not None else 'Not checked'),
             'Saved settings' if name in linked else 'Not linked')
            for name in ('codex', 'claude', 'hermes', 'opencode')]


def print_scan(agents: list[dict], profile: dict, available: list[str] | None) -> None:
    detected = {agent['name']: agent for agent in agents}
    print('\nAI tools and Docker connections')
    print(f'{"AI tool":10} {"Windows/local CLI":20} {"Docker runtime":16} Connection')
    for name, runtime, connection in connection_rows(profile, available):
        local = 'Detected' if detected.get(name, {}).get('found') else 'Not detected'
        print(f'{name.title():10} {local:20} {runtime:16} {connection}')
    print('Local CLI detection describes this computer, not the Docker runtime.')
    print('Docker can use saved settings even when the local CLI is not detected.')
    print('Saved settings do not prove that a model connection works.')


def print_summary(profile: dict) -> None:
    print('\nDocker connection summary')
    for name, runtime, connection in connection_rows(profile):
        action = profile.get('setup_connection_actions', {}).get(name)
        if runtime == 'Not included':
            message = 'Runtime not included in this image; unavailable as a Docker executor.'
        elif action:
            message = action
        elif connection == 'Saved settings':
            message = 'Existing Docker settings kept.'
        else:
            message = 'No settings linked by this setup.'
        print(f'  {name.title():10} {message}')
    print('Connection tests: not run by setup. Verify configured executors with a small task.')
    print('Skills only connect an AI assistant to AgentBC commands; they do not install AI runtimes.')
    print('Run agentbc setup again to change settings.')


def print_intro(profile: dict) -> None:
    print("\nAgentBC Docker setup")
    print("1. Detect local AI tools and choose the connections to link.")
    print("2. Choose task permissions, session retention and monitoring.")
    print("3. Choose host project folders that tasks may access.")
    print("4. Install AgentBC skills and check background services.")
    print("Press Enter to keep the displayed default. Existing tasks are preserved.")
    print("Default task workspace (inside Docker): /data/workspace")
    print("Reports: /data/workspace/tasks/report; task files: /data/workspace/tasks/artifacts")
    print("These Docker-volume paths are not Windows Explorer paths.")
    print("Use task report or WebUI to read reports. Host projects must be explicitly mounted.")
    for index, path in enumerate(profile.get('projects', [])):
        print(f"Host project: {path} -> /projects/{index}")
