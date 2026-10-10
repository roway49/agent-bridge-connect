#!/usr/bin/env python3
"""Offline installer entry for hosts that keep PowerShell script policy unchanged."""
import sys
from pathlib import Path


def main():
    if sys.version_info < (3, 10):
        raise SystemExit('AgentBC requires Python 3.10 or newer. Run this file with a supported interpreter.')
    root = Path(__file__).resolve().parent
    wheels = list(root.glob('agentbc-*.whl'))
    if len(wheels) != 1:
        raise SystemExit('Expected exactly one AgentBC wheel in this bundle.')
    sys.path.insert(0, str(wheels[0]))
    sys.argv.insert(1, str(root))
    from agent_bridge_connect.docker_install_bundle import main as install
    return install()


if __name__ == '__main__':
    raise SystemExit(main())
