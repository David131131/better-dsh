"""Compare natural and checkpoint-driven completion at fixed high effort."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import random
import statistics
import tempfile
from threading import Event
import time

from bench import DEFAULT_DSH, PRICES, ROOT, ROUTE, execute, preflight, read_events, read_session, task_completed, write_json
from paired_cases import CASES, clean
from paired_isolation import Relay, check_code, environment, isolation_probe, wrap


TIMEOUT = 240
REPEATS = 3
PARALLELISM = 4
SEED = 20261113
ARMS = ("natural", "checkpoint")
RUNTIME = ROOT / ".runtime"
WIRE_BUCKETS = {"inputTokens": "input_tokens", "cacheReadTokens": "cache_read_input_tokens",
                "outputTokens": "output_tokens"}


def fixture(path, files):
    for name, code in files.items():
        (path / name).write_text(clean(code), encoding="utf-8")


def validate(cases):
    with tempfile.TemporaryDirectory(prefix="better-dsh-paired-check-") as raw:
        root = Path(raw).resolve()
        isolation_probe(root, RUNTIME)
        scratch = root / "scratch"
        scratch.mkdir()
        for label, case in cases.items():
            workspace = root / label
            workspace.mkdir()
            fixture(workspace, case.files)
            passed, detail = check_code(clean(case.public), workspace, scratch)
            if passed or detail.startswith("sandbox-exec:"):
                raise ValueError(f"{label} 初始实现或隔离异常：{detail}")
            fixture(workspace, case.fixed)
            for code in (case.public, case.regression, case.hidden):
                passed, detail = check_code(clean(code), workspace, scratch)
                if not passed:
                    raise ValueError(f"{label} 参考实现未通过：{detail}")


def candidate_digest(workspace):
    digest = sha256()
    for path in sorted(workspace.rglob("*.py")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        digest.update(path.relative_to(workspace).as_posix().encode() + b"\0")
        digest.update(sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class Checkpoint:
    def __init__(self, case, workspace, scratch, arm, cancel, started):
        self.case = case
        self.workspace = workspace
        self.scratch = scratch
        self.arm = arm
        self.cancel = cancel
        self.started = started
        self.events = []
        self.digest = None
        self.confirmed = False
        self.stop = Event()

    def observe(self, *, request_boundary=True):
        digest = candidate_digest(self.workspace)
        if digest != self.digest:
            self.digest = digest
            public, public_detail = check_code(clean(self.case.public), self.workspace, self.scratch)
            regression, regression_detail = (
                check_code(clean(self.case.regression), self.workspace, self.scratch)
                if public else (None, None)
            )
            self.confirmed = public and regression
            self.events.append({
                "elapsed_s": round(time.monotonic() - self.started, 3),
                "digest": digest, "public": public, "regression": regression,
                "public_detail": public_detail, "regression_detail": regression_detail,
                "request_boundary": request_boundary,
            })
        if request_boundary and self.arm == "checkpoint" and self.confirmed:
            self.stop.set()
            return True
        return False

    def wait(self, timeout):
        return self.stop.wait(timeout) or self.cancel.is_set()


def usage_rows(rows, *, stopped, rejected):
    accepted = [row for row in rows if row["http_status"] == 200]
    valid = [row for row in accepted if row["finished"] and all(
        wire in row["usage"] or bucket == "cacheReadTokens" for bucket, wire in WIRE_BUCKETS.items()
    ) and row["usage"].get("cache_creation_input_tokens", 0) == 0]
    tokens = {bucket: sum(row["usage"].get(wire, 0) for row in valid) for bucket, wire in WIRE_BUCKETS.items()}
    complete = bool(accepted) and len(valid) == len(accepted) and not rejected and stopped
    estimated = round(sum(tokens[k] * rate for k, rate in PRICES.items()) / 1_000_000, 10)
    return {"tokens": tokens, "complete": complete, "estimated_usd": estimated,
            "requests_metered": len(valid), "requests_forwarded": len(rows)}


def run(batch, case_id, case, repeat, arm, key, cancel):
    if cancel.is_set():
        return None
    label = f"{case_id}-{repeat}-{arm}"
    print(f"运行 {label}", flush=True)
    path = batch / label
    path.mkdir()
    workspace, inputs, home, scratch = (path / n for n in ("workspace", "inputs", "dsh-home", "scratch"))
    for p in (workspace, inputs, home, scratch):
        p.mkdir()
    fixture(workspace, case.files)
    fixture(inputs, {
        "public.py": "import sys\nsys.path.insert(0, '.')\n" + clean(case.public),
        "regression.py": "import sys\nsys.path.insert(0, '.')\n" + clean(case.regression),
    })
    for name in ("base.patch.yml", "high.patch.yml"):
        (inputs / name).write_bytes((ROOT / name).read_bytes())
    prompt = case.prompt + (
        "\n在当前目录修改实现。可在当前目录运行 python3 -B ../inputs/public.py 与 "
        "python3 -B ../inputs/regression.py 验证，不要修改验收文件。"
    )
    (path / "prompt.txt").write_text(prompt + "\n")
    started = time.monotonic()
    checkpoint = Checkpoint(case, workspace, scratch, arm, cancel, started)
    remaining = lambda: TIMEOUT - (time.monotonic() - started)
    with Relay(key, checkpoint.observe, remaining) as relay:
        env = environment(home, scratch)
        env.update(DEEPSEEK_API_KEY=relay.token,
                   DEEPSEEK_BASE_URL=f"http://127.0.0.1:{relay.port}/anthropic")
        dsh_args = [DEFAULT_DSH, "--profile", "headless", "--patch", inputs / "base.patch.yml",
                    "--patch", inputs / "high.patch.yml", "--json",
                    "--permission-mode", "danger-full-access", "--approval-policy", "never", prompt]
        # The outer Seatbelt confines DSH and every child. Nesting Seatbelt is unnecessary.
        command = wrap(dsh_args, (RUNTIME, inputs), (workspace, home, scratch), relay.port)
        exit_code, elapsed, outcome = execute(command, workspace, env, path, TIMEOUT, checkpoint)
    if checkpoint.stop.is_set():
        outcome = "checkpoint_stop"
    checkpoint.observe(request_boundary=False)
    events, invalid_lines = read_events(path / "events.jsonl")
    session = next((e for e in events if e.get("type") == "session"), {})
    final = next((e for e in reversed(events) if e.get("type") == "final"), {})
    session_path, session_events, invalid_session = read_session(home, session.get("sessionId"))
    headers = [e["data"]["header"]["config"] for e in session_events if e.get("type") == "request/header"]
    route_ok = not invalid_session and bool(headers and relay.rows) and all(
        (h.get("provider"), h.get("model")) == ROUTE and h.get("reasoningEffort") == "high" for h in headers
    ) and all(row["effort"] == "high" for row in relay.rows)
    if outcome is None:
        outcome = "completed" if task_completed(exit_code, final.get("reason")) else "dsh_error"
    public_passed, public_detail = check_code(clean(case.public), workspace, scratch)
    regression_passed, regression_detail = check_code(clean(case.regression), workspace, scratch)
    hidden_passed, hidden_detail = check_code(clean(case.hidden), workspace, scratch)
    in_budget = elapsed <= TIMEOUT and outcome in ("completed", "checkpoint_stop")
    costs = usage_rows(relay.rows, stopped=outcome in ("completed", "checkpoint_stop"), rejected=relay.rejected)
    row = {
        "label": label, "case": case_id, "repeat": repeat, "arm": arm, "route_ok": route_ok,
        "outcome": outcome, "exit_code": exit_code, "final_reason": final.get("reason"),
        "elapsed_s": elapsed, "in_budget": in_budget,
        "passed": bool(route_ok and in_budget and public_passed and regression_passed and hidden_passed),
        "public_passed": public_passed, "public_detail": public_detail,
        "regression_passed": regression_passed, "regression_detail": regression_detail,
        "hidden_passed": hidden_passed, "hidden_detail": hidden_detail,
        "first_public_pass_s": next((e["elapsed_s"] for e in checkpoint.events if e["public"]), None),
        "first_confirmed_s": next((e["elapsed_s"] for e in checkpoint.events
                                   if e["public"] and e["regression"]), None),
        "invalid_event_lines": invalid_lines, "invalid_session_lines": invalid_session,
        "tool_calls": sum(e.get("type") == "tool_call" for e in events),
        "session_log": str(session_path.relative_to(path)) if session_path else None,
        "blocked_relay_requests": relay.rejected,
        **costs,
    }
    write_json(path / "checkpoints.json", checkpoint.events)
    write_json(path / "relay-usage.json", relay.rows)
    write_json(path / "result.json", row)
    print(f"  {label} {outcome} high核对={route_ok} 通过={row['passed']} "
          f"耗时={elapsed:.1f}s 计量完整={costs['complete']}", flush=True)
    return row


def summarize(batch, schedule):
    results = {}
    for case, repeat, first, second in schedule:
        for arm in (first, second):
            label = f"{case}-{repeat}-{arm}"
            path = batch / label / "result.json"
            if path.is_file():
                results[label] = json.loads(path.read_text())
    by_arm = {}
    for arm in ARMS:
        rows = [r for r in results.values() if r["arm"] == arm]
        passed = sum(r["passed"] for r in rows)
        metered = [r for r in rows if r["complete"] and r["route_ok"]]
        cost = sum(r["estimated_usd"] for r in metered)
        by_arm[arm] = {
            "recorded": len(rows), "verified_high": sum(r["route_ok"] for r in rows),
            "passed": passed, "hidden_passed": sum(r["hidden_passed"] for r in rows),
            "outcomes": dict(Counter(r["outcome"] for r in rows)),
            "median_elapsed_s": statistics.median(r["elapsed_s"] for r in rows) if rows else None,
            "complete_metering": len(metered), "complete_estimated_usd": round(cost, 10),
            "usd_per_success": round(cost / passed, 10) if passed and len(metered) == len(rows) else None,
        }
    pairs = []
    for case, repeat, _, _ in schedule:
        a, b = (results.get(f"{case}-{repeat}-{arm}") for arm in ARMS)
        if a is None or b is None:
            continue
        pair = {"case": case, "repeat": repeat, "natural_passed": a["passed"], "checkpoint_passed": b["passed"],
                "seconds_saved": round(a["elapsed_s"] - b["elapsed_s"], 3)}
        if all(row["route_ok"] and row["complete"] for row in (a, b)):
            pair["output_tokens_saved"] = a["tokens"]["outputTokens"] - b["tokens"]["outputTokens"]
            pair["estimated_usd_saved"] = round(a["estimated_usd"] - b["estimated_usd"], 10)
        pairs.append(pair)
    write_json(batch / "summary.json", {"planned": len(schedule) * 2, "recorded": len(results),
                                       "by_arm": by_arm, "pairs": pairs})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="验证隔离、夹具和参考实现，零 API 请求")
    parser.add_argument("--holdout", action="store_true", help="使用未参与规则调整的 8 道留出题")
    args = parser.parse_args()
    if args.holdout:
        from high_holdout_cases import CASES as cases
    else:
        cases = CASES
    version = preflight(DEFAULT_DSH)
    validate(cases)
    print(f"{len(cases)} 道题、两套公开检查、隐藏验收及 Seatbelt 探针通过。DSH {version}。", flush=True)
    if args.check:
        return 0
    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API Key（输入不显示）: ")
    if not key:
        parser.error("缺少 API Key。")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suite = "high-holdout" if args.holdout else "high-paired"
    batch = Path(tempfile.mkdtemp(prefix=f"{stamp}-{suite}-", dir=ROOT / "runs"))
    rng = random.Random(SEED)
    if args.holdout:
        first_arms = [arm for arm in ARMS for _ in range(len(cases) // 2)]
        rng.shuffle(first_arms)
        initial = dict(zip(cases, first_arms, strict=True))
    else:
        initial = {case: rng.choice(ARMS) for case in cases}
    schedule = []
    for repeat in range(1, REPEATS + 1):
        for case in rng.sample(list(cases), len(cases)):
            order = ARMS if initial[case] == "natural" else ARMS[::-1]
            first, second = order if repeat % 2 else order[::-1]
            schedule.append([case, repeat, first, second])
    write_json(batch / "manifest.json", {
        "suite": suite, "dsh_version": version, "model": ROUTE[1], "provider": ROUTE[0], "effort": "high",
        "repeats": REPEATS, "timeout_s": TIMEOUT, "seed": SEED, "schedule": schedule,
        "parallelism": PARALLELISM, "titles": {i: c.title for i, c in cases.items()},
        "prices": PRICES, "input_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("paired_bench.py", "paired_isolation.py", "paired_cases.py",
                         "base.patch.yml", "high.patch.yml", "bench.py")
                         + (("high_holdout_cases.py",) if args.holdout else ())
        },
    })
    print(f"结果目录：{batch}", flush=True)
    cancel = Event()
    pool = ThreadPoolExecutor(max_workers=PARALLELISM)
    try:
        for repeat in range(1, REPEATS + 1):
            pairs = [p for p in schedule if p[1] == repeat]
            for position in (2, 3):
                futures = [pool.submit(run, batch, p[0], cases[p[0]], p[1], p[position], key, cancel)
                           for p in pairs]
                rows = [future.result() for future in futures]
                summarize(batch, schedule)
                if not any(r["route_ok"] for r in rows):
                    print("这一组没有核对成功的 high 会话，停止继续付费。", flush=True)
                    return 1
    except KeyboardInterrupt:
        cancel.set()
        return 130
    finally:
        pool.shutdown(wait=True, cancel_futures=cancel.is_set())
        summarize(batch, schedule)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
