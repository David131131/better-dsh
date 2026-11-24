"""Compare fixed efforts with an independently gated high-effort repair."""

import argparse
from datetime import datetime, timezone
import getpass
from hashlib import sha256
import os
from pathlib import Path
import statistics
import tempfile
import time

import adaptive_cases
import holdout_cases
from bench import BUCKETS, DEFAULT_DSH, PRICES, ROOT, ROUTE, dsh_env, preflight, run_task, write_json
from tasks import REPAIRS, check_python, grade, prepare, write_reference


def run_gate(rules, workspace):
    started = time.monotonic()
    checks = []
    for rule, code in rules:
        passed, detail = check_python(code, workspace)
        checks.append({"rule": rule, "passed": passed, "detail": detail})
    return {"passed": all(row["passed"] for row in checks), "checks": checks,
            "elapsed_s": round(time.monotonic() - started, 3)}


def validate(cases, gates):
    with tempfile.TemporaryDirectory(prefix="better-dsh-adaptive-check-") as tmp:
        for case in cases:
            workspace = Path(tmp) / case
            prepare(case, workspace)
            assert not grade(case, workspace)[0] and not run_gate(gates[case], workspace)["passed"], case
            write_reference(case, workspace)
            passed, detail = grade(case, workspace)
            assert passed, (case, detail)
            assert run_gate(gates[case], workspace)["passed"], case


def combine(low, gate, repair):
    attempts = [low, repair] if repair else [low]
    return {
        "passed": attempts[-1]["passed"],
        "elapsed_s": round(sum(r["elapsed_s"] for r in attempts) + gate["elapsed_s"], 3),
        "normalized_peak_usd": sum(r["normalized_peak_usd"] for r in attempts),
        "tokens": {
            key: sum(r["tokens"][key] for r in attempts)
            if all(r["tokens"][key] is not None for r in attempts) else None
            for key in BUCKETS
        },
    }


def summarize(pairs):
    summary = {}
    for policy in ("high", "low", "adaptive"):
        rows = [p[policy] for p in pairs]
        costs = sum(r["normalized_peak_usd"] for r in rows)
        passed = sum(r["passed"] for r in rows)
        summary[policy] = {
            "runs": len(rows), "passed": passed, "total_estimated_usd": costs,
            "usd_per_success": costs / passed if passed else None,
            "median_elapsed_s": statistics.median(r["elapsed_s"] for r in rows),
            "tokens": {
                key: sum(r["tokens"][key] for r in rows) if all(r["tokens"][key] is not None for r in rows) else None
                for key in BUCKETS
            },
        }
    summary["gate"] = {
        "triggered": sum(not p["gate"]["passed"] for p in pairs),
        "recovered": sum(not p["low"]["passed"] and p["adaptive"]["passed"] for p in pairs),
        "missed_low_failures": sum(p["gate"]["passed"] and not p["low"]["passed"] for p in pairs),
        "escalated_low_passes": sum(not p["gate"]["passed"] and p["low"]["passed"] for p in pairs),
        "regressions": sum(p["low"]["passed"] and not p["adaptive"]["passed"] for p in pairs),
    }
    return summary


def verify(row, label):
    if row["outcome"] != "completed" or not row["route_ok"] or row["normalized_peak_usd"] is None:
        raise SystemExit(f"运行数据不完整，批次停止：{label}")
    print(f"  通过={row['passed']}，总 token={row['tokens']['totalTokens']}，"
          f"估算费用={row['normalized_peak_usd']:.6f}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只做本地样本、门控检查和 DSH 预检")
    parser.add_argument("--suite", choices=("adaptive", "holdout"), default="adaptive")
    args = parser.parse_args()
    suite = holdout_cases if args.suite == "holdout" else adaptive_cases
    cases, gates = suite.CASES, suite.GATES
    timeout = 240 if args.suite == "holdout" else 180
    assert not set(REPAIRS).intersection(cases)
    REPAIRS.update(cases)
    validate(cases, gates)
    version = preflight(DEFAULT_DSH)
    print(f"{len(cases)} 道新题、两套独立评分检查和 DSH {version} 均已验证。", flush=True)
    if args.check:
        return

    key = os.environ.get("DEEPSEEK_API_KEY") or getpass.getpass("DeepSeek API Key（输入不显示）: ")
    if not key:
        parser.error("缺少 DeepSeek API Key。")
    root = ROOT / "runs"
    root.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch = Path(tempfile.mkdtemp(prefix=f"{stamp}-{args.suite}-", dir=root))
    home = batch / "dsh-home"
    home.mkdir()
    env = dsh_env(home)
    env["DEEPSEEK_API_KEY"] = key
    order = [(case, repeat, ("high", "low") if (index + repeat) % 2 else ("low", "high"))
             for repeat in (1, 2) for index, case in enumerate(cases)]
    write_json(batch / "manifest.json", {
        "suite": args.suite, "dsh_version": version, "provider": ROUTE[0], "model": ROUTE[1],
        "schedule": order, "timeout_s": timeout, "normalized_peak_usd_per_million": PRICES,
        "adaptive_policy": "reuse fixed-low first attempt; if independent gate fails, high in a new session with the low worktree and failed rules",
        "input_sha256": {name: sha256((ROOT / name).read_bytes()).hexdigest() for name in
                         ("adaptive_bench.py", f"{suite.__name__}.py", "bench.py", "tasks.py",
                          "base.patch.yml", "high.patch.yml", "low.patch.yml")},
    })
    print(f"结果目录：{batch}", flush=True)
    pairs = []
    for case, repeat, efforts in order:
        initial = {}
        for effort in efforts:
            label = f"{case}-{effort}-{repeat}"
            initial[effort] = run_task(DEFAULT_DSH, batch, home, env, case, effort, repeat, timeout)
            verify(initial[effort], label)
        seed = batch / f"{case}-low-{repeat}" / "workspace"
        gate = run_gate(gates[case], seed)
        write_json(seed.parent / "gate.json", gate)
        repair = None
        if not gate["passed"]:
            diagnostic = "；".join(c["rule"] for c in gate["checks"] if not c["passed"])
            print(f"  门控触发：{diagnostic}", flush=True)
            repair = run_task(DEFAULT_DSH, batch, home, env, case, "high", repeat, timeout,
                              repair=(seed, diagnostic))
            verify(repair, f"{case}-repair-{repeat}")
        pair = {"case": case, "repeat": repeat, **initial, "gate": gate, "repair": repair,
                "adaptive": combine(initial["low"], gate, repair)}
        pairs.append(pair)
        write_json(batch / f"{case}-pair-{repeat}.json", pair)
        write_json(batch / "summary.json", summarize(pairs))
    print(f"已完成 {len(pairs)} 对照组。", flush=True)


if __name__ == "__main__":
    main()
