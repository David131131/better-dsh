"""Report the same-trajectory comparison of safe-stop policies."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from paired_isolation import check_code
from safe_stop_bench import MODES, snapshot
from safe_stop_cases import CASES


def middle(values):
    return median(values) if values else None


def stats(rows, mode):
    selected = [r["policies"][mode] for r in rows if r["high_verified"]]
    stops = [r for r in selected if r["checkpoint"]]
    metered = [r for r in selected if r["metered"]]
    success = sum(r["passed"] for r in selected)
    expense = sum(r["estimated_usd"] for r in metered)
    return {
        "scored": len(selected), "passed": success, "stops": len(stops),
        "wrong_stops": sum(not r["snapshot_passed"] for r in stops),
        "fallback": len(selected) - len(stops), "metered": len(metered),
        "output_tokens": sum(r["tokens"]["outputTokens"] for r in metered),
        "estimated_usd": round(expense, 10),
        "usd_per_success": round(expense / success, 10) if success and len(metered) == len(selected) else None,
        "median_seconds": middle([r["elapsed_s"] for r in selected]),
        "requests": sum(r["requests"] for r in selected),
    }


def show(value, decimals=3):
    return "—" if value is None else f"{value:,.{decimals}f}"


def regrade_streams(batch, rows):
    """Grade the documented stream contract and preserve the original scores."""
    case = CASES["S03"]
    assertion = "assert len(actual)==len(expected) and all(a is b for a,b in zip(actual,expected))"
    if case.hidden.count(assertion) != 1:
        raise ValueError("S03 的原始断言已变更")
    corrected = case.hidden.replace(assertion, "assert actual == expected")
    audit = []
    with TemporaryDirectory(prefix="dsh-safe-stop-regrade-") as directory:
        root = Path(directory).resolve()
        work, scratch = root / "workspace", root / "scratch"
        work.mkdir()
        scratch.mkdir()
        source = work / "ledger.py"
        for contents, expected in ((case.incomplete, False), (case.reference, True)):
            source.write_text(contents)
            ok, detail = check_code(corrected, work, scratch)
            if ok != expected or detail.startswith("sandbox-exec:"):
                raise ValueError(f"S03 修正后的对照验证失败：{detail}")
        for row in rows:
            if row["case"] != "S03":
                continue
            label = f"S03-{row['repeat']}"
            folder = batch / label
            for mode, policy in row["policies"].items():
                if "hidden" not in policy["checks"]:
                    continue
                contents = ((folder / f"{mode}.py").read_bytes() if policy["checkpoint"]
                            else snapshot(folder / "workspace"))
                if contents is None:
                    raise ValueError(f"缺少 {label} {mode} 的原始快照")
                source.write_bytes(contents)
                before = policy["checks"]["hidden"]
                after = check_code(corrected, work, scratch)
                if after[1].startswith("sandbox-exec:"):
                    raise RuntimeError(after[1])
                audit.append({"run": label, "policy": mode, "before": before, "after": after})
                policy["checks"]["hidden"] = after
                policy["snapshot_passed"] = all(ok for ok, _ in policy["checks"].values())
                policy["passed"] = bool(row["high_verified"] and policy["snapshot_passed"]
                                        and (policy["checkpoint"] or row["outcome"] == "completed"))
    (batch / "grading-correction.json").write_text(json.dumps({
        "case": "S03", "reason": "题面只约定元组的值和顺序。原断言额外要求同一元组对象。",
        "original_assertion": assertion, "corrected_assertion": "assert actual == expected",
        "regraded": audit,
    }, ensure_ascii=False, indent=2) + "\n")
    return audit


def main(batch):
    manifest = json.loads((batch / "manifest.json").read_text())
    rows = [json.loads(path.read_text()) for label, repeat in manifest["order"]
            if (path := batch / f"{label}-{repeat}" / "result.json").is_file()]
    raw_arms = {mode: stats(rows, mode) for mode in MODES}
    corrections = regrade_streams(batch, rows)
    arms = {mode: stats(rows, mode) for mode in MODES}
    by_case = {key: {mode: stats([r for r in rows if r["case"] == key], mode) for mode in MODES}
               for key in manifest["titles"]}
    verified = [r for r in rows if r["high_verified"]]
    pairs = []
    for r in verified:
        a, b, n = (r["policies"][k] for k in MODES)
        pairs.append({
            "case": r["case"], "repeat": r["repeat"],
            "extra_requests": b["requests"] - a["requests"],
            "requests_saved_vs_natural": n["requests"] - b["requests"],
            "extra_tokens": b["tokens"]["outputTokens"] - a["tokens"]["outputTokens"]
            if a["metered"] and b["metered"] else None,
            "tokens_saved_vs_natural": n["tokens"]["outputTokens"] - b["tokens"]["outputTokens"]
            if b["metered"] and n["metered"] else None,
            "natural_tokens_matched": n["tokens"]["outputTokens"] if b["metered"] and n["metered"] else None,
            "avoided_wrong_stop": a["checkpoint"] and not a["snapshot_passed"]
            and not (b["checkpoint"] and not b["snapshot_passed"]),
            "recovered_delivery": not a["passed"] and b["passed"],
            "lost_delivery": a["passed"] and not b["passed"],
            "same_boundary": a["checkpoint"] and b["checkpoint"] and a["requests"] == b["requests"],
        })
    expense = sum(r["policies"]["natural"]["visible_estimated_usd"] for r in rows)
    summary = {"planned": len(manifest["order"]), "recorded": len(rows), "verified_high": len(verified),
               "outcomes": dict(Counter(r["outcome"] for r in rows)), "arms": arms,
               "raw_arms": raw_arms, "pairs": pairs, "by_case": by_case,
               "observed_api_cost_usd": round(expense, 10)}
    (batch / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    a, b, n = (arms[mode] for mode in MODES)
    metered = [p for p in pairs if p["tokens_saved_vs_natural"] is not None]
    extra_metered = [p for p in pairs if p["extra_tokens"] is not None]
    natural_tokens = sum(p["natural_tokens_matched"] for p in metered)
    saved_tokens = sum(p["tokens_saved_vs_natural"] for p in metered)
    percent_saved = f"（{saved_tokens / natural_tokens:.1%}）" if natural_tokens else ""
    boundary_times = [c["added_seconds"] for r in verified
                      for c in json.loads((batch / f"{r['case']}-{r['repeat']}" / "checkpoints.json").read_text())
                      if "added_seconds" in c]
    changed_grades = sum(g["before"][0] != g["after"][0] for g in corrections)
    lines = [
        "# 固定 high 安全收尾对照", "",
        f"8 道自编任务，每题三次，共归档 {len(rows)}/{len(manifest['order'])} 条轨迹。"
        "所有会话自然运行，在模型下一次请求前分别记录现有检查与补强检查首次通过的位置。"
        "只给模型定向和常规回归用例。补强检查和独立隐藏验收位于模型不可读目录。"
        "两个观察点与最终代码分别验收。策略的请求、用量和时间来自同一轨迹的前缀。"
        "[配置和文件哈希](manifest.json) · [逐次及分题指标](metrics.json)", "",
        "离线预检为每道题验证一个不完整补丁，确认它通过两项原有检查，但会被补强检查和隐藏验收拦住。"
        "8 道独立题用于测量覆盖缺口，三次复跑用于衡量同题波动。", "",
        f"S03 的原始隐藏用例额外要求返回同一个元组对象。按题面改为比较值及顺序后，"
        f"重新验收了 {len(corrections)} 份快照，其中 {changed_grades} 份隐藏判分改变。"
        "逐次原始记录及模型轨迹保持原样。[判分修正和逐项对照](grading-correction.json)", "",
        "| 指标 | 现有规则 | 加一项边界回归 | 自然结束 |", "|---|---:|---:|---:|",
        f"| 触发收尾 | {a['stops']} | {b['stops']} | — |",
        f"| 触发时隐藏验收失败 | {a['wrong_stops']}/{a['stops']} | {b['wrong_stops']}/{b['stops']} | — |",
        f"| 交付通过，含未触发时正常结束 | {a['passed']}/{len(verified)} | {b['passed']}/{len(verified)} | {n['passed']}/{len(verified)} |",
        f"| 模型请求 | {a['requests']} | {b['requests']} | {n['requests']} |",
        f"| 用量完整 | {a['metered']}/{len(verified)} | {b['metered']}/{len(verified)} | {n['metered']}/{len(verified)} |",
        f"| 已核对输出 tokens | {a['output_tokens']:,} | {b['output_tokens']:,} | {n['output_tokens']:,} |",
        f"| 每次交付成功估算费用（美元） | {show(a['usd_per_success'],6)} | {show(b['usd_per_success'],6)} | {show(n['usd_per_success'],6)} |",
        f"| 从启动到收尾点的中位秒数 | {show(a['median_seconds'])} | {show(b['median_seconds'])} | {show(n['median_seconds'])} |",
        "",
        f"- 加一项检查避免了 {sum(p['avoided_wrong_stop'] for p in pairs)} 次已观察到的错误收尾。"
        f"其中交付通过数增加 {sum(p['recovered_delivery'] for p in pairs)} 次，减少 {sum(p['lost_delivery'] for p in pairs)} 次。"
        f"两种规则在 {sum(p['same_boundary'] for p in pairs)} 次的同一个请求边界触发。",
        f"- 在 {len(extra_metered)} 个有双边完整计量的配对中，补强相对现有规则多用 "
        f"{sum(p['extra_tokens'] for p in extra_metered):,} 输出 tokens。"
        f"与自然结束比较，{len(metered)} 个可比配对共少用 {saved_tokens:,} 输出 tokens{percent_saved}。",
        f"- 补强检查执行了 {len(boundary_times)} 次，单次中位耗时 {show(middle(boundary_times))} 秒。",
        f"- 完整返回请求的可见 API 费用估算为 {expense:.5f} 美元，使用项目固定价表。"
        "收尾点耗时包含实际运行的宿主检查，观察记录未直接执行停止，也不计算最终说明的质量。",
    ]
    replay_file = batch / "repo-boundary-replay.json"
    if replay_file.is_file():
        replay = json.loads(replay_file.read_text())
        runs = replay["runs"]
        early_failures = [r for r in runs if not r["previous_expanded_grade"]["checkpoint_pass"]]
        blocked = [r for r in early_failures if not r["early_check"][0]]
        early_passes = [r for r in runs if r["previous_expanded_grade"]["checkpoint_pass"]]
        false_rejections = sum(not r["early_check"][0] for r in early_passes)
        repaired = sum(r["final_check"][0] for r in blocked)
        lines.extend([
            "", "## 真实仓库快照离线复验", "",
            f"上一轮 humanize 和 packaging 的 {len(runs)} 个早停快照都通过了原先的两项检查。"
            f"按已有的扩展验收，{len(early_failures)} 个存在边界缺陷。每题增加一项有针对性的检查后，"
            f"拦住 {len(blocked)}/{len(early_failures)} 个有缺陷的快照，误拦 {false_rejections}/{len(early_passes)} 个通过的快照。"
            f"继续自然运行后，{repaired}/{len(blocked)} 个被拦案例通过新增检查。"
            "这些用例根据已知缺陷选定，结论限于归档快照，费用与用量不计入上表。"
            "[用例、对照和逐次复验](repo-boundary-replay.json)", "",
            "拦下的缺陷涉及跨时区后跨年、版本本地标签接受尾部换行，以及多约束过滤时的重复项与对象保留。",
        ])
    lines.extend([
        "", "## 分题", "",
        "| 任务 | 现有规则误收尾 | 补强规则误收尾 | 自然结束交付通过 |",
        "|---|---:|---:|---:|",
    ])
    for key, title in manifest["titles"].items():
        c = by_case[key]
        lines.append(f"| {key} {title} | {c['original']['wrong_stops']}/{c['original']['stops']} | "
                     f"{c['strengthened']['wrong_stops']}/{c['strengthened']['stops']} | "
                     f"{c['natural']['passed']}/{c['natural']['scored']} |")
    lines.extend(["", "## 逐次记录", "",
                  "表中使用修正后的验收。每条记录链接指向实验结束时保存的原始文件。", "",
                  "| 会话 | 现有规则 | 补强规则 | 自然结束 | 自然会话状态 |",
                  "|---|---|---|---|---|"])
    for row in sorted(rows, key=lambda r: (r["case"],r["repeat"])):
        label = f"{row['case']}-{row['repeat']}"
        statuses = [("通过" if p["passed"] else "失败") + ("（触发）" if p["checkpoint"] else "")
                    for p in (row["policies"][k] for k in MODES)]
        lines.append(f"| [{label}]({label}/result.json) | {' | '.join(statuses)} | {row['outcome']} |")
    (batch / "analysis.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k,v in summary.items() if k not in ("by_case", "pairs", "raw_arms")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    main(parser.parse_args().batch)
