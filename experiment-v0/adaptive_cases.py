"""New tasks with separate checks for escalation and final scoring."""

from textwrap import dedent

from tasks import Repair


CASES = {
    "V01": Repair(
        "ordered-config-operations",
        """修复 config.render(base, operations)，将有序配置操作逐条作用于当前配置。
operations 每项是 (action, 点分隔路径, value)。set 用独立副本整体替换目标；
merge 在新旧都是字典时递归合并，否则用新值整体替换。遇到非字典父节点时，set/merge 用空字典替换父节点。
remove 删除目标，忽略 value。目标不存在或父路径不是字典时，remove 不创建节点也不改动原值。
数组和 None 都是合法值。保留操作顺序、入口签名，输入与输出不得共享可变对象。""",
        {
            "config.py": dedent("""\
                from merge import merge
                from paths import collect

                def render(base, operations):
                    return merge(base, collect(operations))
                """),
            "merge.py": dedent("""\
                def merge(old, new):
                    return {**old, **new}
                """),
            "paths.py": dedent("""\
                def collect(operations):
                    patch = {}
                    for action, path, value in operations:
                        if action == "remove":
                            patch.pop(path, None)
                        else:
                            patch[path] = value
                    return patch
                """),
        },
        dedent("""\
            from config import render
            from copy import deepcopy
            base = {'server': {'tls': True, 'ports': [80], 'auth': {'mode': 'old'}}, 'level': 4}
            ops = [('remove', 'server.auth', None), ('merge', 'server.auth', {'enabled': True}),
                   ('merge', 'server', {'ports': [], 'auth': {'required': False}}),
                   ('remove', 'level.child', None), ('remove', 'never.created', None)]
            before = deepcopy((base, ops))
            got = render(base, ops)
            assert got == {'server': {'tls': True, 'ports': [], 'auth': {'enabled': True, 'required': False}},
                           'level': 4}
            got['server']['ports'].append(123)
            assert (base, ops) == before
            assert render({'a': {'old': 1}}, [('set', 'a', {'new': 2})]) == {'a': {'new': 2}}
            assert render({'a': 7}, [('set', 'a.b.c', None)]) == {'a': {'b': {'c': None}}}
            assert render({'a': [1]}, [('merge', 'a', {'b': 3})]) == {'a': {'b': 3}}
            assert render({'a': {'b': 1}}, [('merge', 'a', 0), ('merge', 'a.c', 2)]) == {'a': {'c': 2}}
            """),
        {
            "config.py": dedent("""\
                from copy import deepcopy
                from paths import apply

                def render(base, operations):
                    result = deepcopy(base)
                    for operation in operations:
                        apply(result, *operation)
                    return result
                """),
            "merge.py": dedent("""\
                from copy import deepcopy

                def merge(old, new):
                    if not isinstance(old, dict) or not isinstance(new, dict):
                        return deepcopy(new)
                    result = deepcopy(old)
                    for key, value in new.items():
                        result[key] = merge(result.get(key), value)
                    return result
                """),
            "paths.py": dedent("""\
                from copy import deepcopy
                from merge import merge

                def apply(config, action, path, value):
                    parts = path.split(".")
                    node = config
                    for part in parts[:-1]:
                        if not isinstance(node.get(part), dict):
                            if action == "remove":
                                return
                            node[part] = {}
                        node = node[part]
                    leaf = parts[-1]
                    if action == "remove":
                        node.pop(leaf, None)
                    elif action == "set":
                        node[leaf] = deepcopy(value)
                    else:
                        node[leaf] = merge(node.get(leaf), value)
                """),
        },
    ),
    "V02": Repair(
        "atomic-transfer-replay",
        """修复 ledger.process(balances, batches)。batches 每项是 (batch_id, lines)，lines 是有序的 (source, target, quantity)。
空批次拒绝。每行数量必须为正，两个账户都必须存在且不同。逐行使用批次内当前余额，同批次早先的转入可以资助后续转出；
只要任一行无效或余额不足，整个批次拒绝且余额不变。后续可用相同 batch_id 重试被拒绝的批次。
一旦某个 batch_id 成功，后续相同 ID 一律记为 duplicate。返回 (剩余余额字典, 按输入顺序的 [(batch_id, 'applied'/'rejected'/'duplicate')])。
原余额输入不可修改，保持入口签名。""",
        {
            "ledger.py": dedent("""\
                from posting import apply_batch

                def process(balances, batches):
                    state = dict(balances)
                    statuses = []
                    seen = set()
                    for batch_id, lines in batches:
                        if batch_id in seen:
                            statuses.append((batch_id, "duplicate"))
                            continue
                        seen.add(batch_id)
                        status = "applied" if apply_batch(state, lines) else "rejected"
                        statuses.append((batch_id, status))
                    return state, statuses
                """),
            "posting.py": dedent("""\
                from rules import allowed

                def apply_batch(state, lines):
                    for source, target, quantity in lines:
                        if not allowed(state, source, target, quantity):
                            return False
                        state[source] -= quantity
                        state[target] = state.get(target, 0) + quantity
                    return True
                """),
            "rules.py": dedent("""\
                def allowed(state, source, target, quantity):
                    return state.get(source, 0) >= quantity
                """),
        },
        dedent("""\
            from ledger import process
            balances = {'a': 9, 'b': 0, 'c': 0}
            batches = [
                ('redo', [('a', 'b', 5), ('b', 'c', 6)]),
                ('chain', [('a', 'b', 4), ('b', 'c', 4)]),
                ('redo', [('c', 'b', 2)]),
                ('chain', [('missing', 'c', 1)]),
                ('bad-account', [('a', 'b', 1), ('a', 'missing', 1)]),
                ('bad-quantity', [('a', 'b', 1), ('b', 'a', -1)]),
                ('self', [('a', 'a', 1)]),
                ('empty', []),
            ]
            expected = ['rejected', 'applied', 'applied', 'duplicate', 'rejected', 'rejected', 'rejected', 'rejected']
            assert process(balances, batches) == ({'a': 5, 'b': 2, 'c': 2},
                 [(key, status) for (key, _), status in zip(batches, expected)])
            assert balances == {'a': 9, 'b': 0, 'c': 0}
            assert process(balances, []) == (balances, [])
            assert process(balances, [('x', [('b', 'a', 0)])]) == (balances, [('x', 'rejected')])
            """),
        {
            "ledger.py": dedent("""\
                from posting import apply_batch

                def process(balances, batches):
                    state = dict(balances)
                    statuses = []
                    seen = set()
                    for batch_id, lines in batches:
                        if batch_id in seen:
                            statuses.append((batch_id, "duplicate"))
                        elif apply_batch(state, lines):
                            seen.add(batch_id)
                            statuses.append((batch_id, "applied"))
                        else:
                            statuses.append((batch_id, "rejected"))
                    return state, statuses
                """),
            "posting.py": dedent("""\
                from rules import allowed

                def apply_batch(state, lines):
                    if not lines:
                        return False
                    trial = dict(state)
                    for source, target, quantity in lines:
                        if not allowed(trial, source, target, quantity):
                            return False
                        trial[source] -= quantity
                        trial[target] += quantity
                    state.update(trial)
                    return True
                """),
            "rules.py": dedent("""\
                def allowed(state, source, target, quantity):
                    return (source != target and source in state and target in state
                            and quantity > 0 and state[source] >= quantity)
                """),
        },
    ),
    "V03": Repair(
        "ordered-job-scheduling",
        """修复 schedule.plan(jobs, resources)。jobs 按输入顺序排列，每项是 (唯一 job_id, prerequisite_ids, costs)。
每一步从剩余任务的输入顺序开头重新找，选第一个依赖已全部完成且资源足够的任务。运行后扣减它的全部资源。
没被选中的任务不消耗资源。缺失资源视为 0，需求为 0 的项不影响可运行性，也不新增资源键。
没有可运行任务就结束。循环依赖、未知依赖和资源不足的任务保留在未完成列表。
返回 (完成 ID 的执行顺序, 未完成 ID 的原输入顺序, 剩余资源字典)。资源输入不可修改。""",
        {
            "schedule.py": dedent("""\
                from dependencies import ready
                from budget import consume

                def plan(jobs, resources):
                    complete, pending = [], []
                    for key, deps, costs in jobs:
                        if ready(deps, complete) and consume(resources, costs):
                            complete.append(key)
                        else:
                            pending.append(key)
                    return complete, pending, resources
                """),
            "dependencies.py": dedent("""\
                def ready(deps, complete):
                    return not deps or any(dep in complete for dep in deps)
                """),
            "budget.py": dedent("""\
                def consume(resources, costs):
                    for resource, quantity in costs.items():
                        if resources.get(resource, 0) < quantity:
                            return False
                        resources[resource] = resources.get(resource, 0) - quantity
                    return True
                """),
        },
        dedent("""\
            from schedule import plan
            resources = {'cpu': 4, 'ram': 2}
            jobs = [
                ('unlocked', ['boot', 'seed'], {'cpu': 1}),
                ('missing', ['boot', 'unknown'], {'cpu': 2}),
                ('oversized', [], {'cpu': 1, 'ram': 3}),
                ('boot', [], {'cpu': 1}),
                ('seed', ['boot'], {'ram': 1, 'unknown': 0}),
                ('competing', [], {'cpu': 2}),
                ('c1', ['c2'], {}), ('c2', ['c1'], {}),
            ]
            assert plan(jobs, resources) == (['boot', 'seed', 'unlocked', 'competing'],
                ['missing', 'oversized', 'c1', 'c2'], {'cpu': 0, 'ram': 1})
            assert resources == {'cpu': 4, 'ram': 2}
            assert plan([('priority', ['unlock'], {'cpu': 1}), ('unlock', [], {}),
                         ('later', [], {'cpu': 1})], {'cpu': 1}) == (
                ['unlock', 'priority'], ['later'], {'cpu': 0})
            assert plan([('zero', [], {'absent': 0})], {}) == (['zero'], [], {})
            assert plan([], resources) == ([], [], resources)
            """),
        {
            "schedule.py": dedent("""\
                from dependencies import ready
                from budget import consume

                def plan(jobs, resources):
                    remaining = list(jobs)
                    state = dict(resources)
                    complete = []
                    while remaining:
                        for i, (key, deps, costs) in enumerate(remaining):
                            if ready(deps, complete) and consume(state, costs):
                                complete.append(key)
                                remaining.pop(i)
                                break
                        else:
                            break
                    return complete, [job[0] for job in remaining], state
                """),
            "dependencies.py": dedent("""\
                def ready(deps, complete):
                    return all(dep in complete for dep in deps)
                """),
            "budget.py": dedent("""\
                def consume(resources, costs):
                    if any(resources.get(resource, 0) < quantity for resource, quantity in costs.items()):
                        return False
                    for resource, quantity in costs.items():
                        if quantity:
                            resources[resource] -= quantity
                    return True
                """),
        },
    ),
}

# Separate examples drive escalation. The final grader uses different data.
GATES = {
    "V01": (
        ("按顺序删除父节点，再建立子路径时不得恢复已删除的旧字段",
         "from config import render\n"
         "assert render({'x': {'stale': 1}}, [('remove', 'x', None), ('merge', 'x.y', {'z': 3})])"
         " == {'x': {'y': {'z': 3}}}"),
        ("在不存在的子路径执行 remove 时保留标量父节点且不创建路径",
         "from config import render\n"
         "assert render({'leaf': 2}, [('remove', 'leaf.x', 1), ('remove', 'absent.x', 1)]) == {'leaf': 2}"),
    ),
    "V02": (
        ("后续行失败要回滚先前扣款，被拒绝的 ID 仍可重试",
         "from ledger import process\n"
         "assert process({'u': 8, 'v': 0}, [('p', [('u','v',4),('v','u',5)]), ('p', [('u','v',7)])])"
         " == ({'u':1,'v':7}, [('p','rejected'),('p','applied')])"),
        ("只有当前余额允许时可出账，同批前面的转入可以使用",
         "from ledger import process\n"
         "assert process({'p':4,'q':0,'r':0}, [('x',[('p','q',4),('q','r',3)])])"
         " == ({'p':0,'q':1,'r':3}, [('x','applied')])"),
    ),
    "V03": (
        ("解锁一个更早的任务后，下一轮要从输入顺序起点重新选择",
         "from schedule import plan\n"
         "assert plan([('early',['first'],{'slot':1}),('first',[],{}),('late',[],{'slot':1})], {'slot':1})"
         " == (['first','early'], ['late'], {'slot':0})"),
        ("资源不足的任务不能部分扣减资源",
         "from schedule import plan\n"
         "assert plan([('bad',[],{'slot':1,'memory':4}),('ok',[],{'slot':2})], {'slot':2,'memory':1})"
         " == (['ok'], ['bad'], {'slot':0,'memory':1})"),
    ),
}
