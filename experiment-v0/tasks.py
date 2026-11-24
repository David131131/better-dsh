"""Local fixtures and independent checks for the nine pilot tasks."""

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent


@dataclass(frozen=True)
class Repair:
    category: str
    prompt: str
    files: dict[str, str]
    check: str
    reference: dict[str, str]


REPAIRS = {
    "L01": Repair(
        "local",
        "calendar_rules.py 把 1900 年判为闰年。请修复，保留函数签名。"
        "复现：python3 -c 'from calendar_rules import leap; print(leap(1900))'",
        {"calendar_rules.py": "def leap(year):\n    return year % 4 == 0\n"},
        "from calendar_rules import leap\n"
        "assert [leap(y) for y in (1899, 1900, 1996, 2000, 2001, 2100)] "
        "== [False, False, True, True, False, False]\n",
        {"calendar_rules.py": "def leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"},
    ),
    "L02": Repair(
        "local",
        "pagination.py 的分页每页多返回一项。修复 page，page_no 从 1 开始，"
        "越过末页返回空列表。复现：python3 -c 'from pagination import page; print(page([1,2,3], 1, 2))'",
        {"pagination.py": "def page(items, page_no, size):\n    start = (page_no - 1) * size\n    return items[start:start + size + 1]\n"},
        "from pagination import page\n"
        "assert page([1, 2, 3], 1, 2) == [1, 2]\n"
        "assert page([1, 2, 3], 2, 2) == [3]\n"
        "assert page([1, 2, 3], 3, 2) == []\n"
        "assert page([], 1, 2) == []\n",
        {"pagination.py": "def page(items, page_no, size):\n    start = (page_no - 1) * size\n    return items[start:start + size]\n"},
    ),
    "L03": Repair(
        "local",
        "limits.py 的 retry_count 把显式 0 当成未设置。只在参数为 None 时使用默认值。"
        "复现：python3 -c 'from limits import retry_count; print(retry_count(0, 3))'",
        {"limits.py": "def retry_count(value, default=3):\n    return value or default\n"},
        "from limits import retry_count\n"
        "assert retry_count(0) == 0\n"
        "assert retry_count(None) == 3\n"
        "assert retry_count(None, 5) == 5\n"
        "assert retry_count(2) == 2\n",
        {"limits.py": "def retry_count(value, default=3):\n    return default if value is None else value\n"},
    ),
    "X01": Repair(
        "cross-file",
        "CLI 的配置优先级有误。规则是显式 --level 覆盖 APP_LEVEL，"
        "APP_LEVEL 覆盖 settings.json，三者都缺省时是 info。"
        "保持现有 CLI 入口和输出。复现：APP_LEVEL=warn python3 cli.py --level debug",
        {
            "settings.json": '{"level": "error"}\n',
            "settings.py": "import json\nfrom pathlib import Path\n\ndef configured_level(env, filename='settings.json'):\n"
            "    path = Path(filename)\n    config = json.loads(path.read_text()) if path.exists() else {}\n"
            "    return config.get('level') or env.get('APP_LEVEL') or 'info'\n",
            "cli.py": "import argparse\nimport os\nfrom settings import configured_level\n\n"
            "if __name__ == '__main__':\n    parser = argparse.ArgumentParser()\n    parser.add_argument('--level')\n"
            "    args = parser.parse_args()\n    print(configured_level(os.environ) or args.level)\n",
        },
        "import os, subprocess, sys\nfrom settings import configured_level\n"
        "def run(env_level, *args):\n"
        "    env = dict(os.environ)\n    env.pop('APP_LEVEL', None)\n"
        "    if env_level is not None:\n        env['APP_LEVEL'] = env_level\n"
        "    return subprocess.check_output([sys.executable, '-B', 'cli.py', *args], env=env, text=True).strip()\n"
        "assert run('warn', '--level', 'debug') == 'debug'\n"
        "assert run('warn') == 'warn'\nassert run(None) == 'error'\n"
        "assert run(None, '--level', 'debug') == 'debug'\n"
        "assert configured_level({}, 'missing.json') == 'info'\n",
        {
            "settings.py": "import json\nfrom pathlib import Path\n\ndef configured_level(env, filename='settings.json'):\n"
            "    path = Path(filename)\n    config = json.loads(path.read_text()) if path.exists() else {}\n"
            "    return env.get('APP_LEVEL') or config.get('level') or 'info'\n",
            "cli.py": "import argparse\nimport os\nfrom settings import configured_level\n\n"
            "if __name__ == '__main__':\n    parser = argparse.ArgumentParser()\n    parser.add_argument('--level')\n"
            "    args = parser.parse_args()\n    print(args.level or configured_level(os.environ))\n",
        },
    ),
    "X02": Repair(
        "cross-file",
        "profile_api.save 与 load 往返时，nickname=None 变成了空字符串。"
        "保持字段省略与显式 null 的区别，空字符串和普通字符串也要保留。"
        "复现：python3 -c 'from profile_api import save,load; print(load(save(dict(nickname=None))))'",
        {
            "profile_api.py": "import json\nfrom profile_codec import encode, decode\n\n"
            "def save(profile):\n    return json.dumps(encode(profile))\n\n"
            "def load(text):\n    return decode(json.loads(text))\n",
            "profile_codec.py": "def encode(profile):\n    return {key: value for key, value in profile.items() if value is not None}\n\n"
            "def decode(data):\n    return {**data, 'nickname': data.get('nickname') or ''}\n",
        },
        "from profile_api import save, load\nimport json\n"
        "for p in ({}, {'nickname': None}, {'nickname': ''}, {'nickname': 'Lee'}, {'name': 'Lee'}):\n"
        "    assert json.loads(save(p)) == p\n    assert load(save(p)) == p\n"
        "assert load('{\"nickname\":null}') == {'nickname': None}\n",
        {"profile_codec.py": "def encode(profile):\n    return dict(profile)\n\ndef decode(data):\n    return dict(data)\n"},
    ),
    "X03": Repair(
        "cross-file",
        "仓库的 SKU 查询应忽略两侧空白和大小写。旧数据里的键可能也有空白或大小写混用。"
        "找不到时仍返回 None，接口签名保持不变。"
        "复现：python3 -c 'from inventory import lookup; print(lookup({\" Ab-7 \": 4}, \"AB-7\"))'",
        {
            "sku.py": "def normalize(sku):\n    return sku.lower()\n",
            "inventory.py": "from sku import normalize\n\ndef lookup(stock, sku):\n    return stock.get(normalize(sku))\n",
        },
        "from inventory import lookup\nfrom sku import normalize\n"
        "assert normalize(' Ab-7 ') == 'ab-7'\n"
        "assert lookup({' Ab-7 ': 4}, 'AB-7') == 4\n"
        "assert lookup({'ab-7': 4, 'AB-8': 0}, ' ab-8 ') == 0\n"
        "assert lookup({'ab-7': 4}, 'missing') is None\n",
        {
            "sku.py": "def normalize(sku):\n    return sku.strip().lower()\n",
            "inventory.py": "from sku import normalize\n\ndef lookup(stock, sku):\n"
            "    target = normalize(sku)\n    return next((v for k, v in stock.items() if normalize(k) == target), None)\n",
        },
    ),
}

BASELINE_REPAIR_IDS = tuple(REPAIRS)

REPAIRS.update({
    "H01": Repair(
        "event-replay",
        """库存视图在重放乱序事件时会丢更新，删除后的记录也会被旧消息复活。
公开入口 view.stock(events) 接收 (sku, version, action, quantity) 四元组列表。
每个 SKU 只接受版本严格大于已见最高版本的事件。同版本时以第一次接受的事件为准。
put 设置绝对数量（含 0）；delete 移除该 SKU，但仍要记住版本；更新的 put 可以恢复。
同一个输入允许多次调用，调用间不共享状态。保持公开入口，使用标准库。
复现：python3 -c "from view import stock; print(stock([('A',2,'delete',None),('A',1,'put',8)]))" 应输出 {}。""",
        {
            "store.py": dedent("""\
                class Store:
                    def __init__(self):
                        self.items = {}

                    def apply(self, sku, version, action, quantity):
                        if action == "delete":
                            self.items.pop(sku, None)
                        else:
                            self.items[sku] = quantity
                """),
            "replay.py": dedent("""\
                def replay(store, events):
                    seen_versions = set()
                    for sku, version, action, quantity in events:
                        if version in seen_versions:
                            continue
                        seen_versions.add(version)
                        store.apply(sku, version, action, quantity)
                """),
            "view.py": dedent("""\
                from store import Store
                from replay import replay

                def stock(events):
                    store = Store()
                    replay(store, events)
                    return dict(store.items)
                """),
        },
        dedent("""\
            from view import stock
            assert stock([]) == {}
            assert stock([('A', 4, 'put', 0), ('B', 4, 'put', 9)]) == {'A': 0, 'B': 9}
            assert stock([('A', 4, 'put', 3), ('A', 2, 'put', 12)]) == {'A': 3}
            assert stock([('A', 4, 'put', 3), ('A', 4, 'delete', None)]) == {'A': 3}
            assert stock([('A', 4, 'delete', None), ('A', 4, 'put', 3), ('A', 2, 'put', 12)]) == {}
            events = [('A', 1, 'put', 5), ('B', 5, 'put', 3), ('A', 3, 'delete', None),
                      ('B', 4, 'delete', None), ('A', 2, 'put', 12), ('A', 4, 'put', 0)]
            assert stock(events) == {'A': 0, 'B': 3}
            assert stock([('A', 0, 'put', 1)]) == {'A': 1}
            assert stock(events) == {'A': 0, 'B': 3}
            """),
        {
            "store.py": dedent("""\
                class Store:
                    def __init__(self):
                        self.items = {}
                        self.versions = {}

                    def apply(self, sku, version, action, quantity):
                        if sku in self.versions and version <= self.versions[sku]:
                            return
                        self.versions[sku] = version
                        if action == "delete":
                            self.items.pop(sku, None)
                        else:
                            self.items[sku] = quantity
                """),
            "replay.py": dedent("""\
                def replay(store, events):
                    for event in events:
                        store.apply(*event)
                """),
        },
    ),
    "H02": Repair(
        "nested-config",
        """修复 config.load(defaults, file_config, overrides) 的分层配置。
优先级 defaults < file_config < overrides。两个值都是字典时递归合并；其他情况高优先级的值整体替换。
list 整体替换，None、False、0、空 list 都是合法值。空字典与已有字典合并时保留已有键。
overrides 的键是用点分隔的路径。遇到路径中间已有标量时将该节点替换成字典，按 overrides 的插入顺序逐条应用。
调用不得修改任一输入。返回值后续被调用者修改，也不能影响输入。只使用标准库，保持入口签名。
复现：load({'server': {'port': 80, 'tls': True}}, {'server': {'port': 90}}, {'server.port': 0}) 应保留 tls=True 且 port=0。""",
        {
            "config.py": dedent("""\
                from merge import merge
                from paths import expand

                def load(defaults, file_config, overrides):
                    return merge(merge(defaults, file_config), expand(overrides))
                """),
            "merge.py": dedent("""\
                def merge(base, layer):
                    return {**base, **{key: value for key, value in layer.items() if value}}
                """),
            "paths.py": dedent("""\
                def expand(overrides):
                    return {key.split(".")[-1]: value for key, value in overrides.items()}
                """),
        },
        dedent("""\
            from config import load
            from copy import deepcopy
            defaults = {'server': {'port': 80, 'tls': True, 'hosts': ['a']}, 'labels': {'x': 1}, 'count': 3}
            file_config = {'server': {'port': 90, 'hosts': []}, 'labels': {}, 'count': None}
            overrides = {'server.port': 0, 'server.tls': False, 'labels.y': [1], 'mode.feature.on': True}
            original = deepcopy((defaults, file_config, overrides))
            value = load(defaults, file_config, overrides)
            assert value == {'server': {'port': 0, 'tls': False, 'hosts': []},
                             'labels': {'x': 1, 'y': [1]}, 'count': None,
                             'mode': {'feature': {'on': True}}}
            value['server']['hosts'].append('b')
            value['labels']['y'].append(2)
            assert (defaults, file_config, overrides) == original
            assert load({'x': {'old': 1}}, {'x': None}, {'x.new': 2}) == {'x': {'new': 2}}
            assert load({'x': {'old': 1}}, {}, {'x': None, 'x.new': 2}) == {'x': {'new': 2}}
            assert load({'x': {'old': 1}}, {}, {'x.new': 2, 'x': None}) == {'x': None}
            assert load({}, {}, {}) == {}
            """),
        {
            "config.py": dedent("""\
                from merge import merge
                from paths import apply

                def load(defaults, file_config, overrides):
                    return apply(merge(defaults, file_config), overrides)
                """),
            "merge.py": dedent("""\
                from copy import deepcopy

                def merge(base, layer):
                    result = deepcopy(base)
                    for key, value in layer.items():
                        previous = result.get(key)
                        if isinstance(previous, dict) and isinstance(value, dict):
                            result[key] = merge(previous, value)
                        else:
                            result[key] = deepcopy(value)
                    return result
                """),
            "paths.py": dedent("""\
                from merge import merge

                def apply(config, overrides):
                    for key, value in overrides.items():
                        parts = key.split(".")
                        node = config
                        for part in parts[:-1]:
                            if not isinstance(node.get(part), dict):
                                node[part] = {}
                            node = node[part]
                        leaf = parts[-1]
                        node.update(merge({leaf: node[leaf]} if leaf in node else {}, {leaf: value}))
                    return config
                """),
        },
    ),
    "H03": Repair(
        "cursor-pagination",
        """catalog.page(records, limit, cursor=None) 的分页在合并多个来源的记录时会跳项。
records 是 (id, updated_at, value) 三元组序列，id 为字符串，updated_at 为整数。
先按 id 去重，保留 updated_at 最大的记录，同 id 同时间保留最早输入。
再按 (updated_at, id) 升序排列。cursor 是上一页末条的 (updated_at, id)，只返回严格大于 cursor 的记录。
返回 (当前页记录列表, 下一页 cursor)。仅在还有后续记录时返回下一页 cursor。
limit <= 0 抛 ValueError。cursor 对应的原记录被删时仍可继续。保持入口签名和返回形状。""",
        {
            "catalog.py": dedent("""\
                from normalize import unique
                from cursor import slice_page

                def page(records, limit, cursor=None):
                    return slice_page(unique(records), limit, cursor)
                """),
            "normalize.py": dedent("""\
                def unique(records):
                    by_id = {}
                    for record in records:
                        by_id[record[0]] = record
                    return list(by_id.values())
                """),
            "cursor.py": dedent("""\
                def slice_page(records, limit, cursor):
                    eligible = [row for row in records if cursor is None or row[1] > cursor[0]]
                    page = eligible[:limit]
                    next_cursor = (page[-1][1], page[-1][0]) if page else None
                    return page, next_cursor
                """),
        },
        dedent("""\
            from catalog import page
            rows = [('b', 2, 'B'), ('z', 1, 'Z'), ('c', 2, 'C-first'), ('a', 2, 'A'),
                    ('c', 1, 'stale'), ('a', 0, 'stale'), ('d', 3, 'D'), ('c', 2, 'C-second')]
            first, cursor = page(rows, 2)
            assert first == [('z', 1, 'Z'), ('a', 2, 'A')] and cursor == (2, 'a')
            second, cursor = page(rows, 2, cursor)
            assert second == [('b', 2, 'B'), ('c', 2, 'C-first')] and cursor == (2, 'c')
            assert page(rows, 1, cursor) == ([('d', 3, 'D')], None)
            assert page(rows, 2, (2, 'aa')) == ([('b', 2, 'B'), ('c', 2, 'C-first')], (2, 'c'))
            assert page(rows, 2, (3, 'd')) == ([], None)
            assert page([], 2) == ([], None)
            for bad in (0, -2):
                try:
                    page(rows, bad)
                except ValueError:
                    pass
                else:
                    raise AssertionError('nonpositive limit accepted')
            """),
        {
            "normalize.py": dedent("""\
                def unique(records):
                    by_id = {}
                    for record in records:
                        identifier, timestamp, _ = record
                        if identifier not in by_id or timestamp > by_id[identifier][1]:
                            by_id[identifier] = record
                    return list(by_id.values())
                """),
            "cursor.py": dedent("""\
                def slice_page(records, limit, cursor):
                    if limit <= 0:
                        raise ValueError("limit must be positive")
                    def key(row):
                        return row[1], row[0]
                    eligible = sorted((row for row in records if cursor is None or key(row) > cursor), key=key)
                    page = eligible[:limit]
                    return page, key(page[-1]) if len(eligible) > limit else None
                """),
        },
    ),
    "H04": Repair(
        "atomic-reservation",
        """修复 fulfillment.fulfill(stock, orders) 的库存预留。
stock 是 SKU 到非负库存的字典；orders 按输入顺序处理，每项为 (order_id, lines)，lines 是 (sku, quantity) 列表。
同一订单内先汇总同 SKU 数量。存在未知 SKU、非正数量、空订单或任一 SKU 不足时，拒绝整单，库存完全不变。
相同 order_id 仅处理第一次出现的订单（第一次被拒绝也一样），后续忽略。
保持返回 (剩余库存字典, [(order_id, 'accepted' 或 'rejected'), ...])，保持输入 stock 不被修改。""",
        {
            "fulfillment.py": dedent("""\
                from lines import aggregate
                from reservation import reserve

                def fulfill(stock, orders):
                    remaining = stock.copy()
                    report = []
                    for order_id, lines in orders:
                        required = aggregate(lines)
                        ok = reserve(remaining, required)
                        report.append((order_id, "accepted" if ok else "rejected"))
                    return remaining, report
                """),
            "lines.py": dedent("""\
                def aggregate(lines):
                    return dict(lines)
                """),
            "reservation.py": dedent("""\
                def reserve(stock, required):
                    for sku, quantity in required.items():
                        if stock.get(sku, 0) < quantity:
                            return False
                        stock[sku] = stock.get(sku, 0) - quantity
                    return True
                """),
        },
        dedent("""\
            from fulfillment import fulfill
            stock = {'A': 5, 'B': 2, 'C': 0}
            orders = [
                ('oversold', [('A', 3), ('A', 3)]),
                ('oversold', [('A', 1)]),
                ('partial', [('A', 2), ('B', 3)]),
                ('negative', [('A', -1), ('A', 3)]),
                ('zero', [('B', 0)]),
                ('unknown', [('A', 1), ('X', 1)]),
                ('empty', []),
                ('ok', [('A', 2), ('A', 3), ('B', 1)]),
                ('ok', [('B', 1)]),
                ('last', [('B', 1)]),
                ('none', [('C', 1)]),
            ]
            final, report = fulfill(stock, orders)
            assert final == {'A': 0, 'B': 0, 'C': 0}
            assert report == [(key, 'accepted' if key in {'ok', 'last'} else 'rejected') for key in
                              ('oversold', 'partial', 'negative', 'zero', 'unknown', 'empty', 'ok', 'last', 'none')]
            assert stock == {'A': 5, 'B': 2, 'C': 0}
            assert fulfill(stock, []) == (stock, [])
            """),
        {
            "fulfillment.py": dedent("""\
                from lines import aggregate
                from reservation import reserve

                def fulfill(stock, orders):
                    remaining = stock.copy()
                    report = []
                    seen = set()
                    for order_id, lines in orders:
                        if order_id in seen:
                            continue
                        seen.add(order_id)
                        required = aggregate(lines)
                        ok = reserve(remaining, required)
                        report.append((order_id, "accepted" if ok else "rejected"))
                    return remaining, report
                """),
            "lines.py": dedent("""\
                def aggregate(lines):
                    totals = {}
                    for sku, quantity in lines:
                        if quantity <= 0:
                            return {}
                        totals[sku] = totals.get(sku, 0) + quantity
                    return totals
                """),
            "reservation.py": dedent("""\
                def reserve(stock, required):
                    if not required or any(sku not in stock or stock[sku] < n for sku, n in required.items()):
                        return False
                    for sku, quantity in required.items():
                        stock[sku] -= quantity
                    return True
                """),
        },
    ),
})

# (one-based evidence line, component, error code)
LOG_CASES = {
    "C01": (143, "assets/compiler.py", "E_ASSET_MANIFEST"),
    "C02": (809, "config/loader.py", "E_CONFIG_VERSION"),
    "C03": (1463, "tests/fixture.py", "E_FIXTURE_MISSING"),
}

SMOKE_CASES = ("X01", "C01")
CASE_IDS = BASELINE_REPAIR_IDS + tuple(LOG_CASES)
HARD_CASE_IDS = tuple(case for case in REPAIRS if case not in BASELINE_REPAIR_IDS)


def category(case):
    return REPAIRS[case].category if case in REPAIRS else "long-context"


def prompt_for(case):
    if case in REPAIRS:
        return REPAIRS[case].prompt + "\n仅在当前目录工作。完成后简述修改和你运行的验证。"
    return (
        "诊断当前目录 build.log 的构建失败。定位引发连锁错误的首个构建失败。"
        "仅在当前目录工作，保留 build.log，在 answer.json 写入 JSON 对象，"
        "包含 component（日志中的组件路径）、code（错误码）、line（从 1 起算的行号，整数）。"
        "简述根因证据。"
    )


def prepare(case, workspace):
    workspace.mkdir()
    if case in REPAIRS:
        for name, content in REPAIRS[case].files.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    else:
        (workspace / "build.log").write_text(build_log(case), encoding="utf-8")
    return prompt_for(case)


def build_log(case):
    line, component, code = LOG_CASES[case]
    log = []
    for i in range(1, 1801):
        if i == line:
            message = f"ERROR stage=build component={component} code={code} failed while preparing inputs"
        elif i > line and i % 13 == 0:
            message = f"ERROR stage=cleanup component=runner.py code=E_BUILD_ABORTED upstream build did not complete"
        else:
            message = f"INFO worker={(i * 7) % 19:02d} target=bundle-{i % 37:02d} processed shard={i:04d}"
        log.append(message)
    return "\n".join(log) + "\n"


def grade(case, workspace):
    if case in LOG_CASES:
        line, component, code = LOG_CASES[case]
        try:
            source = workspace / "build.log"
            expected_log = build_log(case).encode("utf-8")
            if (source.is_symlink() or source.stat().st_size != len(expected_log)
                    or source.read_bytes() != expected_log):
                return False, "build.log 被修改"
            path = workspace / "answer.json"
            if path.is_symlink() or path.stat().st_size > 4096:
                return False, "answer.json 无效"
            answer = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return False, f"answer.json 无法读取：{error}"
        passed = (
            isinstance(answer, dict) and type(answer.get("line")) is int
            and answer["line"] == line and answer.get("component") == component
            and answer.get("code") == code
        )
        return passed, "证据匹配" if passed else "证据字段不匹配"

    return check_python(REPAIRS[case].check, workspace)


def check_python(check, workspace):
    check = "import sys\nsys.path.insert(0, '.')\n" + check
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", check], cwd=workspace,
            capture_output=True, text=True, errors="replace", timeout=15,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return False, "独立验收超时"
    return result.returncode == 0, result.stderr[-4000:] or "独立断言通过"


def write_reference(case, workspace):
    if case in REPAIRS:
        for name, content in REPAIRS[case].reference.items():
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return
    line, component, code = LOG_CASES[case]
    (workspace / "answer.json").write_text(
        json.dumps({"line": line, "component": component, "code": code}), encoding="utf-8",
    )
