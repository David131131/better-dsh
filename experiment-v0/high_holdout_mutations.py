"""Probe whether the holdout's hidden checks catch plausible broken implementations."""

import argparse
from pathlib import Path
import tempfile

from bench import write_json
from high_holdout_cases import CASES
from paired_bench import fixture
from paired_cases import clean
from paired_isolation import check_code


MUTATIONS = {
    "Q01": ("limiter.py", "吞掉正在运行任务的取消信号", "return await factory()",
            """try:
                return await factory()
            except asyncio.CancelledError:
                return None"""),
    "Q02": ("leases.py", "确认后允许以同一 ID 重新提交",
            "if id in self.rows or id in self.done:", "if id in self.rows:"),
    "Q03": ("overlay.py", "把挖空记录当作不存在",
            "if a == b: continue", "if a == b or v is None: continue"),
    "Q04": ("records.py", "关闭后重新接收输入",
            "if self.bad or self.closed: raise ValueError('reader unavailable')",
            "if self.bad: raise ValueError('reader unavailable')"),
    "Q05": ("inventory.py", "同版本用后到事件覆盖",
            "rev > self.rows[key][0]", "rev >= self.rows[key][0]"),
    "Q06": ("config.py", "列表只做浅拷贝",
            "if isinstance(value, list): return [_copy(v) for v in value]",
            "if isinstance(value, list): return list(value)"),
    "Q07": ("allocation.py", "同分时保留字典序较大的 ID",
            "if key not in new or candidate < new[key]: new[key]=candidate",
            """if (key not in new or candidate[:2] < new[key][:2]
                    or (candidate[:2] == new[key][:2] and candidate[2] > new[key][2])):
                    new[key] = candidate"""),
    "Q08": ("rebuild.py", "错误地拒绝无关的依赖环",
            "def affected(self,changed):",
            """def affected(self,changed):
        from graphlib import TopologicalSorter
        tuple(TopologicalSorter(self.rows).static_order())"""),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    args = parser.parse_args()
    rows = []
    with tempfile.TemporaryDirectory(prefix="better-dsh-holdout-mutations-") as tmp:
        root = Path(tmp).resolve()
        scratch = root / "scratch"
        scratch.mkdir()
        for case_id, (name, label, before, after) in MUTATIONS.items():
            case = CASES[case_id]
            workspace = root / case_id
            workspace.mkdir()
            fixture(workspace, case.files)
            fixture(workspace, case.fixed)
            path = workspace / name
            source = path.read_text()
            assert before in source, (case_id, before)
            path.write_text(source.replace(before, after, 1))
            checks = {k: check_code(clean(getattr(case, k)), workspace, scratch)
                      for k in ("public", "regression", "hidden")}
            rows.append({"case": case_id, "mutation": label, "checks": checks})
    write_json(args.batch / "mutation-audit.json", rows)
    print(f"checked {len(rows)} deliberately broken variants")
    for row in rows:
        print(row["case"], *(f"{k}={v[0]}" for k, v in row["checks"].items()))


if __name__ == "__main__":
    main()
