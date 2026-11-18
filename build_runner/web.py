"""Build Runner local HTTP server."""

import argparse
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from urllib.parse import parse_qs
import webbrowser

# The Hub invokes the manifest entry as a file.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from build_runner import jobs
from build_runner.paths import ROOT, ToolError, data_dir, read_json, write_json
from build_runner.web_api import Api, Download, HttpError
from standalone_update import handle_update

MAX_BODY = 1024 * 1024
STATIC_FILES = {
    "/": ("index.html", "text/html"),
    "/index.html": ("index.html", "text/html"),
    "/style.css": ("style.css", "text/css"),
    "/app.js": ("app.js", "text/javascript"),
    "/config.js": ("config.js", "text/javascript"),
    "/operations.js": ("operations.js", "text/javascript"),
    "/packs.js": ("packs.js", "text/javascript"),
    "/ui-kit.css": ("ui-kit.css", "text/css"),
    "/tb-theme.js": ("tb-theme.js", "text/javascript"),
    "/tb-icons.js": ("tb-icons.js", "text/javascript"),
    "/tb-dirpicker.js": ("tb-dirpicker.js", "text/javascript"),
    "/tb-standalone-update.js": ("tb-standalone-update.js", "text/javascript"),
}


def loopback_origins(port):
    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BuildRunner"
    timeout = 15

    def log_message(self, format, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except ConnectionError:
            pass

    def handle_expect_100(self):
        try:
            self.validate_host()
            if self.command not in ("POST", "DELETE"):
                raise HttpError(417, "EXPECTATION_FAILED", "此方法不接受 Expect: 100-continue。")
            self.validate_write()
            self.validate_size(self.body_size())
            return super().handle_expect_100()
        except HttpError as exc:
            self.close_connection = True
            self.send_json(exc.status, exc.response)
            self.finish_rejection()
        return False

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        ancestors = " ".join(("'self'", *self.server.hub_origins))
        theme_sources = " ".join(f"{origin}/tool-theme.css" for origin in self.server.hub_origins)
        self.send_header("Content-Security-Policy",
                         f"default-src 'self'; style-src 'self' {theme_sources}; "
                         f"base-uri 'none'; form-action 'none'; frame-ancestors {ancestors}")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_bytes(self, status, content_type, content):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_download(self, status, content, filename):
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def send_json(self, status, value):
        self.send_bytes(status, "application/json; charset=utf-8",
                        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def validate_host(self):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        if len(hosts) != 1 or hosts[0].lower() not in (f"localhost:{port}", f"127.0.0.1:{port}"):
            raise HttpError(403, "FORBIDDEN_HOST", "Host 必須是此伺服器的 loopback 位址。")

    def body_size(self):
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is None and len(lengths) == 1
                and re.fullmatch(r"[0-9]{1,10}", lengths[0])):
            return int(lengths[0])
        return None

    def validate_write(self):
        if self.headers.get_all("X-Toolbox-Request") != ["1"]:
            raise HttpError(403, "MISSING_TOOLBOX_HEADER", "缺少 X-Toolbox-Request: 1。")
        origins = self.headers.get_all("Origin", [])
        allowed = (*loopback_origins(self.server.server_port), *self.server.hub_origins)
        if origins and (len(origins) != 1 or origins[0].lower() not in allowed):
            raise HttpError(403, "FORBIDDEN_ORIGIN", "不允許此請求來源。")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].partition(";")[0].strip().lower() != "application/json":
            raise HttpError(415, "UNSUPPORTED_MEDIA_TYPE", "Content-Type 必須是 application/json。")

    @staticmethod
    def validate_size(size):
        if size is None:
            raise HttpError(400, "INVALID_BODY_LENGTH", "需要有效的 Content-Length。")
        if size > MAX_BODY:
            raise HttpError(413, "PAYLOAD_TOO_LARGE", "請求內容上限為 1 MB。")

    def read_body(self):
        size = self.body_size()
        # Drain a bounded body before rejecting it. Closing with unread input
        # can reset the Windows socket before the client receives the 403.
        raw = self.rfile.read(size) if size is not None and size <= MAX_BODY else b""
        self.body_read = len(raw) == size
        self.validate_host()
        self.validate_write()
        self.validate_size(size)
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, RecursionError) as exc:
            raise HttpError(400, "BAD_JSON", "請提供有效的 UTF-8 JSON 物件。") from exc
        if len(raw) != size or not isinstance(value, dict):
            raise HttpError(400, "BAD_JSON", "請求本體必須是完整的 JSON 物件。")
        return value

    def drain_rejected_body(self):
        size = self.body_size()
        if size is None or not 0 < size <= MAX_BODY + 64 * 1024:
            return False
        # Discard only a bounded amount, without parsing oversized JSON.
        # If the client sent all bytes, the connection can stay synchronized.
        remaining = size
        try:
            deadline = time.monotonic() + 0.5
            while remaining and time.monotonic() < deadline:
                self.connection.settimeout(max(0.001, deadline - time.monotonic()))
                block = self.rfile.read1(min(remaining, 16384))
                if not block:
                    break
                remaining -= len(block)
        except OSError:
            return False
        finally:
            self.connection.settimeout(self.timeout)
        return remaining == 0

    def finish_rejection(self):
        # Half-close first so Windows has a chance to deliver the response
        # before the connection is torn down.
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(0.25)
            self.connection.recv(1)
        except OSError:
            pass

    def handle_request(self):
        self.body_read = False
        try:
            method = "GET" if self.command == "HEAD" else self.command
            if method not in ("POST", "DELETE"):
                self.validate_host()
            if method not in ("GET", "POST", "DELETE"):
                raise HttpError(405, "METHOD_NOT_ALLOWED", "不支援此 HTTP 方法。")
            if method == "GET" and (self.headers.get("Transfer-Encoding") is not None
                                    or self.headers.get_all("Content-Length", []) not in ([], ["0"])):
                raise HttpError(400, "INVALID_BODY_LENGTH", "GET 與 HEAD 不接受請求本體。")
            body = self.read_body() if method != "GET" else {}
            path, _, raw_query = self.path.partition("?")
            if method == "POST" and path == "/api/standalone-update":
                if self.server.managed:
                    # Started by the Hub: report "managed, no standalone update" instead of 403 (no browser error);
                    # opening the standalone updater is still refused.
                    if isinstance(body, dict) and body.get("action") == "check":
                        self.send_json(200, {"ok": True, "update": None, "managed": True})
                        return
                    raise HttpError(403, "MANAGED_BY_HUB", "請從 Hub 更新工具。")
                self.send_json(200, handle_update(ROOT, body))
                return
            if method == "GET" and path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self.send_bytes(200, f"{content_type}; charset=utf-8", (ROOT / "ui" / name).read_bytes())
                return
            if method == "GET" and path == "/favicon.ico":
                self.send_bytes(204, "image/x-icon", b"")
                return
            query = parse_qs(raw_query, keep_blank_values=True)
            status, result = self.server.api.route(method, path, query, body)
            if isinstance(result, Download):
                self.send_download(status, result.data, result.filename)
                return
            self.send_json(status, result)
        except HttpError as exc:
            # Keep a synchronized HTTP/1.1 connection open. In particular,
            # Windows can reset an immediate close even after a small reply.
            if not self.body_read and (self.headers.get("Transfer-Encoding") is not None
                                       or self.headers.get_all("Content-Length", []) not in ([], ["0"])):
                if not self.drain_rejected_body():
                    self.close_connection = True
            self.send_json(exc.status, exc.response)
            if self.close_connection:
                self.finish_rejection()
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            self.close_connection = True
            print(f"Web request failed: {exc}", file=sys.stderr)
            self.send_json(500, HttpError(500, "INTERNAL", "伺服器處理請求時發生錯誤。").response)

    do_GET = do_HEAD = do_POST = do_DELETE = do_OPTIONS = do_PUT = do_PATCH = handle_request


def reject_constant(value):
    raise ValueError(value)


def parent_alive(pid):
    try:
        if os.name == "nt":
            with jobs.process_token(pid) as token:
                return token is not None
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watch_parent(server, pid):
    while parent_alive(pid):
        time.sleep(1)
    server.shutdown()


def publish_listener(data, manifest, url, port):
    """Advertise the live URL for local peers (e.g. the Culprit tool's runner_client).

    Peers read this file, then verify with GET /api/health; a stale file is
    therefore harmless, but release_listener() still cleans it up on shutdown.
    """
    write_json(data / "listener.json", {
        "tool": manifest.get("id", "build-runner"), "version": manifest.get("version"),
        "url": url, "port": port, "pid": os.getpid(), "startedAt": jobs.now(),
    })


def release_listener(data):
    """Only delete the file when this instance still owns it (two servers never clobber)."""
    try:
        if read_json(data / "listener.json", {}).get("pid") == os.getpid():
            (data / "listener.json").unlink(missing_ok=True)
    except OSError:
        pass


def bind_server(port, default):
    candidates = [port] if port is not None else [*range(default, default + 21), 0]
    for candidate in candidates:
        try:
            return LocalServer(("127.0.0.1", candidate), Handler)
        except OSError as exc:
            if port is None and candidate != 0:
                continue
            code = getattr(exc, "winerror", None) or exc.errno
            if code in (10048, errno.EADDRINUSE):
                reason = "port 已被其他程式佔用"
            elif code in (10013, errno.EACCES):
                reason = "port 被 Windows 保留或拒絕存取"
            else:
                reason = str(exc)
            raise ToolError("BIND_FAILED", f"無法監聽 127.0.0.1:{candidate}，{reason}", 5) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--data-dir")
    parser.add_argument("--hub-origin")
    args = parser.parse_args(argv)
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.parent_pid < 0:
        parser.error("--parent-pid cannot be negative")
    hub_port = 8700
    if args.hub_origin:
        match = re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})", args.hub_origin, re.I)
        if not match or not 1 <= int(match[1]) <= 65535:
            parser.error("--hub-origin must be an HTTP loopback origin with a port")
        hub_port = int(match[1])
    try:
        data = data_dir(args.data_dir)
        manifest = read_json(ROOT / "tool.json")
        if args.parent_pid and not parent_alive(args.parent_pid):
            raise ToolError("PARENT_NOT_FOUND", "父行程已結束。", 5)
        for file, _ in set(STATIC_FILES.values()):
            if not (ROOT / "ui" / file).is_file():
                raise ToolError("ASSET_MISSING", f"缺少介面檔案 {file}。", 1)
        try:
            with jobs.lock(data):
                jobs.sweep_orphans(data)
        except (ToolError, OSError):
            pass  # Orphan cleanup is best effort and must never block startup.
        server = bind_server(args.port, manifest["web"]["defaultPort"])
    except (ToolError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return getattr(exc, "exit_code", 1)
    with server:
        server.managed = bool(args.parent_pid)
        server.hub_origins = loopback_origins(hub_port)
        server.api = Api(data, manifest, server)
        url = f"http://127.0.0.1:{server.server_port}"
        publish_listener(data, manifest, url, server.server_port)
        print(f"READY {url}", flush=True)
        if args.parent_pid:
            threading.Thread(target=watch_parent, args=(server, args.parent_pid), daemon=True).start()
        if args.open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.api.analyses.close()
    release_listener(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
