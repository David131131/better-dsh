"""Seatbelt confinement and a local, single-route DeepSeek streaming relay."""

from http.client import HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
from socketserver import TCPServer
import ssl
import subprocess
import sys
import threading


SEATBELT = "/usr/bin/sandbox-exec"
UPSTREAM = "api.deepseek.com"
ENDPOINT = "/anthropic/v1/messages"
SYSTEM_CA = "/etc/ssl/cert.pem"
BLOCKED = ("/Users", "/Volumes", "/private/tmp", "/private/var/folders",
           "/private/var/tmp", "/private/var/root", "/opt/homebrew/var")
SAFE_PATH = os.pathsep.join(dict.fromkeys([
    str(Path(sys.executable).parent), "/usr/local/bin", "/opt/homebrew/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
]))


def wrap(command, readable, writable, port=None, *, read_denied=(), read_only_allowlist=False):
    def terms(paths):
        return " ".join(f"(subpath {json.dumps(str(Path(p).resolve()), ensure_ascii=False)})" for p in paths)

    # Allow system libraries and tools while restricting user/project files.
    file_reads = "(deny file-read-data)" if read_only_allowlist else f"(deny file-read-data {terms(BLOCKED)})"
    rules = ["(version 1)", "(allow default)", "(deny network*)", file_reads, "(deny file-write*)"]
    if readable or writable:
        rules.append(f"(allow file-read-data {terms((*readable, *writable))})")
    if read_only_allowlist:
        rules.append('(allow file-read-data (literal "/" "/Library" "/System" "/System/Volumes" "/usr" "/usr/local"))')
    if read_denied:
        rules.append(f"(deny file-read-data {terms(read_denied)})")
    if writable:
        rules.append(f"(allow file-write* {terms(writable)} (literal \"/dev/null\"))")
    if port is not None:
        rules.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    return [SEATBELT, "-p", "\n".join(rules), *map(str, command)]


def environment(home, scratch):
    return {"HOME": str(home), "TMPDIR": str(scratch), "PATH": SAFE_PATH,
            "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "DSH_HOME": str(home),
            "DSH_TELEMETRY_DISABLED": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def check_code(code, workspace, scratch):
    script = "import sys\nsys.path.insert(0, '.')\n" + code
    command = wrap([sys.executable, "-I", "-B", "-c", script], (workspace,), (scratch,))
    try:
        result = subprocess.run(command, cwd=workspace, env=environment(scratch, scratch),
                                capture_output=True, text=True, errors="replace", timeout=8)
    except subprocess.TimeoutExpired:
        return False, "独立检查超时"
    return result.returncode == 0, (result.stderr or result.stdout)[-2000:] or "断言通过"


def isolation_probe(root, dsh_runtime):
    allowed = socket.socket()
    forbidden = socket.socket()
    with allowed, forbidden:
        allowed.bind(("127.0.0.1", 0))
        forbidden.bind(("127.0.0.1", 0))
        allowed.listen()
        forbidden.listen()
        work = root / "workspace"
        work.mkdir()
        secret = root / "reference.txt"
        secret.write_text("hidden")
        probe = """
import errno, pathlib, socket, sys

workspace, hidden, runtime, allowed, other = sys.argv[1:]
def denied(action):
    try: action()
    except OSError as exc: assert exc.errno in (errno.EACCES, errno.EPERM), exc
    else: raise AssertionError('restriction was not enforced')
pathlib.Path(workspace, 'written.txt').write_text('sandbox write allowed')
denied(lambda: pathlib.Path(hidden).read_text())
denied(lambda: pathlib.Path(hidden).write_text('blocked'))
assert pathlib.Path(runtime, 'node_modules/@deepseek-ai/dsh/package.json').read_text()
with socket.create_connection(('127.0.0.1', int(allowed)), 1): pass
denied(lambda: socket.create_connection(('127.0.0.1', int(other)), 1))
denied(lambda: socket.create_connection(('1.1.1.1', 443), 1))
"""
        args = [sys.executable, "-I", "-B", "-c", probe, work, secret, dsh_runtime,
                allowed.getsockname()[1], forbidden.getsockname()[1]]
        command = wrap(["/bin/bash", "-c", 'exec "$@"', "bash", *args],
                       (dsh_runtime,), (work,), allowed.getsockname()[1])
        subprocess.run(command, cwd=work, env=environment(work, work), check=True, timeout=12)


class Relay:
    def __init__(self, key, before_request, remaining):
        self.key = key
        self.token = secrets.token_urlsafe(24)
        self.before_request = before_request
        self.remaining = remaining
        self.rows = []
        self.rejected = 0
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_POST(self):
                relay.forward(self)

        class LoopbackServer(ThreadingHTTPServer):
            def server_bind(self):
                # Binding a numeric loopback address needs no reverse DNS.
                TCPServer.server_bind(self)
                self.server_name, self.server_port = self.server_address

        self.server = LoopbackServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def forward(self, handler):
        auth = handler.headers.get("x-api-key") or handler.headers.get("Authorization", "").removeprefix("Bearer ")
        length = handler.headers.get("Content-Length", "0")
        if (handler.path != ENDPOINT or auth != self.token or not length.isdigit()
                or not 0 < int(length) <= 12_000_000):
            self.rejected += 1
            handler.send_error(403)
            return
        raw = handler.rfile.read(int(length))
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("model") != "deepseek-flash":
                raise ValueError("unexpected model")
            if body.get("stream") is not True:
                raise ValueError("expected streaming request")
        except (TypeError, ValueError):
            self.rejected += 1
            handler.send_error(400)
            return
        if self.before_request():
            try:
                handler.send_error(409, "Public checks confirmed")
            except OSError:
                pass
            return
        if self.remaining() <= 0:
            handler.send_error(408)
            return
        row = {"model": body["model"], "thinking": body.get("thinking"),
               "effort": body.get("output_config", {}).get("effort"), "finished": False,
               "usage": {}, "http_status": None}
        self.rows.append(row)
        if self.local_response(handler, body, row):
            return
        prepared = self.prepare_body(body)
        if prepared is not body:
            raw = json.dumps(prepared, ensure_ascii=False).encode()
        context = ssl.create_default_context(cafile=SYSTEM_CA)
        connection = HTTPSConnection(UPSTREAM, timeout=max(3, min(50, self.remaining())), context=context)
        responded = False
        try:
            headers = {"x-api-key": self.key, "content-type": "application/json",
                       "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01"),
                       "accept": "text/event-stream"}
            for name in ("anthropic-beta", "user-agent", "x-deepseek-harness-user-id",
                         "x-deepseek-harness-session-id"):
                if handler.headers.get(name):
                    headers[name] = handler.headers[name]
            connection.request("POST", ENDPOINT, body=raw, headers=headers)
            response = connection.getresponse()
            row["http_status"] = response.status
            handler.send_response(response.status)
            for name in ("Content-Type", "Retry-After"):
                if response.getheader(name):
                    handler.send_header(name, response.getheader(name))
            handler.end_headers()
            responded = True
            pending = b""
            while data := response.read1(16_384):
                pending += data
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith(b"data:"):
                        self.record_event(row, line[5:])
                handler.wfile.write(data)
                handler.wfile.flush()
        except (OSError, ValueError) as error:
            row["transport_error"] = type(error).__name__
            if not responded:
                try:
                    handler.send_error(502, "Relay transport failed")
                except OSError:
                    pass
        finally:
            connection.close()

    def local_response(self, handler, body, row):
        return False

    def prepare_body(self, body):
        return body

    @staticmethod
    def record_event(row, line):
        try:
            event = json.loads(line)
        except ValueError:
            return
        kind = event.get("type")
        if kind == "message_start":
            usage = event.get("message", {}).get("usage", {})
        elif kind == "message_delta":
            usage = event.get("usage", {})
        elif kind == "message_stop":
            row["finished"] = True
            return
        else:
            return
        row["usage"].update({key: value for key, value in usage.items()
                             if type(value) is int and value >= 0})
