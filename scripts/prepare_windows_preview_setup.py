"""Prepare a verified Compose preview for manual setup, preserving its old volume.

Run from the preview host venv after installing the matching wheel. This is an
explicit preview migration, not a bypass of the formal Windows bundle gate.
"""

from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

from agent_bridge_connect.docker_preview_setup import documents_workspace, idle, refresh
from agent_bridge_connect.docker_install_runtime import engine_identity
from agent_bridge_connect.docker_webui import verify as verify_webui
from agent_bridge_connect.installation import private_json
from agent_bridge_connect.host_platform import (
    ensure_private_directory,
    add_user_path,
    try_lock,
)
from agent_bridge_connect.windows_desktop import register, ensure

COPY = r"""
import hashlib,json,shutil,sys
from pathlib import Path,PureWindowsPath
source=Path('/source');target=Path('/target')
assert not any(target.iterdir()), 'Destination must be empty'
files={};names=set()
for p in sorted(source.rglob('*')):
 assert not p.is_symlink(), 'Source contains a symlink'
 rel=p.relative_to(source)
 key=str(rel).casefold()
 assert key not in names, 'Case-insensitive path collision'
 names.add(key)
 assert all(not PureWindowsPath(n).is_reserved() and not any(c in n for c in '<>:"|?*') and n.rstrip(' .')==n for n in rel.parts), 'Unsupported Windows filename'
 if p.is_file(): files[rel.as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
for p in sorted(source.rglob('*')):
 q=target/p.relative_to(source)
 if p.is_dir():q.mkdir(exist_ok=True)
 elif p.is_file():q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
assert {p.relative_to(target).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file()}==files, 'Workspace copy verification failed'
print(json.dumps({'files':len(files),'sha256':hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()}))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--expected-runner", required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("Windows preview only")
    p = args.profile
    with (p.parent / "preview-setup.lock").open("a+b") as lock:
        try_lock(lock.fileno())
        perform(args)


def perform(args):
    p = args.profile
    old = json.loads(p.read_text())
    if old.get("setup_binding"):
        raise ValueError("Setup already prepared; use agentbc setup")
    assert old["container_id"] == args.expected_runner and old["schema"] == 1
    verify_webui(old)
    old_spec = json.loads(args.compose.read_text())
    assert set(old_spec["services"]) == {"runner", "webui"}

    def run(*a):
        r = subprocess.run(["docker", *a], capture_output=True, text=True, timeout=180)
        if r.returncode:
            raise RuntimeError("Docker " + a[0] + " failed: " + r.stderr[:700])
        return r.stdout

    assert set(
        run(
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=com.docker.compose.project=" + old_spec["name"],
        ).split()
    ) == {old["container_id"], old["webui"]["container_id"]}
    info = json.loads(run("inspect", old["container_id"]))[0]
    assert info["Config"]["Labels"]["com.docker.compose.project"] == old_spec["name"]
    for key, cid in [
        ("runner", old["container_id"]),
        ("webui", old["webui"]["container_id"]),
    ]:
        observed = json.loads(run("inspect", cid))[0]
        assert (
            observed["Name"].lstrip("/") == old_spec["services"][key]["container_name"]
            and observed["Image"] == old_spec["services"][key]["image"]
        )
    data = next(m for m in info["Mounts"] if m["Destination"] == "/data/workspace")
    assert (
        data["Type"] == "volume" and data["Name"] == old_spec["volumes"]["data"]["name"]
    )
    target = documents_workspace()
    if target.exists() and any(target.iterdir()):
        raise ValueError("Workspace already contains files; nothing changed")
    link = Path.home() / ".local/bin/agentbc.cmd"
    entry = p.parent / "bin/agentbc.cmd"
    if link.exists() and link.read_bytes() != entry.read_bytes():
        raise ValueError(
            "Another agentbc entry exists; resolve installation conflict first"
        )
    idle(old["container_id"])
    backup = p.parent / "before-workspace-setup"
    ensure_private_directory(backup)
    private_json(backup / "profile.json", old)
    private_json(backup / "compose.json", old_spec)
    register(p, enable=False)
    run("compose", "-f", str(args.compose), "stop")
    target.mkdir(parents=True, exist_ok=True)
    try:
        receipt = json.loads(
            run(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--mount",
                "type=volume,source=" + data["Name"] + ",target=/source,readonly",
                "--mount",
                "type=bind,source=" + str(target) + ",target=/target",
                "--entrypoint",
                "python",
                info["Image"],
                "-c",
                COPY,
            )
        )
        private_json(backup / "workspace-copy.json", receipt)
        spec = copy.deepcopy(old_spec)
        for name, service in spec["services"].items():
            service["volumes"] = [
                v for v in service["volumes"] if not v.startswith("data:")
            ]
            service["volumes"].append(
                {
                    "type": "bind",
                    "source": str(target),
                    "target": "/data/workspace",
                    "read_only": name == "webui",
                    "bind": {"create_host_path": False},
                }
            )
        health = "from pathlib import Path;from urllib.request import Request,urlopen;from urllib.parse import urlsplit;u=urlsplit(Path('/run/webui/launch-url').read_text().strip());urlopen(Request('http://127.0.0.1:8080/',headers={'Host':u.netloc}),timeout=2).read(1)"
        spec["services"]["webui"]["healthcheck"] = {
            "test": ["CMD", "python", "-c", health],
            "interval": "5s",
            "timeout": "3s",
            "retries": 3,
        }
        # Keep the original data volume declared/external, never remove it.
        managed = p.parent / "preview-compose.json"
        private_json(managed, spec)
        run("compose", "-f", str(managed), "up", "-d", "--no-build", "--pull", "never")
        new = copy.deepcopy(old)
        new.update(
            host_workspace=str(target),
            settings={
                "preferred_view": "webui",
                "port": old["webui"]["port"],
                "autostart": False,
            },
            executor_imports={
                "codex": "Existing Windows settings kept; connection not tested by setup",
                "hermes": "Existing Windows DeepSeek settings kept; last task 38K8-001 completed",
            },
        )
        new["setup_binding"] = {
            "compose": str(managed),
            "engine": engine_identity(),
            "executors": ["codex", "hermes"],
            "profile_roots": {"codex": "/home/agentbc", "hermes": "/profiles/hermes"},
        }
        refresh(new, p, managed, spec)
        (p.parent / "preview-setup.enabled").touch()
        docker_directory = str(Path(shutil.which("docker")).parent)
        if any(c in docker_directory for c in '%!"\r\n'):
            raise ValueError("Unsupported Docker executable directory")
        body = entry.read_text()
        body = body.replace('@echo off\n', '@echo off\nset "PATH=' + docker_directory + ';%PATH%"\n', 1)
        entry.write_text(body)
        link.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, link)
        add_user_path(link.parent)
        ensure(p)
        print(
            json.dumps(
                {
                    "prepared": True,
                    "workspace": str(target),
                    "copy": receipt,
                    "entry": str(link),
                    "next": "agentbc setup",
                }
            )
        )
    except BaseException:
        # Preserve partial target for inspection. Restore original data source.
        run(
            "compose",
            "-f",
            str(args.compose),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
        )
        ri = json.loads(
            run("inspect", old_spec["services"]["runner"]["container_name"])
        )[0]
        wi = json.loads(
            run("inspect", old_spec["services"]["webui"]["container_name"])
        )[0]
        from agent_bridge_connect.docker_webui import binding

        old.update(
            container_id=ri["Id"], container_name=ri["Name"], webui=binding(ri, wi)
        )
        private_json(p, old)
        ensure(p)
        raise


if __name__ == "__main__":
    main()
