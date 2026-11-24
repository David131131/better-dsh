"""Predefined holdout fixtures and distinct, cheaper escalation checks."""

from textwrap import dedent

from tasks import Repair


CASES = {
    "W01": Repair(
        "transactional-json-patch",
        """修复 patch.apply(document, batches)，实现事务化 JSON 补丁。返回 (新文档, 每个批次是否提交的布尔列表)，不得修改或共享输入中的可变对象。
每个批次是按顺序执行的操作列表。任何操作非法则整个批次回滚，继续下一个批次。空批次成功。操作是字典，支持 add、remove、replace、copy、move、test；copy/move 的源路径字段叫 from。
路径使用 JSON Pointer，空字符串表示根，其他以 / 开头，逐段解码 ~0 为 ~、~1 为 /，其余 ~ 转义非法。字典键可为空字符串。数组索引是 0 或无前导零的正十进制整数，只接受 ASCII 数字；只有 add 语义的最后一段可以用 - 追加。
add 对字典创建或覆盖键，对列表插入，索引可等于长度；中间节点必须存在。remove 和 replace 要求目标存在，禁止 remove 根。add/replace 可以替换根。copy 读源值的独立副本，用 add 语义写目的地。move 先验证源存在，再删除源并按删除后的结构 add；禁止移动根或移到源的严格子路径，源和目标相同时是无操作。test 要求递归同类型同值，布尔值和整数不同。保持入口和返回形状。""",
        {
            "patch.py": dedent("""\
                from operations import apply_one

                def apply(document, batches):
                    decisions = []
                    for batch in batches:
                        try:
                            for operation in batch:
                                document = apply_one(document, operation)
                        except (KeyError, IndexError, ValueError, TypeError):
                            decisions.append(False)
                        else:
                            decisions.append(True)
                    return document, decisions
                """),
            "pointer.py": dedent("""\
                def segments(path):
                    return [item.replace('~1', '/').replace('~0', '~') for item in path.strip('/').split('/')]

                def walk(value, path):
                    for part in path:
                        value = value[int(part) if isinstance(value, list) else part]
                    return value
                """),
            "operations.py": dedent("""\
                from pointer import segments, walk

                def apply_one(document, operation):
                    parts = segments(operation['path'])
                    target = walk(document, parts[:-1])
                    key = int(parts[-1]) if isinstance(target, list) else parts[-1]
                    kind = operation['op']
                    if kind in ('add', 'replace'):
                        target[key] = operation['value']
                    elif kind == 'remove':
                        del target[key]
                    elif kind == 'test':
                        if target[key] != operation['value']:
                            raise ValueError('test failed')
                    else:
                        raise ValueError('unsupported op')
                    return document
                """),
        },
        dedent("""\
            from copy import deepcopy
            from patch import apply
            doc = {'': {'~1': [False, {'k': ['v']}, 'z']}, 'a/b': {}, 'n': 1}
            batches = [
                [{'op':'copy', 'from':'//~01/1', 'path':'/a~1b/clone'},
                 {'op':'move', 'from':'//~01/0', 'path':'//~01/2'}],
                [{'op':'add', 'path':'/a~1b/clone/k/-', 'value':'x'},
                 {'op':'test', 'path':'/n', 'value':True}],
                [{'op':'add', 'path':'//~01/01', 'value':4}],
                [{'op':'move', 'from':'/a~1b', 'path':'/a~1b/nested'}],
                [{'op':'remove', 'path':'//~01/0'}],
                [{'op':'test', 'path':'//~01/1', 'value':False}],
                [{'op':'move', 'from':'/missing', 'path':'/missing'}],
                [{'op':'add', 'path':'/bad~2', 'value':1}],
                [{'op':'replace', 'path':'/unknown', 'value':0}],
            ]
            before = deepcopy((doc, batches))
            result, ok = apply(doc, batches)
            assert ok == [True, False, False, False, True, True, False, False, False], ok
            assert result == {'': {'~1': ['z', False]}, 'a/b': {'clone': {'k': ['v']}}, 'n': 1}, result
            result['a/b']['clone']['k'].append('new')
            assert (doc, batches) == before
            assert apply({'x': [4]}, [[{'op':'copy', 'from':'/x', 'path':''}],
                                      [{'op':'add','path':'/-','value':8}],
                                      [{'op':'remove','path':''}], []]) == ([4,8], [True, True, False, True])
            assert apply([{'x': 1}], [[{'op':'test','path':'','value':[{'x':True}]}]]) == ([{'x':1}], [False])
            """),
        {
            "patch.py": dedent("""\
                from copy import deepcopy
                from operations import apply_one

                def apply(document, batches):
                    document = deepcopy(document)
                    decisions = []
                    for batch in batches:
                        trial = deepcopy(document)
                        try:
                            for operation in batch:
                                trial = apply_one(trial, operation)
                        except (KeyError, IndexError, ValueError, TypeError):
                            decisions.append(False)
                        else:
                            document = trial
                            decisions.append(True)
                    return document, decisions
                """),
            "pointer.py": dedent("""\
                import re

                def segments(path):
                    if path == '':
                        return []
                    if not path.startswith('/') or re.search(r'~(?![01])', path):
                        raise ValueError('invalid pointer')
                    return [part.replace('~1', '/').replace('~0', '~') for part in path[1:].split('/')]

                def index(part, size, add=False):
                    if add and part == '-':
                        return size
                    if not re.fullmatch(r'0|[1-9][0-9]*', part):
                        raise ValueError('invalid index')
                    result = int(part)
                    if result > size or (result == size and not add):
                        raise IndexError(result)
                    return result

                def walk(value, path):
                    for part in path:
                        if isinstance(value, list):
                            value = value[index(part, len(value))]
                        elif isinstance(value, dict):
                            value = value[part]
                        else:
                            raise TypeError('not a container')
                    return value
                """),
            "operations.py": dedent("""\
                from copy import deepcopy
                from pointer import index, segments, walk

                def equal(a, b):
                    if type(a) is not type(b):
                        return False
                    if isinstance(a, dict):
                        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
                    if isinstance(a, list):
                        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
                    return a == b

                def write(document, parts, action, value=None):
                    if not parts:
                        if action == 'remove':
                            raise ValueError('cannot remove root')
                        return value
                    target = walk(document, parts[:-1])
                    key = parts[-1]
                    if isinstance(target, list):
                        pos = index(key, len(target), action == 'add')
                        if action == 'add':
                            target.insert(pos, value)
                        elif action == 'remove':
                            del target[pos]
                        else:
                            target[pos] = value
                    elif isinstance(target, dict):
                        if action != 'add' and key not in target:
                            raise KeyError(key)
                        if action == 'remove':
                            del target[key]
                        else:
                            target[key] = value
                    else:
                        raise TypeError('not a container')
                    return document

                def apply_one(document, operation):
                    parts = segments(operation['path'])
                    kind = operation['op']
                    if kind == 'test':
                        if not equal(walk(document, parts), operation['value']):
                            raise ValueError('test failed')
                        return document
                    if kind in ('copy', 'move'):
                        source = segments(operation['from'])
                        value = deepcopy(walk(document, source))
                        if kind == 'move':
                            if not source or (len(parts) > len(source) and parts[:len(source)] == source):
                                raise ValueError('invalid move')
                            if source == parts:
                                return document
                            document = write(document, source, 'remove')
                        return write(document, parts, 'add', value)
                    if kind in ('add', 'replace'):
                        return write(document, parts, kind, deepcopy(operation['value']))
                    if kind == 'remove':
                        return write(document, parts, kind)
                    raise ValueError('unsupported op')
                """),
        },
    ),
    "W02": Repair(
        "lease-replay",
        """修复 service.run(events) 的可重放租约。events 是时间非递减的字典序列，t 是整数，op 为 acquire/renew/release/snapshot。返回与事件逐项对应的字典或 None。
处理每个事件前清理 expires <= t 的租约。每个 resource 最多一个有效租约。acquire 要求空闲且整数 ttl > 0，成功结果为 {'owner': owner, 'token': token, 'expires': t+ttl}。同一 resource 的 token 从 1 起，成功获取才加一，释放和过期不重置。
renew 和 release 必须匹配当前资源的 owner 和 token；renew 要求 ttl > 0，expires 设为 t+ttl（允许缩短），返回更新后的租约。release 成功返回释放前的租约。失败都返回 None，不改状态。
除 snapshot 外，事件有 request_id。只有成功的请求进入全局重放表，后来相同 ID 返回原始成功结果的独立副本，不再次执行，即使动作、资源或 owner 不同。被拒绝的 ID 可以重试。snapshot 忽略 ID，返回当时所有有效租约的独立快照，后续操作不应修改历史结果。保持入口，禁止修改 events。""",
        {
            "service.py": dedent("""\
                from store import Store
                from replay import Replay

                def run(events):
                    store, replay, output = Store(), Replay(), []
                    for event in events:
                        op = event['op']
                        if op == 'snapshot':
                            output.append(dict(store.active))
                        else:
                            output.append(replay.apply(event, store))
                    return output
                """),
            "replay.py": dedent("""\
                class Replay:
                    def __init__(self):
                        self.seen = {}

                    def apply(self, event, store):
                        key = event['resource'], event['request_id']
                        if key not in self.seen:
                            self.seen[key] = store.apply(event)
                        return self.seen[key]
                """),
            "store.py": dedent("""\
                class Store:
                    def __init__(self):
                        self.active = {}

                    def apply(self, event):
                        t, op, resource = event['t'], event['op'], event['resource']
                        lease = self.active.get(resource)
                        if op == 'acquire':
                            if lease:
                                return None
                            lease = {'owner': event['owner'], 'token': 1, 'expires': t + event['ttl']}
                            self.active[resource] = lease
                        elif not lease or lease['owner'] != event['owner']:
                            return None
                        elif op == 'renew':
                            lease['expires'] = t + event['ttl']
                        else:
                            del self.active[resource]
                        return lease
                """),
        },
        dedent("""\
            from copy import deepcopy
            from service import run
            def e(t, op, rid=None, r='a', owner='ada', **kw):
                return dict(t=t,op=op,request_id=rid,resource=r,owner=owner,**kw)
            def L(token, expires, owner='ada'):
                return dict(owner=owner,token=token,expires=expires)
            events = [
                e(0,'acquire','initial',ttl=4),
                e(1,'snapshot','initial'),
                e(2,'renew','wrong',token=9,ttl=9),
                e(2,'renew','short',token=1,ttl=1),
                e(3,'snapshot'),
                e(3,'acquire','wrong',owner='bea',ttl=2),
                e(3,'release','initial',r='b',owner='someone',token=99),
                e(4,'renew','invalid',owner='bea',token=2,ttl=0),
                e(4,'release','free',owner='bea',token=2),
                e(4,'acquire','invalid',ttl=1),
                e(5,'acquire','later',ttl=3),
                e(5,'release','free',token=4),
                e(5,'snapshot'),
            ]
            before = deepcopy(events)
            expected = [L(1,4), {'a':L(1,4)}, None, L(1,3), {}, L(2,5,'bea'), L(1,4),
                        None, L(2,5,'bea'), L(3,5), L(4,8), L(2,5,'bea'), {'a':L(4,8)}]
            actual = run(events)
            assert actual == expected, actual
            actual[0]['expires'] = 99
            assert actual[1] == {'a':L(1,4)} and actual[6] == L(1,4)
            assert actual[8] is not actual[11]
            assert events == before and run(events) == expected
            assert run([e(0,'acquire','zero',ttl=0), e(0,'acquire','zero',r='z',ttl=1)]) == [None,L(1,1)]
            """),
        {
            "service.py": dedent("""\
                from copy import deepcopy
                from store import Store
                from replay import Replay

                def run(events):
                    store, replay, output = Store(), Replay(), []
                    for event in events:
                        store.expire(event['t'])
                        if event['op'] == 'snapshot':
                            output.append(deepcopy(store.active))
                        else:
                            output.append(replay.apply(event, store))
                    return output
                """),
            "replay.py": dedent("""\
                from copy import deepcopy

                class Replay:
                    def __init__(self):
                        self.seen = {}

                    def apply(self, event, store):
                        key = event['request_id']
                        if key not in self.seen:
                            result = store.apply(event)
                            if result is None:
                                return None
                            self.seen[key] = deepcopy(result)
                        return deepcopy(self.seen[key])
                """),
            "store.py": dedent("""\
                class Store:
                    def __init__(self):
                        self.active = {}
                        self.tokens = {}

                    def expire(self, t):
                        for key in [k for k, value in self.active.items() if value['expires'] <= t]:
                            del self.active[key]

                    def apply(self, event):
                        t, op, resource = event['t'], event['op'], event['resource']
                        lease = self.active.get(resource)
                        if op == 'acquire':
                            if lease or event['ttl'] <= 0:
                                return None
                            token = self.tokens.get(resource, 0) + 1
                            self.tokens[resource] = token
                            lease = {'owner':event['owner'], 'token':token, 'expires':t+event['ttl']}
                            self.active[resource] = lease
                        elif not lease or lease['owner'] != event['owner'] or lease['token'] != event['token']:
                            return None
                        elif op == 'renew':
                            if event['ttl'] <= 0:
                                return None
                            lease['expires'] = t + event['ttl']
                        else:
                            del self.active[resource]
                        return lease
                """),
        },
    ),
    "W03": Repair(
        "discount-refund-allocation",
        """修复 invoice.settle(lines, discount, refunds)，金额为整数分。lines 每项为 (唯一 SKU, 正整数数量, 非负单价)，discount 在 0 至商品总毛额之间。
先按各行毛额占比分配折扣：各行取整向下，剩余折扣分给分数余量最大的行，余量相同按原始行顺序；总毛额为 0 时各行折扣为 0。
每行净额均分给该行各单位，不能均分的多出分给最前面的单位（例如净额 7 分、3 个单位为 [3,2,2]）。
refunds 按输入顺序，每项是 (request_id, [(sku, quantity), ...])。每个明细的数量必须为正，SKU 必须存在；先聚合同 SKU，再检查剩余可退数量。空退款或任一明细非法整单拒绝且不消耗单位。成功时退最早尚未退款的单位，金额可能为 0。
成功 ID 重放返回第一次金额，不重复扣减，也不检查新明细。拒绝的 ID 不记录，可以重试。返回 (初始订单净额, 按请求顺序的退款金额列表，失败为 None, 剩余订单净额)。不能修改输入，保持入口。""",
        {
            "invoice.py": dedent("""\
                from pricing import unit_values
                from refunds import apply_refunds

                def settle(lines, discount, refunds):
                    units = unit_values(lines, discount)
                    net = sum(sum(values) for values in units.values())
                    amounts = apply_refunds(units, refunds)
                    return net, amounts, net - sum(value or 0 for value in amounts)
                """),
            "pricing.py": dedent("""\
                def unit_values(lines, discount):
                    gross = sum(quantity * price for _, quantity, price in lines)
                    units = {}
                    for sku, quantity, price in lines:
                        line_discount = round(discount * quantity * price / gross) if gross else 0
                        net = quantity * price - line_discount
                        units[sku] = [net // quantity] * quantity
                    return units
                """),
            "refunds.py": dedent("""\
                def apply_refunds(units, refunds):
                    output = []
                    for request, details in refunds:
                        total = 0
                        for sku, quantity in details:
                            values = units.get(sku, [])
                            if len(values) < quantity:
                                total = None
                                break
                            total += sum(values[:quantity])
                            del values[:quantity]
                        output.append(total)
                    return output
                """),
        },
        dedent("""\
            from copy import deepcopy
            from invoice import settle
            items = [('c', 3, 3), ('a', 1, 9), ('b', 2, 3), ('free', 2, 0)]
            requests = [
                ('a', [('c', 1), ('missing', 1)]), ('b', [('free', 1)]),
                ('a', [('c', 1)]), ('a', [('c', 8)]),
                ('c', [('c', -1), ('c', 2)]), ('d', [('c', 1), ('c', 2)]),
                ('e', [('c', 1), ('c', 1), ('b', 1)]), ('f', [('a', 1), ('b', 1)]),
                ('g', []), ('b', [('c', 2)]), ('h', [('free', 1)]),
            ]
            before = deepcopy((items, requests))
            assert settle(items, 5, requests) == (19, [None,0,3,3,None,None,7,9,None,0,0], 0)
            assert (items, requests) == before
            assert settle([('z', 1, 2), ('a', 1, 2)], 1,
                          [('first',[('z',1)]),('second',[('a',1)])]) == (3, [1,2], 0)
            assert settle([('x', 2, 0)], 0, [('a',[('x',2)]),('b',[('x',1)])]) == (0,[0,None],0)
            assert settle([], 0, [('empty', [])]) == (0, [None], 0)
            """),
        {
            "invoice.py": dedent("""\
                from pricing import unit_values
                from refunds import apply_refunds

                def settle(lines, discount, refunds):
                    units = unit_values(lines, discount)
                    net = sum(sum(values) for values in units.values())
                    amounts = apply_refunds(units, refunds)
                    return net, amounts, sum(sum(values) for values in units.values())
                """),
            "pricing.py": dedent("""\
                def unit_values(lines, discount):
                    gross = sum(quantity * price for _, quantity, price in lines)
                    allocations = [divmod(discount * quantity * price, gross) if gross else (0, 0)
                                   for _, quantity, price in lines]
                    discounts = [item[0] for item in allocations]
                    order = sorted(range(len(lines)), key=lambda i: (-allocations[i][1], i))
                    for i in order[:discount - sum(discounts)]:
                        discounts[i] += 1
                    units = {}
                    for (sku, quantity, price), share in zip(lines, discounts):
                        base, extra = divmod(quantity * price - share, quantity)
                        units[sku] = [base + 1] * extra + [base] * (quantity - extra)
                    return units
                """),
            "refunds.py": dedent("""\
                def apply_refunds(units, refunds):
                    output, successful = [], {}
                    for request, details in refunds:
                        if request in successful:
                            output.append(successful[request])
                            continue
                        if not details or any(sku not in units or quantity <= 0 for sku, quantity in details):
                            output.append(None)
                            continue
                        needed = {}
                        for sku, quantity in details:
                            needed[sku] = needed.get(sku, 0) + quantity
                        if any(quantity > len(units[sku]) for sku, quantity in needed.items()):
                            output.append(None)
                            continue
                        total = 0
                        for sku, quantity in needed.items():
                            total += sum(units[sku][:quantity])
                            del units[sku][:quantity]
                        successful[request] = total
                        output.append(total)
                    return output
                """),
        },
    ),
}

GATES = {
    "W01": (
        ("同一批次的后置测试失败时，前面的修改必须回滚",
         "from patch import apply\n"
         "assert apply({'a':2},[[{'op':'replace','path':'/a','value':3},"
         "{'op':'test','path':'/a','value':2}]]) == ({'a':2},[False])"),
        ("同列表 move 使用删除后的索引，最后一段 - 可追加",
         "from patch import apply\n"
         "assert apply([1,2,3],[[{'op':'move','from':'/0','path':'/1'}],"
         "[{'op':'add','path':'/-','value':4}]]) == ([2,1,3,4],[True,True])"),
    ),
    "W02": (
        ("到期边界先过期；被拒绝的 ID 可重试，成功获取的 token 继续递增",
         "from service import run\n"
         "a=dict(op='acquire',resource='k',owner='m',ttl=3)\n"
         "assert run([dict(a,t=0,request_id='a'),dict(a,t=2,request_id='b'),dict(a,t=3,request_id='b')])"
         " == [dict(owner='m',token=1,expires=3),None,dict(owner='m',token=2,expires=6)]"),
        ("旧 token 不可操作重新取得的租约，快照保留当时内容",
         "from service import run\n"
         "a=dict(op='acquire',resource='r',owner='u',ttl=1)\n"
         "assert run([dict(a,t=1,request_id='a'),dict(t=1,op='snapshot'),"
         "dict(a,t=2,request_id='b'),dict(t=2,op='release',request_id='c',resource='r',owner='u',token=1)])"
         " == [dict(owner='u',token=1,expires=2),{'r':dict(owner='u',token=1,expires=2)},"
         "dict(owner='u',token=2,expires=3),None]"),
    ),
    "W03": (
        ("折扣余数平局按行的输入顺序分配，退款金额逐单位守恒",
         "from invoice import settle\n"
         "assert settle([('y',2,3),('x',2,3)],1,[('a',[('y',1)]),('b',[('y',1)]),('c',[('x',2)])])"
         " == (11,[3,2,6],0)"),
        ("退款原子拒绝，失败 ID 可重试，成功重放不重复扣减",
         "from invoice import settle\n"
         "assert settle([('p',3,2)],1,[('r',[('p',1),('x',1)]),('r',[('p',1)]),"
         "('r',[('p',3)]),('s',[('p',2)])]) == (5,[None,2,2,3],0)"),
    ),
}
