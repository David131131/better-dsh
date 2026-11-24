#!/usr/bin/env python3
"""Run the fixed-effort DSH pilot and retain auditable local results."""

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from tasks import CASE_IDS, HARD_CASE_IDS, SMOKE_CASES, category, grade, prepare, prompt_for, write_reference


ROOT = Path(__file__).resolve().parent
PIN = "0.2.2-rc.1"
DEFAULT_DSH = ROOT / ".runtime/node_modules/.bin/dsh"
ROUTE = ("deepseek-official", "deepseek-flash")
BUCKETS = ("inputTokens", "cacheReadTokens", "outputTokens", "reasoningTokens", "totalTokens")
# Fixed published peak USD per million tokens, for normalized comparisons.
PRICES = {"inputTokens": 0.30, "cacheReadTokens": 0.006, "outputTokens": 1.20}
SUITES = {"smoke": (SMOKE_CASES, 1), "baseline": (CASE_IDS, 2), "hard": (HARD_CASE_IDS, 2)}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_events(path):
    rows = []
    malformed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
            else:
                malformed += 1
        except ValueError:
            malformed += 1
    return rows, malformed


def dsh_env(home):
    env = os.environ.copy()
    for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL",
                "DSH_PERMISSION_MODE", "DSH_TELEMETRY_OTLP_URL"):
        env.pop(key, None)
    env.update(DSH_HOME=str(home), DSH_TELEMETRY_DISABLED="1")
    return env


def preflight(dsh):
    with tempfile.TemporaryDirectory(prefix="better-dsh-check-") as tmp:
        env = dsh_env(tmp)
        version = subprocess.check_output([dsh, "--version"], text=True, env=env, timeout=20).strip()
        if version != PIN:
            raise ValueError(f"实验需要 DSH {PIN}，实际为 {version}。请按 README 安装项目内版本。")
        command = [dsh, "--profile", "headless"]
        help_text = subprocess.check_output(command + ["--help"], text=True, env=env, timeout=30)
        for option in ("--json", "--permission-mode", "--approval-policy"):
            if option not in help_text:
                raise ValueError(f"headless 帮助缺少 {option}。")
        for effort in ("high", "low"):
            config = subprocess.check_output(
                command + ["--patch", ROOT / "base.patch.yml", "--patch", ROOT / f"{effort}.patch.yml",
                           "--dump-config"],
                text=True, env=env, timeout=30,
            )
            if f"reasoningEffort: {effort}" not in config or "compression: none" not in config:
                raise ValueError(f"{effort} 配置未正确合成。")
        return version


def stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    except ProcessLookupError:
        process.wait()


def execute(command, workspace, env, directory, timeout, cancel=None):
    outcome = None
    started = time.monotonic()
    with (directory / "events.jsonl").open("wb") as stdout, (directory / "stderr.log").open("wb") as stderr:
        process = subprocess.Popen(
            command, cwd=workspace, env=env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=True,
        )
        try:
            if cancel is None:
                process.wait(timeout=timeout)
            else:
                deadline = started + timeout
                while process.poll() is None:
                    if cancel.wait(min(0.25, max(0, deadline - time.monotonic()))):
                        outcome = "interrupted"
                        stop(process)
                        break
                    if time.monotonic() >= deadline and process.poll() is None:
                        raise subprocess.TimeoutExpired(command, timeout)
        except subprocess.TimeoutExpired:
            outcome = "timeout"
            stop(process)
        except KeyboardInterrupt:
            outcome = "interrupted"
            stop(process)
    return process.returncode, round(time.monotonic() - started, 3), outcome


def read_session(home, session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        return None, [], 0
    candidates = [
        p for p in (home / "sessions").glob(f"*/{session_id}/session*.jsonl")
        if re.fullmatch(r"session(?:\.v\d+)?\.jsonl", p.name)
    ]
    if not candidates:
        return None, [], 0
    path = max(candidates, key=lambda p: int(re.search(r"\.v(\d+)", p.name)[1])
               if ".v" in p.name else 0)
    events, malformed = read_events(path)
    return path, events, malformed


def count(value):
    return type(value) is int and value >= 0


def task_completed(exit_code, final_reason):
    kind = final_reason.get("kind") if isinstance(final_reason, dict) else final_reason
    return exit_code == 0 and kind == "completed"


def inspect_usage(events, session_events, complete_run):
    started = {(e.get("turn"), e.get("step")) for e in events
               if e.get("type") == "status" and e.get("phase") == "step_start"}
    steps = [e for e in events if e.get("type") == "status" and e.get("phase") == "step_end"]
    ended = {(e.get("turn"), e.get("step")) for e in steps}
    samples = [e.get("usage") for e in steps]
    complete = (
        complete_run and bool(started) and started == ended and len(ended) == len(steps)
        and all(isinstance(sample, dict) for sample in samples)
    )
    totals = {
        key: sum(sample[key] for sample in samples)
        if complete and all(count(sample.get(key)) for sample in samples) else None
        for key in BUCKETS
    }
    if complete and totals["totalTokens"] is None and all(
        all(count(s.get(k)) for k in (*PRICES, "cacheWriteTokens")) for s in samples
    ):
        totals["totalTokens"] = sum(sum(s[k] for k in (*PRICES, "cacheWriteTokens")) for s in samples)
    cost = None
    if complete and all(totals[key] is not None for key in PRICES) and all(
        sample.get("cacheWriteTokens", 0) == 0 for sample in samples
    ):
        cost = round(sum(totals[key] * rate for key, rate in PRICES.items()) / 1_000_000, 10)
    return {
        "steps_started": len(started),
        "steps_with_usage": sum(isinstance(s, dict) for s in samples),
        "retry_attempt_events": sum(e.get("type") == "assistant/attempt" for e in session_events),
        "tool_calls": sum(e.get("type") == "tool_call" for e in events),
        "tokens": totals,
        "normalized_peak_usd": cost,
    }


def run_task(dsh, batch, home, env, case, effort, repeat, timeout, *, repair=None, workspace_root=None, cancel=None):
    workspace = (workspace_root or batch) / "workspace"
    label = "repair" if repair else effort
    directory = batch / f"{case}-{label}-{repeat}"
    directory.mkdir()
    if repair:
        seed, diagnostic = repair
        shutil.copytree(seed, workspace, ignore=shutil.ignore_patterns("__pycache__"))
        prompt = prompt_for(case) + (
            "\n当前目录是此前 low 尝试留下的代码。请从现有改动继续。"
            f"独立验收未通过的规则：{diagnostic}。请修复，并验证相邻边界。"
        )
    else:
        prompt = prepare(case, workspace)
    (directory / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    command = [
        dsh, "--profile", "headless", "--patch", ROOT / "base.patch.yml",
        "--patch", ROOT / f"{effort}.patch.yml", "--json",
        "--permission-mode", "workspace-write", "--approval-policy", "never", prompt,
    ]
    print(f"运行 {directory.name}", flush=True)
    started_utc = datetime.now(timezone.utc).isoformat()
    exit_code, elapsed, outcome = execute(command, workspace, env, directory, timeout, cancel)
    ended_utc = datetime.now(timezone.utc).isoformat()
    events, bad_lines = read_events(directory / "events.jsonl")
    final = next((e for e in reversed(events) if e.get("type") == "final"), {})
    session = next((e for e in events if e.get("type") == "session"), {})
    session_id = session.get("sessionId")
    path, session_events, bad_session_lines = read_session(home, session_id)
    configs = [e["data"]["header"]["config"] for e in session_events
               if e.get("type") == "request/header"]
    route_ok = not bad_session_lines and bool(configs) and all(
        (c.get("provider"), c.get("model")) == ROUTE and c.get("reasoningEffort") == effort
        for c in configs
    )
    completed = task_completed(exit_code, final.get("reason"))
    passed, grade_detail = grade(case, workspace) if completed else (False, "DSH 任务未正常结束")
    if workspace.exists():
        workspace.rename(directory / "workspace")
    result = {
        "case": case, "category": category(case), "effort": effort, "repeat": repeat,
        "repair_from": str(repair[0].parent.relative_to(batch)) if repair else None,
        "session_id": session_id, "session_log": str(path.relative_to(batch)) if path else None,
        "route_ok": route_ok, "request_configs": configs,
        "exit_code": exit_code, "final_reason": final.get("reason"), "elapsed_s": elapsed,
        "started_utc": started_utc, "ended_utc": ended_utc,
        "outcome": outcome or ("completed" if completed else "dsh_error"),
        "passed": passed, "grade_detail": grade_detail,
        "invalid_output_lines": bad_lines, "invalid_session_lines": bad_session_lines,
        **inspect_usage(events, session_events, bool(final) and not outcome and not bad_lines),
    }
    write_json(directory / "result.json", result)
    return result


def summarize(results):
    summary = {}
    for effort in ("high", "low"):
        rows = [r for r in results if r["effort"] == effort]
        passed = sum(r["passed"] for r in rows)
        costs = [r["normalized_peak_usd"] for r in rows]
        verified = [r for r in rows if r["route_ok"]]
        known_cost = sum(cost is not None for cost in costs)
        comparable = bool(rows) and len(verified) == known_cost == len(rows)
        token_totals = {
            key: sum(r["tokens"][key] for r in rows)
            if rows and len(verified) == len(rows) and all(r["tokens"][key] is not None for r in rows)
            else None
            for key in BUCKETS
        }
        summary[effort] = {
            "runs": len(rows), "passed": passed, "route_verified": len(verified),
            "runs_with_known_cost": known_cost,
            "by_category": {
                name: {"runs": sum(r["category"] == name for r in rows),
                       "passed": sum(r["category"] == name and r["passed"] for r in rows)}
                for name in sorted({r["category"] for r in results})
            },
            "median_elapsed_s": statistics.median(r["elapsed_s"] for r in rows) if rows else None,
            "tokens": token_totals,
            "normalized_peak_usd": round(sum(costs), 10) if comparable else None,
            "usd_per_success": round(sum(costs) / passed, 10) if comparable and passed else None,
        }
    return summary


def self_check():
    with tempfile.TemporaryDirectory(prefix="better-dsh-fixtures-") as tmp:
        for case in (*CASE_IDS, *HARD_CASE_IDS):
            workspace = Path(tmp) / case
            prepare(case, workspace)
            if grade(case, workspace)[0]:
                raise ValueError(f"{case} 的初始版本不应通过")
            write_reference(case, workspace)
            passed, detail = grade(case, workspace)
            if not passed:
                raise ValueError(f"{case} 的参考答案未通过：{detail}")
    print(f"{len(CASE_IDS) + len(HARD_CASE_IDS)} 个样本均已验证：初始状态失败，参考答案通过。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("self-check", "check", *SUITES))
    parser.add_argument("--dsh", type=Path, default=DEFAULT_DSH, help="使用固定版本的 dsh 可执行文件")
    parser.add_argument("--timeout", type=int, default=180, help="每次 DSH 调用的秒数上限")
    args = parser.parse_args()
    if args.action == "self-check":
        self_check()
        return 0
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    dsh = args.dsh.absolute()
    version = preflight(dsh)
    print(f"DSH {version} 的 headless 和两档配置检查通过。")
    if args.action == "check":
        return 0

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key and sys.stdin.isatty():
        key = getpass.getpass("DeepSeek API Key（输入不显示）: ")
    if not key:
        parser.error("需要 DeepSeek API Key，可用终端输入或由环境变量 DEEPSEEK_API_KEY 提供。")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (ROOT / "runs").mkdir(exist_ok=True)
    batch = Path(tempfile.mkdtemp(prefix=f"{stamp}-{args.action}-", dir=ROOT / "runs"))
    home = batch / "dsh-home"
    home.mkdir()
    env = dsh_env(home)
    env["DEEPSEEK_API_KEY"] = key
    cases, repeats = SUITES[args.action]
    schedule = [
        (case, effort, repeat)
        for repeat in range(1, repeats + 1)
        for index, case in enumerate(cases)
        for effort in (("high", "low") if (index + repeat) % 2 else ("low", "high"))
    ]
    write_json(batch / "manifest.json", {
        "suite": args.action, "dsh_version": version, "provider": ROUTE[0], "model": ROUTE[1],
        "timeout_s": args.timeout, "cases": cases, "repeats_per_effort": repeats,
        "schedule": [f"{case}-{effort}-{repeat}" for case, effort, repeat in schedule],
        "normalized_peak_usd_per_million": PRICES,
        "input_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                         for name in ("bench.py", "tasks.py", "base.patch.yml", "high.patch.yml", "low.patch.yml")},
    })
    results = []
    print(f"结果目录：{batch}", flush=True)
    for case, effort, repeat in schedule:
        row = run_task(dsh, batch, home, env, case, effort, repeat, args.timeout)
        results.append(row)
        write_json(batch / "summary.json", summarize(results))
        print(f"  通过={row['passed']}  档位已核对={row['route_ok']}  "
              f"估算费用={row['normalized_peak_usd']}", flush=True)
        if row["outcome"] == "interrupted":
            return 130
        if not row["route_ok"] or (args.action == "smoke" and row["normalized_peak_usd"] is None):
            print(f"检查 {batch / f'{case}-{effort}-{repeat}'} 中的 result.json 和 stderr.log 后重试。",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        sys.exit(f"实验未能继续：{error}")
