"""User-owned WebUI lifecycle; never signals a PID or controls a Runner."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .config import load_config, resolve_config_path, webui_settings as settings
from .protocol import ABCError


def allocate_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def select_settings(config, *, interactive):
    current = settings(config)
    if not interactive:
        return current
    from .setup import _prompt, _confirm

    default = "2" if current["preferred_view"] == "webui" else "1"
    while True:
        value = _prompt(
            f"Task list style: 1) Terminal  2) WebUI [default: {default}]: ", default=default
        )
        if value in ("1", "2"):
            break
    current["preferred_view"] = "webui" if value == "2" else "task_list"
    if value == "1":
        current["autostart"] = False
        return current
    if current.get("port") is None or _confirm(
        f"Fixed WebUI port: {current['port']}. Assign a new port? [y/N] ",
        default=False,
        eof_default=False,
    ):
        current["port"] = allocate_port()
    current["autostart"] = _confirm(
        "Keep WebUI running in the background? [Y/n] ",
        default=True,
        eof_default=False,
    )
    return current


class Service:
    def __init__(self, config=None, *, assets=None):
        self.assets = assets
        self.config = resolve_config_path(config).expanduser().resolve()
        self.key = hashlib.sha256(str(self.config).encode()).hexdigest()[:16]
        # Short Unix socket path, isolated by uid and canonical configuration.
        self.root = Path(f"/tmp/agentbc-webui-{os.getuid()}-{self.key}")
        self.record = self.root / "instance.json"
        self.address = str(self.root / "control.sock")
        self.label = f"com.agentbc.webui.{self.key}"
        self.plist = Path.home() / "Library/LaunchAgents" / f"{self.label}.plist"

    def prepare(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        st = self.root.lstat()
        if (
            not stat.S_ISDIR(st.st_mode)
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
        ):
            raise ABCError(
                "webui_state_unsafe", "WebUI state directory must be private and owned"
            )

    @contextlib.contextmanager
    def lock(self, name, *, blocking=True):
        self.prepare()
        fd = os.open(self.root / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_uid != os.getuid()
                or st.st_mode & 0o077
            ):
                raise ABCError("webui_state_unsafe", "Unsafe service lock")
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            yield
        finally:
            os.close(fd)

    def read(self):
        try:
            fd = os.open(self.record, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as handle:
                st = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.getuid()
                    or st.st_mode & 0o077
                ):
                    raise ABCError("webui_state_unsafe", "Unsafe service receipt")
                data = json.loads(handle.read(8192))
            if not isinstance(data, dict) or data.get("config") != str(self.config):
                raise ABCError(
                    "webui_identity_mismatch", "Receipt belongs to another config"
                )
            return data
        except (FileNotFoundError, ValueError):
            return None

    def request(self, receipt, action):
        if not receipt:
            return None
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(1)
                sock.connect(self.address)
                sock.sendall(
                    json.dumps(
                        {
                            "secret": receipt["secret"],
                            "instance": receipt["instance"],
                            "action": action,
                        }
                    ).encode()
                    + b"\n"
                )
                data = json.loads(sock.recv(8192))
            if (
                isinstance(data, dict)
                and data.get("instance") == receipt["instance"]
                and data.get("config") == str(self.config)
            ):
                return data
        except (OSError, ValueError, KeyError):
            pass
        return None

    def status(self):
        cfg = settings(load_config(self.config))
        if self.root.exists():
            self.prepare()
        receipt = self.read()
        live = self.request(receipt, "status")
        error = ""
        error_path = self.root / "error.json"
        if not live and error_path.exists():
            fd = os.open(error_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd) as handle:
                st = os.fstat(handle.fileno())
                if (
                    stat.S_ISREG(st.st_mode)
                    and st.st_uid == os.getuid()
                    and not st.st_mode & 0o077
                ):
                    try:
                        value = json.loads(handle.read(1024)).get("code", "")
                        if (
                            isinstance(value, str)
                            and value.startswith("webui_")
                            and value.replace("_", "").isalnum()
                            and len(value) < 80
                        ):
                            error = value
                    except (ValueError, AttributeError):
                        pass
        return {
            "error": error,
            "running": bool(live),
            "port": live.get("port") if live else cfg.get("port"),
            "configured_port": cfg.get("port"),
            "autostart": cfg["autostart"],
            "preferred_view": cfg["preferred_view"],
            "login_registered": self.plist.is_file(),
            "state": "running" if live else "stale" if receipt else "stopped",
        }

    def command(self):
        return [
            sys.executable,
            "-m",
            "agent_bridge_connect.web_service",
            "--config",
            str(self.config),
        ] + (["--assets", str(self.assets)] if self.assets else [])

    def start(self, *, open_browser=True):
        cfg = settings(load_config(self.config))
        if cfg.get("port") is None:
            raise ABCError(
                "webui_setup_required",
                "Run agentbc setup to configure a fixed WebUI port",
            )
        with self.lock("manager.lock"):
            receipt = self.read()
            live = self.request(receipt, "status")
            if live and live["port"] != cfg["port"]:
                raise ABCError(
                    "webui_config_changed", "Close WebUI before applying the new port"
                )
            if not live:
                error = self.root / "error.json"
                error.unlink(missing_ok=True)
                env = dict(os.environ)
                # A source checkout is resolved explicitly, independent of caller cwd.
                env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
                process = subprocess.Popen(
                    self.command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    cwd="/",
                    env=env,
                )
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    receipt = self.read()
                    live = self.request(receipt, "status")
                    if live:
                        break
                    if process.poll() is not None:
                        code = (
                            json.loads(error.read_text()).get("code")
                            if error.is_file()
                            else "webui_start_failed"
                        )
                        raise ABCError(
                            code,
                            "WebUI did not start; check configuration, packaged assets and fixed-port availability",
                        )
                    time.sleep(0.05)
                if not live:
                    process.terminate()
                    process.wait(timeout=3)
                    raise ABCError(
                        "webui_start_timeout",
                        "WebUI readiness timed out; inspect webui status",
                    )
                threading.Thread(target=process.wait, daemon=True).start()
            url = f"http://127.0.0.1:{live['port']}/#token={receipt['token']}"
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return url

    def close(self):
        with self.lock("manager.lock"):
            receipt = self.read()
            if self.request(receipt, "status"):
                if not self.request(receipt, "close"):
                    raise ABCError(
                        "webui_close_failed", "WebUI did not acknowledge shutdown"
                    )
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    try:
                        with self.lock("server.lock", blocking=False):
                            return
                    except BlockingIOError:
                        time.sleep(0.05)
                raise ABCError(
                    "webui_close_timeout", "WebUI shutdown is not yet verified"
                )
            # Never kill a receipt PID: lock proves no owned worker remains.
            try:
                with self.lock("server.lock", blocking=False):
                    self.record.unlink(missing_ok=True)
            except BlockingIOError as exc:
                raise ABCError(
                    "webui_control_unavailable",
                    "Owned service is busy but control is unavailable",
                ) from exc

    def login(self, enabled, *, activate=True):
        if sys.platform != "darwin":
            if enabled:
                raise ABCError(
                    "webui_autostart_unsupported",
                    "Native login startup is macOS-only; use Docker on other hosts",
                )
            return
        domain = f"gui/{os.getuid()}"
        if self.plist.exists() or self.plist.is_symlink():
            if (
                self.plist.is_symlink()
                or plistlib.loads(self.plist.read_bytes()).get("Label") != self.label
            ):
                raise ABCError(
                    "webui_login_unsafe", "Refusing to change an unowned login item"
                )
            probe = subprocess.run(
                ["launchctl", "print", f"{domain}/{self.label}"], capture_output=True
            )
            if probe.returncode == 0:
                subprocess.run(
                    ["launchctl", "bootout", f"{domain}/{self.label}"],
                    check=True,
                    capture_output=True,
                )
            self.plist.unlink()
        if enabled:
            self.plist.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": self.label,
                "ProgramArguments": self.command() + ["--launch"],
                "RunAtLoad": True,
                "KeepAlive": False,
                "EnvironmentVariables": {
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1])
                },
            }
            fd = os.open(
                self.plist, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as handle:
                plistlib.dump(payload, handle)
            if not activate:
                return
            try:
                subprocess.run(
                    ["launchctl", "bootstrap", domain, str(self.plist)],
                    check=True,
                    capture_output=True,
                )
            except Exception:
                self.plist.unlink(missing_ok=True)
                raise

    def configure(self):
        cfg = settings(load_config(self.config))
        enabled = cfg["preferred_view"] == "webui" and cfg["autostart"]
        self.login(enabled)
        if enabled:
            self.start(open_browser=False)
        else:
            self.close()
        return self.status()

    def suspend_update(self):
        state = self.status()
        if state["running"] or state["login_registered"]:
            self.login(False)
            self.close()
        return {
            "running": state["running"],
            "login_registered": state["login_registered"],
            "config": str(self.config),
        }

    def restore_update(self, state):
        if state.get("login_registered"):
            self.login(True, activate=False)
        if state.get("running"):
            self.start(open_browser=False)

    def worker(self):
        from .web_gateway import create_server

        self.prepare()
        with self.lock("server.lock", blocking=False):
            self.record.unlink(missing_ok=True)
            Path(self.address).unlink(missing_ok=True)
            from types import SimpleNamespace

            cfg = settings(load_config(self.config))
            args = SimpleNamespace(
                config=self.config,
                root=None,
                report_root=None,
                assets=self.assets,
                port=cfg.get("port"),
            )
            if args.port is None:
                raise ABCError("webui_setup_required", "Missing fixed port")
            with (
                create_server(args) as server,
                socket.socket(socket.AF_UNIX) as control,
            ):
                control.bind(self.address)
                os.chmod(self.address, 0o600)
                control.listen(4)
                control.settimeout(0.2)
                receipt = {
                    "config": str(self.config),
                    "instance": secrets.token_hex(24),
                    "secret": secrets.token_hex(32),
                    "token": server.token,
                    "pid": os.getpid(),
                    "port": server.server_port,
                }
                stopped = threading.Event()

                def handle_control():
                    while not stopped.is_set():
                        try:
                            connection, _ = control.accept()
                        except socket.timeout:
                            continue
                        with connection:
                            connection.settimeout(1)
                            try:
                                request = json.loads(connection.recv(4096))
                                if not isinstance(request, dict):
                                    continue
                                if (
                                    not secrets.compare_digest(
                                        str(request.get("secret", "")),
                                        receipt["secret"],
                                    )
                                    or request.get("instance") != receipt["instance"]
                                ):
                                    continue
                                action = request.get("action")
                                if action not in ("status", "close"):
                                    continue
                                connection.sendall(
                                    json.dumps(
                                        {
                                            k: receipt[k]
                                            for k in ("config", "instance", "port")
                                        }
                                    ).encode()
                                )
                                if action == "close":
                                    server.shutdown()
                                    return
                            except (OSError, ValueError):
                                continue

                thread = threading.Thread(target=handle_control, daemon=True)
                thread.start()
                fd = os.open(
                    self.record,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "w") as handle:
                    json.dump(receipt, handle)
                try:
                    server.serve_forever(poll_interval=0.1)
                finally:
                    stopped.set()
                    thread.join(timeout=2)
                    self.record.unlink(missing_ok=True)
                    Path(self.address).unlink(missing_ok=True)


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config")
    parser.add_argument("--suspend-update", action="store_true")
    parser.add_argument("--resume-update", action="store_true")
    parser.add_argument("--restore-state")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--launch", action="store_true")
    parser.add_argument("--assets", type=Path)
    args = parser.parse_args()
    service = Service(args.config, assets=args.assets)
    try:
        if args.uninstall:
            service.login(False)
            service.close()
        elif args.suspend_update:
            state = service.suspend_update()
            service.prepare()
            fd = os.open(
                service.root / "upgrade.json",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
        elif args.resume_update:
            pending = service.root / "upgrade.json"
            if pending.is_file():
                service.restore_update(json.loads(pending.read_text()))
                pending.unlink()
        elif args.restore_state:
            service.restore_update(json.loads(args.restore_state))
        elif args.launch:
            service.start(open_browser=False)
        else:
            service.worker()
    except Exception as exc:
        service.prepare()
        error = service.root / "error.json"
        fd = os.open(
            error, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(fd, "w") as handle:
            json.dump(
                {
                    "code": getattr(
                        exc,
                        "code",
                        "webui_port_in_use"
                        if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE
                        else "webui_start_failed",
                    )
                },
                handle,
            )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
