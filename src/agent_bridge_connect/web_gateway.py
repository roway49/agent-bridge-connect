"""Local, authenticated read-only WebUI host. Never owns a Runner lifecycle."""

from __future__ import annotations

import hmac
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from threading import BoundedSemaphore
from urllib.parse import parse_qs, urlsplit

from .protocol import ABCError
from .web_catalogue import TaskCatalogue
from .web_snapshot import SnapshotProvider, _json

_ERROR_STATUS = {
    "snapshot_task_id_invalid": 400,
    "invalid_limit": 400,
    "invalid_group": 400,
    "invalid_cursor": 400,
    "cursor_expired": 409,
    "snapshot_task_not_found": 404,
    "snapshot_record_invalid": 422,
    "snapshot_file_too_large": 413,
    "snapshot_response_too_large": 413,
    "catalogue_too_large": 413,
}
_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def server_bind(self):
        # HTTPServer resolves an FQDN here. Numeric, allowlisted listeners need
        # no DNS; an unavailable resolver must not delay local service startup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def __init__(
        self,
        provider,
        assets: Path,
        *,
        port=0,
        listen_host="127.0.0.1",
        public_origin=None,
    ):
        self.provider = provider
        self.catalogue = TaskCatalogue(provider)
        self.token = secrets.token_urlsafe(32)
        self._slots = BoundedSemaphore(8)
        assets = assets.resolve(strict=True)
        self.assets = {}
        for p in assets.rglob("*"):
            if (
                p.is_file()
                and not p.is_symlink()
                and p.suffix in {".html", ".js", ".css", ".svg", ".ico"}
            ):
                resolved = p.resolve()
                if (
                    resolved.is_relative_to(assets)
                    and p.stat().st_size <= 2 * 1024 * 1024
                ):
                    self.assets["/" + p.relative_to(assets).as_posix()] = p.read_bytes()
        if "/index.html" not in self.assets:
            raise ABCError(
                "webui_assets_missing", "Build webui assets before starting."
            )
        self.assets["/"] = self.assets["/index.html"]
        if listen_host not in ("127.0.0.1", "0.0.0.0"):
            raise ABCError("webui_listen_invalid", "Unsupported listen address")
        if listen_host != "127.0.0.1" and not public_origin:
            raise ABCError(
                "webui_origin_required",
                "Container binding requires an explicit loopback public origin",
            )
        if public_origin:
            origin = urlsplit(public_origin)
            if (
                origin.scheme != "http"
                or origin.hostname != "127.0.0.1"
                or origin.username
                or origin.password
                or origin.path
                or origin.query
                or origin.fragment
                or not origin.port
            ):
                raise ABCError(
                    "webui_origin_invalid", "Use an exact http://127.0.0.1:port origin"
                )
        super().__init__((listen_host, port), WebHandler)
        self.authority = f"127.0.0.1:{self.server_port}"
        if public_origin:
            self.authority = urlsplit(public_origin).netloc
        self.origin = "http://" + self.authority

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            request.settimeout(5)
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # No request/body/header/trace logging; never echo credentials.
        pass


class WebHandler(BaseHTTPRequestHandler):
    server: WebServer

    def log_message(self, format, *args):
        pass

    def _send(self, code, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else _json(payload)
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _failure(self, status, code):
        self._send(
            status, {"error": {"code": code, "retryable": code == "snapshot_changed"}}
        )

    def _origin_ok(self):
        return (
            self.headers.get_all("Host") == [self.server.authority]
            and (
                self.headers.get_all("Origin") is None
                or self.headers.get_all("Origin") == [self.server.origin]
            )
            and self.headers.get("Sec-Fetch-Site", "none") in {"none", "same-origin"}
        )

    def do_GET(self):
        if not self._origin_ok():
            return self._failure(403, "origin_forbidden")
        try:
            target = urlsplit(self.path)
            if target.scheme or target.netloc or target.fragment:
                return self._failure(400, "invalid_request")
            path = target.path
            if path.startswith("/api/"):
                credentials = self.headers.get_all("Authorization")
                if (
                    credentials is None
                    or len(credentials) != 1
                    or not hmac.compare_digest(
                        credentials[0].encode(),
                        ("Bearer " + self.server.token).encode(),
                    )
                ):
                    return self._failure(401, "authentication_required")
                query = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
                if path == "/api/v1/tasks":
                    if set(query) - {"limit", "cursor", "group"} or any(
                        len(v) != 1 for v in query.values()
                    ):
                        return self._failure(400, "invalid_request")
                    result = self.server.catalogue.page(
                        limit=int(query.get("limit", ["50"])[0]),
                        cursor=query.get("cursor", [None])[0],
                        group=query.get("group", ["all"])[0],
                    )
                elif (
                    path.startswith("/api/v1/tasks/")
                    and path.endswith("/report")
                    and not target.query
                ):
                    document = self.server.provider.report_document(
                        path[len("/api/v1/tasks/") : -len("/report")]
                    )
                    return self._send(200, document, "text/plain; charset=utf-8")
                elif (
                    path.startswith("/api/v1/tasks/")
                    and path.endswith("/snapshot")
                    and not target.query
                ):
                    result = self.server.provider.snapshot(
                        path[len("/api/v1/tasks/") : -len("/snapshot")]
                    )
                else:
                    return self._failure(404, "not_found")
                return self._send(200, result)
            if target.query or path not in self.server.assets:
                return self._failure(404, "not_found")
            mime = (
                "text/html"
                if path == "/"
                else mimetypes.guess_type(path)[0] or "application/octet-stream"
            )
            return self._send(200, self.server.assets[path], mime + "; charset=utf-8")
        except ABCError as exc:
            return self._failure(_ERROR_STATUS.get(exc.code, 503), exc.code)
        except (ValueError, UnicodeError):
            return self._failure(400, "invalid_request")
        except Exception:
            return self._failure(500, "webui_read_failed")

    def _unsupported(self):
        self._failure(405, "method_not_allowed")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _unsupported


def create_server(args):
    from .config import DEFAULT_BOARD_ROOT, DEFAULT_WORKSPACE_ROOT, load_config

    config = load_config(args.config)
    board = args.root or config.get("board_root") or DEFAULT_BOARD_ROOT
    report = (
        args.report_root
        or Path(config.get("workspace_root") or DEFAULT_WORKSPACE_ROOT).expanduser()
        / "tasks"
        / "report"
    )
    assets = args.assets or Path(__file__).parent / "assets" / "webui"
    if not Path(assets).is_dir():
        raise ABCError(
            "webui_assets_missing",
            "Install a package with WebUI assets, or pass --assets to serve",
        )
    return WebServer(
        SnapshotProvider(board, report_root=report),
        Path(assets),
        port=args.port,
        listen_host=getattr(args, "listen_host", "127.0.0.1"),
        public_origin=getattr(args, "public_origin", None),
    )


def run_webui(args):
    from .web_service import Service
    import json

    try:
        action = getattr(args, "webui_action", "serve") or "start"
        if action != "serve":
            service = Service(args.config)
            if action == "start":
                print(
                    "WebUI: "
                    + service.start(open_browser=not getattr(args, "no_open", False))
                )
            elif action == "close":
                service.close()
                print("WebUI stopped; login preference unchanged.")
            elif action == "status":
                print(json.dumps(service.status(), ensure_ascii=False))
            return 0
        with create_server(args) as server:
            launch_file = getattr(args, "launch_file", None)
            if launch_file:
                import os
                import stat

                fd = os.open(
                    launch_file, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600
                )
                with os.fdopen(fd, "w") as handle:
                    st = os.fstat(handle.fileno())
                    if (
                        not stat.S_ISREG(st.st_mode)
                        or st.st_uid != os.getuid()
                        or st.st_mode & 0o077
                        or st.st_nlink != 1
                    ):
                        raise ABCError(
                            "webui_launch_file_unsafe",
                            "Launch file must be private and owned",
                        )
                    handle.truncate(0)
                    handle.write(f"{server.origin}/#token={server.token}\n")
                print(
                    "WebUI ready; private launch link written to the configured file.",
                    flush=True,
                )
            else:
                print(f"WebUI: {server.origin}/#token={server.token}", flush=True)
            print(
                "Read-only. Keep this launch link private. Ctrl+C stops only WebUI.",
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            finally:
                if launch_file:
                    Path(launch_file).unlink(missing_ok=True)
        return 0
    except (ABCError, OSError, ValueError) as exc:
        print(
            f"{getattr(exc, 'code', 'webui_start_failed')}: verify configuration, existing roots, packaged assets and fixed port; run agentbc setup if needed."
        )
        return 1
