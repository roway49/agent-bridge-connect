#!/usr/bin/env python3
"""Build a checksum-bound offline Mac Docker bundle from an explicit image and wheel."""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--platform', choices=('macos-arm64', 'windows-amd64'), default='macos-arm64')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.whl', args.wheel.name):
        raise SystemExit('Invalid wheel filename')
    image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', args.image]))[0]
    arch = {'macos-arm64': 'arm64', 'windows-amd64': 'amd64'}[args.platform]
    if image['Architecture'] != arch or image['Os'] != 'linux':
        raise SystemExit('Image architecture does not match bundle platform')
    with zipfile.ZipFile(args.wheel) as wheel:
        metadata = wheel.read(next(n for n in wheel.namelist() if n.endswith('.dist-info/METADATA'))).decode()
        build_info = json.loads(wheel.read('agent_bridge_connect/_build_info.json'))
        version = next(line.split(': ', 1)[1] for line in metadata.splitlines() if line.startswith('Version: '))
        required = {'agent_bridge_connect/docker_install.py', 'agent_bridge_connect/installation.py',
                    'agent_bridge_connect/assets/webui/index.html'}
        if not required <= set(wheel.namelist()):
            raise SystemExit('Wheel is missing managed installer or frontend resources')
    # Bind container code and host wheel to exactly the same source artifact.
    # The supplied base is an immutable runtime image, never the installation's
    # credential-bearing container or a docker commit of a running service.
    base_tag = 'agentbc-install-runtime:' + image['Id'].split(':', 1)[1]
    subprocess.run(['docker', 'tag', image['Id'], base_tag], check=True)
    with tempfile.TemporaryDirectory(prefix='agentbc-image-bind-') as context:
        stage = Path(context)
        shutil.copy2(args.wheel, stage / args.wheel.name)
        shutil.copy2(ROOT / 'deploy/docker/runtime_launcher.py', stage / 'runtime_launcher.py')
        (stage / 'Dockerfile').write_text(
            f'FROM {base_tag}\nUSER root\nCOPY {args.wheel.name} /tmp/agentbc.whl\n'
            'COPY runtime_launcher.py /opt/agentbc/runtime_launcher.py\n'
            'RUN mkdir -p /profiles/codex /profiles/claude /profiles/hermes /profiles/opencode && chown -R 1000:1000 /profiles && chmod 755 /opt/agentbc/runtime_launcher.py && for name in codex claude hermes opencode; do cp /opt/agentbc/runtime_launcher.py /opt/agentbc/bin/$name; done\n'
            f'RUN cp /tmp/agentbc.whl /tmp/{args.wheel.name} '
            f'&& /probe/sdk-{arch}/bin/python -m pip install --no-index --no-deps --force-reinstall /tmp/{args.wheel.name} '
            f'&& /usr/local/bin/python -m pip install --no-index --no-deps --force-reinstall /tmp/{args.wheel.name} '
            f'&& rm /tmp/agentbc.whl /tmp/{args.wheel.name}\n'
            f"RUN printf '#!/bin/sh\\nexec /probe/sdk-{arch}/bin/python -m agent_bridge_connect.cli \"$@\"\\n' > /usr/local/bin/agentbc && chmod 755 /usr/local/bin/agentbc\n"
            f'LABEL io.agentbc.wheel-sha256="{digest(args.wheel)}"\nUSER 1000:1000\n')
        iid = stage / 'image-id'
        subprocess.run(['docker', 'build', '--network=none', '--platform=linux/' + arch, '--iidfile', str(iid), str(stage)], check=True)
        image = json.loads(subprocess.check_output(['docker', 'image', 'inspect', iid.read_text().strip()]))[0]
    available = ['codex', 'claude', 'hermes'] + ([] if args.platform == 'windows-amd64' else ['opencode'])
    runtime_versions = {}
    for executor in available:
        runtime_versions[executor] = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/opt/agentbc/bin/' + executor, image['Id'], '--version'], text=True, timeout=60).strip()[:256]
    name = f'agentbc-{version}-docker-{args.platform}'
    args.output.mkdir(parents=True, exist_ok=True)
    final = args.output / name
    if final.exists():
        raise SystemExit('Output candidate already exists; use a new output directory')
    with tempfile.TemporaryDirectory(prefix='.docker-bundle-', dir=args.output) as tmp:
        bundle = Path(tmp) / name
        bundle.mkdir()
        shutil.copy2(args.wheel, bundle / args.wheel.name)
        for script in (('install_docker.ps1', 'recover_docker.ps1') if args.platform == 'windows-amd64' else ('install_docker.sh', 'recover_docker.sh')):
            shutil.copy2(ROOT / 'scripts' / script, bundle / script)
        shutil.copy2(ROOT / 'deploy/docker/seccomp/codex-bwrap.json', bundle / 'seccomp.json')
        shutil.copy2(ROOT / 'deploy/docker/runtime-lock.json', bundle / 'runtime-lock.json')
        shutil.copy2(ROOT / 'deploy/docker/runtime_launcher.py', bundle / 'runtime_launcher.py')
        if args.platform == 'windows-amd64':
            shutil.copy2(ROOT / 'deploy/docker/WINDOWS-INSTALLATION.md', bundle / 'WINDOWS-INSTALLATION.md')
            for script in ('install_docker.py', 'recover_docker.py'):
                shutil.copy2(ROOT / 'scripts' / script, bundle / script)
        subprocess.run(['docker', 'save', '--output', str(bundle / 'image.tar'), image['Id']], check=True)
        manifest = {'schema': 1, 'type': 'docker', 'platform': args.platform, 'data_schema': 1,
                    'managed_runner_entry': True, 'windows_install_contract': 1 if args.platform == 'windows-amd64' else None,
                    'available_executors': available, 'runtime_versions': runtime_versions,
                    'version': version, 'image_id': image['Id'], 'wheel': args.wheel.name, 'source_build': build_info,
                    'source_commit': build_info['commit_sha'],
                    'files': {p.name: digest(p) for p in sorted(bundle.iterdir())}}
        (bundle / 'bundle.json').write_text(json.dumps(manifest, indent=2) + '\n')
        archive = Path(tmp) / (name + ('.zip' if args.platform == 'windows-amd64' else '.tar.gz'))
        if args.platform == 'windows-amd64':
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for item in bundle.rglob('*'):
                    if item.is_file():
                        output.write(item, name + '/' + item.relative_to(bundle).as_posix())
        else:
            with tarfile.open(archive, 'w:gz') as output:
                output.add(bundle, arcname=name)
        checksum = digest(archive)
        shutil.move(str(bundle), final)
        shutil.move(str(archive), args.output / archive.name)
        (args.output / (archive.name + '.sha256')).write_text(checksum + '  ' + archive.name + '\n')
    print(json.dumps({'bundle': str(final), 'archive_sha256': checksum, 'image_id': image['Id']}, indent=2))


if __name__ == '__main__':
    main()
