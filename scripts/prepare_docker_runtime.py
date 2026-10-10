#!/usr/bin/env python3
"""Prepare pinned architecture-specific runtime inputs, never copy a host virtual environment."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def download(url, target):
    with urllib.request.urlopen(url, timeout=120) as source, target.open('wb') as output:
        shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--download-cache', type=Path)
    parser.add_argument('--arch', choices=('arm64', 'amd64'), default='arm64')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Use a new runtime-input directory')
    args.output.mkdir(parents=True)
    lock = json.loads((ROOT / 'deploy/docker/runtime-lock.json').read_text())
    downloads = args.output / 'downloads'
    downloads.mkdir()
    for name, destination in (('codex-package', 'codex'), ('claude', 'claude'), ('claude-agent-sdk', 'wheelhouse')):
        asset = lock['assets'][name]['linux/' + args.arch]
        archive = downloads / (name + '.asset')
        cached = list(args.download_cache.rglob(asset['url'].rsplit('/', 1)[-1])) if args.download_cache else []
        verified = next((p for p in cached if p.is_file() and sha(p) == asset['sha256']), None)
        if verified:
            shutil.copyfile(verified, archive)
        else:
            download(asset['url'], archive)
        if sha(archive) != asset['sha256']:
            raise SystemExit('Runtime hash mismatch: ' + name)
        folder = args.output / destination
        folder.mkdir()
        if name == 'claude-agent-sdk':
            shutil.copyfile(archive, folder / asset['url'].rsplit('/', 1)[-1])
        elif name == 'claude':
            shutil.copyfile(archive, folder / 'claude')
            (folder / 'claude').chmod(0o755)
        else:
            with tarfile.open(archive) as bundle:
                bundle.extractall(folder, filter='data')
    source = downloads / 'hermes-source.tar.gz'
    commit = lock['hermes']['commit']
    download(f'https://codeload.github.com/NousResearch/hermes-agent/tar.gz/{commit}', source)
    if sha(source) != lock['hermes']['source_archive_sha256']:
        raise SystemExit('Hermes source archive hash changed')
    if sha(ROOT / 'deploy/docker/locks/hermes-runtime.txt') != lock['hermes']['dependency_lock_sha256']:
        raise SystemExit('Hermes dependency export hash changed')
    with tarfile.open(source) as bundle:
        bundle.extractall(downloads, filter='data')
    extracted = downloads / ('hermes-agent-' + commit)
    if sha(extracted / 'uv.lock') != lock['source']['hermes_uv_lock_sha256']:
        raise SystemExit('Hermes frozen dependency lock changed')
    shutil.move(extracted, args.output / 'hermes-source')
    (args.output / 'source-receipt.json').write_text(json.dumps({'commit': commit, 'archive_sha256': sha(source),
        'runtime_lock_sha256': sha(ROOT / 'deploy/docker/runtime-lock.json')}, indent=2) + '\n')
    print('Prepared verified runtime inputs: ' + str(args.output))


if __name__ == '__main__':
    main()
