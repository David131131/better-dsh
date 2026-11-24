"""Exercise the documented macOS headless setup with a loopback model fixture."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread


PLUGIN = Path(__file__).resolve().parent
EXPERIMENT = PLUGIN.parent / "experiment-v0"
DSH = EXPERIMENT / ".runtime/node_modules/.bin/dsh"
TOOL_INPUT = {"command": "printf 'ready\\n' > result.txt", "description": "Update the fixture"}


@contextmanager
def model(edit_first):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.path != "/anthropic/v1/messages":
                self.send_error(404)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(request["model"])
            editing = edit_first and len(calls) == 1
            block = ({"type": "tool_use", "id": "call_release_smoke", "name": "bash", "input": {}}
                     if editing else {"type": "text", "text": "Loopback reply."})
            events = [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block},
            ]
            if editing:
                events.append({"type": "content_block_delta", "index": 0, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps(TOOL_INPUT),
                }})
            events.extend([
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use" if editing else "end_turn"}},
                {"type": "message_stop"},
            ])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for payload in events:
                self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/anthropic", calls
        finally:
            server.shutdown()
            worker.join()


def run(repo, home, config=None, session=None):
    with model(edit_first=session is None) as (address, calls):
        args = [DSH, "--profile", "headless", "--patch", EXPERIMENT / "base.patch.yml",
                "--patch", EXPERIMENT / "high.patch.yml"]
        if config is not None:
            args += ["--patch", PLUGIN / "checkpoint.patch.yml", "--patch", config]
        if session is not None:
            args += ["--session-id", session]
        args += ["--json", "--permission-mode", "workspace-write", "--approval-policy", "never",
                 "Change result.txt to ready and report what happened." if session is None else "Continue verification."]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "DSH_HOME": str(home),
               "TMPDIR": str(home), "LANG": "en_US.UTF-8", "DSH_TELEMETRY_DISABLED": "1",
               "DEEPSEEK_API_KEY": "release-smoke", "DEEPSEEK_BASE_URL": address}
        proc = subprocess.run(args, cwd=repo, env=env, text=True, capture_output=True, timeout=65)
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    final = next((event for event in reversed(events) if event.get("type") == "final"), {})
    if proc.returncode or final.get("reason") != {"kind": "completed"}:
        raise RuntimeError(f"Headless smoke failed\n{proc.stderr[-3000:]}\n{proc.stdout[-3000:]}")
    records = [json.loads(line.removeprefix("better-dsh: ")) for line in proc.stderr.splitlines()
               if line.startswith("better-dsh: ")]
    assert calls and all(name == "deepseek-flash" for name in calls), calls
    return final["sessionId"], len(calls), records


def main():
    with TemporaryDirectory(prefix="better dsh smoke ") as raw:
        root = Path(raw).resolve()
        for case, mode, expected, requests in (
            ("observe", "observe", "would-stop", 2),
            ("stop", "stop", "stopped", 1),
            ("failed-check", "stop", "check-failed", 2),
        ):
            stage = root / case
            repo = stage / "repo"
            home = stage / "home"
            repo.mkdir(parents=True)
            home.mkdir()
            (repo / "result.txt").write_text("initial\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "result.txt"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "Initial fixture"], cwd=repo, check=True)
            config = stage / "enable.yml"
            config.write_text(json.dumps([{"id": "better-dsh-checkpoint", "config": {
                "enabled": True, "mode": mode, "workspace": str(repo), "paths": ["result.txt"],
                "checks": [{"name": "present", "command": "test -s result.txt"},
                           {"name": "expected content", "command": "grep -qx ready result.txt"},
                           {"name": "independent check", "command": "false" if case == "failed-check" else "git diff --check"}],
            }}]))
            session, observed, records = run(repo, home, config=config)
            assert observed == requests and any(r["event"] == expected for r in records), (case, observed, records)
            assert (repo / "result.txt").read_text() == "ready\n"
            print(f"{case}: passed", flush=True)
            if case == "stop":
                resumed, observed, records = run(repo, home, session=session)
                assert resumed == session and observed == 1 and not records
                print("resume with plugin disabled: passed", flush=True)


if __name__ == "__main__":
    main()
