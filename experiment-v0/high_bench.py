"""Run a larger fixed-high campaign with independent grading and bounded parallelism."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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

from bench import BUCKETS, DEFAULT_DSH, PRICES, ROOT, ROUTE, dsh_env, preflight, run_task, write_json
from high_cases import ORIGINS, SOURCES, make_cases
from tasks import REPAIRS, grade, prepare, write_reference


REPEATS = 3
PARALLELISM = 4
TIMEOUT = 240
ORDER_SEED = 20261112


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def validate(cases):
    provenance = json.loads((SOURCES / "manifest.json").read_text())
    for name, row in provenance.items():
        if sha(SOURCES / name) != row["sha256"]:
            raise ValueError(f"上游源码哈希不符：{name}")
    with tempfile.TemporaryDirectory(prefix="better-dsh-high-check-") as tmp:
        for case in cases:
            workspace = Path(tmp) / case
            prepare(case, workspace)
            assert not grade(case, workspace)[0], (case, "旧实现应失败")
            write_reference(case, workspace)
            passed, detail = grade(case, workspace)
            assert passed, (case, detail)
    return provenance


def aggregate(batch, order):
    records = []
    for case, repeat in order:
        directory = batch / f"{case}-high-{repeat}"
        path = directory / "result.json"
        if path.exists():
            records.append(json.loads(path.read_text()))
        elif (directory / "runner-error.json").exists():
            records.append({"case": case, "repeat": repeat, "outcome": "runner_error", "route_ok": False})
    scored = [r for r in records if r["route_ok"] and r["outcome"] == "completed"]
    metered = [r for r in records if r["route_ok"] and r.get("normalized_peak_usd") is not None]
    passed = sum(r["passed"] for r in scored)
    observed_cost = sum(r["normalized_peak_usd"] for r in metered)
    return {
        "planned": len(order), "recorded": len(records), "graded": len(scored), "passed": passed,
        "failed_grading": len(scored) - passed,
        "outcomes": dict(Counter(r["outcome"] for r in records)),
        "cost_coverage": len(metered),
        "known_estimated_usd": round(observed_cost, 10),
        "usd_per_success": round(observed_cost / passed, 10)
        if passed and len(metered) == len(records) == len(order) else None,
        "median_elapsed_s": statistics.median(r["elapsed_s"] for r in scored) if scored else None,
        "tokens": {
            key: sum(r["tokens"][key] for r in metered)
            if metered and all(r["tokens"][key] is not None for r in metered) else None
            for key in BUCKETS
        },
        "by_case": {
            case: {"planned": REPEATS, "graded": sum(r["case"] == case for r in scored),
                   "passed": sum(r["case"] == case and r["passed"] for r in scored)}
            for case in dict.fromkeys(case for case, _ in order)
        },
    }


def run_case(batch, case, repeat, key, cancel):
    if cancel.is_set():
        return
    active = batch / "active" / case
    home = active / "dsh-home"
    home.mkdir(parents=True, exist_ok=True)
    env = dsh_env(home)
    env["DEEPSEEK_API_KEY"] = key
    label = f"{case}-high-{repeat}"
    try:
        row = run_task(DEFAULT_DSH, batch, home, env, case, "high", repeat, TIMEOUT,
                       workspace_root=active, cancel=cancel)
    except (OSError, ValueError, KeyError, TypeError) as error:
        path = batch / label
        path.mkdir(exist_ok=True)
        write_json(path / "runner-error.json", {"error": str(error)})
        print(f"  {label} 运行器异常，已保存诊断。", flush=True)
        return
    print(f"  {label} 通过={row['passed']} 档位核对={row['route_ok']} "
          f"状态={row['outcome']} 估算费用={row['normalized_peak_usd']}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只做离线样本、参考实现和 DSH 预检")
    args = parser.parse_args()
    cases = make_cases()
    assert not set(REPAIRS).intersection(cases)
    REPAIRS.update(cases)
    provenance = validate(cases)
    version = preflight(DEFAULT_DSH)
    print(f"{len(cases)} 道题均已确认旧实现失败、参考实现通过。DSH {version}。", flush=True)
    if args.check:
        return 0
    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API Key（输入不显示）: ")
    if not key:
        parser.error("缺少 API Key。")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (ROOT / "runs").mkdir(exist_ok=True)
    batch = Path(tempfile.mkdtemp(prefix=f"{stamp}-high-expanded-", dir=ROOT / "runs"))
    rng = random.Random(ORDER_SEED)
    rounds = [rng.sample(list(cases), len(cases)) for _ in range(REPEATS)]
    order = [(case, repeat) for repeat, group in enumerate(rounds, 1) for case in group]
    write_json(batch / "manifest.json", {
        "suite": "high-expanded", "dsh_version": version, "provider": ROUTE[0], "model": ROUTE[1],
        "effort": "high", "parallelism": PARALLELISM, "timeout_s": TIMEOUT, "order_seed": ORDER_SEED,
        "schedule": order, "cases": {case: {"category": task.category, "title": ORIGINS[case][0],
                                          "upstream": ORIGINS[case][1]} for case, task in cases.items()},
        "normalized_peak_usd_per_million": PRICES, "upstream_sources": provenance,
        "input_sha256": {name: sha(ROOT / name) for name in
                         ("high_bench.py", "high_cases.py", "bench.py", "tasks.py",
                          "base.patch.yml", "high.patch.yml")},
    })
    print(f"结果目录：{batch}", flush=True)
    started = time.monotonic()
    cancel = Event()
    pool = ThreadPoolExecutor(max_workers=PARALLELISM)
    try:
        for repeat, group in enumerate(rounds, 1):
            print(f"第 {repeat}/{REPEATS} 轮（最多 {PARALLELISM} 个并发会话）", flush=True)
            futures = [pool.submit(run_case, batch, case, repeat, key, cancel) for case in group]
            for future in as_completed(futures):
                future.result()
                write_json(batch / "summary.json", aggregate(batch, order))
            if repeat == 1 and not aggregate(batch, order)["graded"]:
                print("首轮无可评分结果，停止后续付费调用。", flush=True)
                break
    except KeyboardInterrupt:
        cancel.set()
        return 130
    finally:
        pool.shutdown(wait=True, cancel_futures=cancel.is_set())
        report = aggregate(batch, order)
        report["batch_elapsed_s"] = round(time.monotonic() - started, 3)
        write_json(batch / "summary.json", report)
    print(f"已保存 {report['recorded']}/{report['planned']} 次，"
          f"独立验收通过 {report['passed']}/{report['graded']}。", flush=True)
    return 0 if report["recorded"] == report["planned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
