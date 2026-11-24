"""Reconcile the relay and DSH logs and report the fixed-high holdout."""

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from statistics import median

from bench import PRICES, ROOT, read_events, write_json


ARMS = ("natural", "checkpoint")
TOKEN_FIELDS = {"inputTokens": "input_tokens", "cacheReadTokens": "cache_read_input_tokens",
                "outputTokens": "output_tokens"}


def load(path):
    return json.loads(path.read_text())


def reconcile(folder):
    result = load(folder / "result.json")
    wire = load(folder / "relay-usage.json")
    _, bad_events = read_events(folder / "events.jsonl")
    session, bad_session = (read_events(folder / result["session_log"])
                            if result["session_log"] else ([], 1))
    successful_responses = [r for r in wire if r["http_status"] == 200 and r["finished"]]
    rows = [e["data"]["usage"] for e in session if e["type"] == "assistant/message"
            and isinstance(e.get("data", {}).get("usage"), dict)]
    wire_tokens = {
        name: sum(r["usage"].get(wire_name, 0) for r in successful_responses)
        for name, wire_name in TOKEN_FIELDS.items()
    }
    session_tokens = {name: sum(r.get(name, 0) for r in rows) for name in TOKEN_FIELDS}
    metered = bool(
        result["route_ok"] and not bad_events and not bad_session
        and not result["blocked_relay_requests"] and wire and len(wire) == len(successful_responses) == len(rows)
        and wire_tokens == session_tokens == result["tokens"]
        and all(r["effort"] == "high" and r["thinking"] == {"type": "enabled"}
                and not r.get("transport_error")
                and r["usage"].get("cache_creation_input_tokens", 0) == 0
                and all(field in r["usage"] for field in ("input_tokens", "output_tokens"))
                for r in successful_responses)
    )
    return {**result, "metered": metered, "session_usage_count": len(rows),
            "confirmed_but_hidden_failed": bool(result["public_passed"] and result["regression_passed"]
                                                and not result["hidden_passed"])}


def arm_metrics(rows):
    verified = [r for r in rows if r["route_ok"]]
    checked = [r for r in verified if r["in_budget"] and r["public_passed"] and r["regression_passed"]]
    metered = [r for r in verified if r["metered"]]
    successes = sum(r["passed"] for r in rows)
    cost = sum(r["estimated_usd"] for r in metered)
    return {
        "runs": len(rows), "verified_high": len(verified), "passed": successes,
        "confirmed_in_budget": len(checked),
        "false_deliveries": sum(r["confirmed_but_hidden_failed"] for r in checked),
        "timeouts_with_passing_code": sum(r["outcome"] == "timeout" and r["hidden_passed"]
                                         and r["public_passed"] and r["regression_passed"] for r in verified),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "median_elapsed_s": median(r["elapsed_s"] for r in verified) if verified else None,
        "metered": len(metered),
        "requests": sum(r["requests_forwarded"] for r in verified),
        "tokens": {name: sum(r["tokens"][name] for r in metered) for name in TOKEN_FIELDS},
        "estimated_usd": round(cost, 10),
        "usd_per_success": round(cost / successes, 10) if successes and len(metered) == len(rows) else None,
    }


def pct(before, after):
    if before is None or after is None or not before:
        return "—"
    change = 100 * (after - before) / before
    return f"{change:+.1f}%"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    batch = args.batch.resolve()
    manifest = load(batch / "manifest.json")
    assert manifest["suite"] == "high-holdout"
    for name, digest in manifest["input_sha256"].items():
        assert sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    runs, pairs = {}, []
    for case, repeat, first, _ in manifest["schedule"]:
        for arm in ARMS:
            label = f"{case}-{repeat}-{arm}"
            if (batch / label / "result.json").is_file():
                runs[label] = reconcile(batch / label)
        a, b = (runs.get(f"{case}-{repeat}-{arm}") for arm in ARMS)
        if a is None or b is None or not (a["route_ok"] and b["route_ok"]):
            continue
        pairs.append({
            "case": case, "repeat": repeat, "first": first,
            "natural_passed": a["passed"], "checkpoint_passed": b["passed"],
            "seconds_saved": round(a["elapsed_s"] - b["elapsed_s"], 3),
            "output_saved": a["tokens"]["outputTokens"] - b["tokens"]["outputTokens"]
            if a["metered"] and b["metered"] else None,
            "estimated_usd_saved": round(a["estimated_usd"] - b["estimated_usd"], 10)
            if a["metered"] and b["metered"] else None,
        })
    metrics = {arm: arm_metrics([r for r in runs.values() if r["arm"] == arm]) for arm in ARMS}
    a, b = (metrics[arm] for arm in ARMS)
    priced_pairs = [r for r in pairs if r["output_saved"] is not None]
    cases = {case: {
        arm: arm_metrics([r for r in runs.values() if r["arm"] == arm and r["case"] == case])
        for arm in ARMS
    } for case in manifest["titles"]}
    data = {"planned": 2 * len(manifest["schedule"]), "recorded": len(runs),
            "by_arm": metrics, "by_case": cases, "pairs": pairs,
            "metering_audit": {k: v["metered"] for k, v in runs.items()}}
    write_json(batch / "holdout-metrics.json", data)
    lines = [
        "# 固定 high 自动收尾留出验证", "",
        f"8 道全新独立任务，每题两种策略各重复 3 次，归档 {len(runs)}/{data['planned']} 次。"
        "两组使用相同的 DSH 版本、模型、提示、隔离、公开检查和 240 秒上限。"
        "自动收尾沿用上一轮规则，两套公开检查通过后拦住下一次模型请求。"
        "最终判定使用未向模型开放的隐藏用例。"
        "[运行配置](manifest.json) · [用量复核与配对结果](holdout-metrics.json)", "",
        "| 指标 | 常规 high | 自动收尾 high | 变化 |", "|---|---:|---:|---:|",
        f"| 按时通过 | {a['passed']}/{a['runs']} | {b['passed']}/{b['runs']} | {b['passed']-a['passed']:+d} 次 |",
        f"| 公开通过后隐藏失败 | {a['false_deliveries']}/{a['confirmed_in_budget']} | "
        f"{b['false_deliveries']}/{b['confirmed_in_budget']} | — |",
        f"| 耗时中位数 | {display(a['median_elapsed_s'], 2)} 秒 | {display(b['median_elapsed_s'], 2)} 秒 | "
        f"{pct(a['median_elapsed_s'], b['median_elapsed_s'])} |",
        f"| 模型请求数 | {a['requests']} | {b['requests']} | {pct(a['requests'], b['requests'])} |",
        f"| 交叉核对完整用量 | {a['metered']}/{a['runs']} | {b['metered']}/{b['runs']} | — |",
        f"| 已核对输出 tokens | {a['tokens']['outputTokens']:,} | {b['tokens']['outputTokens']:,} | "
        f"{pct(a['tokens']['outputTokens'], b['tokens']['outputTokens']) if all(x['metered']==x['runs'] for x in (a,b)) else '—'} |",
        f"| 已核对估算费用 | ${a['estimated_usd']:.5f} | ${b['estimated_usd']:.5f} | — |",
        f"| 每次成功估算费用 | {display(a['usd_per_success'], 5)} | {display(b['usd_per_success'], 5)} | "
        f"{pct(a['usd_per_success'], b['usd_per_success'])} |", "",
        f"{len(pairs)} 对有双边档位验证，自动收尾在其中 {sum(r['seconds_saved']>0 for r in pairs)} 对耗时更短。"
        f"{len(priced_pairs)} 对的用量均已交叉核对，自动收尾在其中 "
        f"{sum(r['output_saved']>0 for r in priced_pairs)} 对输出更少。"
        "估算使用项目固定价表。耗时包含超时会话，错解和超时的已核对费用也计入对应组。"
        "独立样本单位是题目，三次复跑用于观察稳定性。", "",
        "## 按题结果", "",
        "| 题目 | 常规按时通过 | 自动收尾按时通过 | 自动收尾公开通过后隐藏失败 |",
        "|---|---:|---:|---:|",
    ]
    for case, title in manifest["titles"].items():
        n, c = (cases[case][arm] for arm in ARMS)
        lines.append(f"| {case} {title} | {n['passed']}/{n['runs']} | {c['passed']}/{c['runs']} | "
                     f"{c['false_deliveries']}/{c['confirmed_in_budget']} |")
    lines.extend(["", "## 逐次记录", "",
                  "| 运行 | 状态 | 按时通过 | 隐藏通过 | 耗时（秒） | 输出 tokens | 已核对费用（美元） |",
                  "|---|---|---|---|---:|---:|---:|"])
    for label, r in sorted(runs.items()):
        tokens = f"{r['tokens']['outputTokens']:,}" if r["metered"] else "—"
        cost = display(r["estimated_usd"], 6) if r["metered"] else "—"
        lines.append(f"| [{label}]({label}/result.json) | {r['outcome']} | "
                     f"{'是' if r['passed'] else '否'} | {'是' if r['hidden_passed'] else '否'} | "
                     f"{r['elapsed_s']:.2f} | {tokens} | {cost} |")
    (batch / "analysis.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: data[k] for k in ("planned", "recorded", "by_arm")}, ensure_ascii=False, indent=2))


def display(number, digits):
    return "—" if number is None else f"{number:.{digits}f}"


if __name__ == "__main__":
    main()
