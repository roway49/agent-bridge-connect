#!/usr/bin/env python3
"""Offline real-Codex sandbox qualification; no credentials or model calls."""
import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path


PAYLOAD = r'''
import ctypes, errno, json, os, socket, subprocess
from pathlib import Path
project=Path('/project')
for name in ['.git','.codex','.agents','中文 空格']:
    (project/name).mkdir()
Path('/outside/canary').write_text('unchanged')
Path('/outside/secret').write_text('synthetic-secret')
(project/'escape').symlink_to('/outside',target_is_directory=True)
home=Path.home()/'.codex';home.mkdir()
config=''
for mode in ['read-only','workspace-write']:
    config+='[permissions.'+mode+'.filesystem]\n":root"="read"\n"/outside/secret"="deny"\n'
    if mode=='workspace-write':
        config+='"/project"="write"\n'
    for name in ['.git','.codex','.agents']:
        config+='"/project/'+name+'"="read"\n'
(home/'config.toml').write_text(config)
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
port=listener.getsockname()[1]
control=socket.create_connection(('127.0.0.1',port),timeout=1);control.close()
code=r"""
import ctypes, json, socket
from pathlib import Path
result={}
for name,path in [('project','/project/中文 空格/result'),('outside','/outside/canary'),('symlink','/project/escape/canary'),('git','/project/.git/canary'),('codex','/project/.codex/canary'),('agents','/project/.agents/canary')]:
    try: Path(path).write_text('changed');result[name]='allowed'
    except OSError as e: result[name]={'errno':e.errno}
try: Path('/outside/secret').read_text();result['secret_read']='allowed'
except OSError as e: result['secret_read']={'errno':e.errno}
try:
    s=socket.create_connection(('127.0.0.1',PORT),timeout=1);s.close();result['network']='allowed'
except OSError as e: result['network']={'errno':e.errno}
lib=ctypes.CDLL(None,use_errno=True)
# A tool must not be able to create another mount namespace or mount a filesystem.
for name,call in [('unshare_mount',lambda:lib.unshare(0x20000)),('mount',lambda:lib.mount(b'tmpfs',b'/project',b'tmpfs',6,None))]:
    ret=call();result[name]={'return':ret,'errno':ctypes.get_errno()}
result['status']={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('Uid:','CapEff:','NoNewPrivs:','Seccomp:'))}
print(json.dumps(result))
""".replace('PORT',str(port))
result={'control_network_reachable':True,'cases':{}}
for mode in ['read-only','workspace-write']:
    p=subprocess.run(['/opt/cli/bin/codex','sandbox','-C','/project','-P',mode,'--','/usr/local/bin/python','-c',code],capture_output=True,text=True,timeout=30)
    case={'exit':p.returncode,'stderr':p.stderr}
    try: case['checks']=json.loads(p.stdout)
    except ValueError: case['stdout']=p.stdout
    result['cases'][mode]=case
result['outside_unchanged']=Path('/outside/canary').read_text()=='unchanged'
print(json.dumps(result))
'''


def validate(result):
    failures = []
    if not result.get('outside_unchanged') or not result.get('control_network_reachable'):
        failures.append('fixture/control failed')
    for mode in ('read-only', 'workspace-write'):
        case = result.get('cases', {}).get(mode, {})
        checks = case.get('checks', {})
        if case.get('exit') != 0:
            failures.append(f'{mode}: command failed')
            continue
        for name in ('outside', 'symlink', 'git', 'codex', 'agents', 'secret_read', 'network'):
            value = checks.get(name)
            if not isinstance(value, dict) or value.get('errno') not in (1, 2, 13, 30):
                failures.append(f'{mode}: {name} not denied')
        project = checks.get('project')
        if mode == 'workspace-write' and project != 'allowed':
            failures.append('workspace-write: project write failed')
        if mode == 'read-only' and (not isinstance(project, dict) or project.get('errno') != 30):
            failures.append('read-only: project not read-only')
        for name in ('unshare_mount', 'mount'):
            if checks.get(name, {}).get('return') != -1:
                failures.append(f'{mode}: {name} not denied')
        status = checks.get('status', {})
        if (status.get('CapEff') != '0000000000000000' or status.get('NoNewPrivs') != '1'
                or status.get('Seccomp') != '2' or status.get('Uid', '').split() != ['1000'] * 4):
            failures.append(f'{mode}: process restrictions changed')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-package', type=Path, required=True, help='Unpacked official Linux package, mounted read-only')
    parser.add_argument('--platform', choices=['linux/arm64', 'linux/amd64'], default='linux/arm64')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seccomp-profile', type=Path, help='Override for baseline negative-control testing')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / 'deploy/docker/runtime-lock.json').read_text())
    profile = (args.seccomp_profile or root / 'deploy/docker/seccomp/codex-bwrap.json').resolve()
    package = args.codex_package.resolve()
    if not (package / 'bin/codex').is_file():
        parser.error('package must contain bin/codex')
    name = 'agentbc-sandbox-probe-' + uuid.uuid4().hex[:10]
    command = ['docker', 'run', '--rm', '--init', '--name', name, '--platform', args.platform,
               '--network', 'none', '--user', '1000:1000', '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '--security-opt', 'seccomp=' + str(profile)]
    for folder in ('/home/probe', '/project', '/outside'):
        command += ['--tmpfs', folder + ':uid=1000,gid=1000,mode=700']
    command += ['-e', 'HOME=/home/probe', '-v', str(package) + ':/opt/cli:ro',
                lock['images']['python']['reference'], 'python', '-c', PAYLOAD]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=90)
        try:
            result = json.loads(proc.stdout)
        except ValueError:
            result = {'stdout': proc.stdout}
        failures = validate(result) if proc.returncode == 0 else ['container failed']
        result.update(container_exit=proc.returncode, container_stderr=proc.stderr, failures=failures)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
        result = {'failures': ['probe timeout']}
    result.update(platform=args.platform, image=lock['images']['python']['reference'],
                  profile_sha256=hashlib.sha256(profile.read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'passed': not result['failures'], 'failures': result['failures'], 'evidence': str(args.output)}))
    return bool(result['failures'])


if __name__ == '__main__':
    sys.exit(main())
