"""Eight original tasks reserved for checking the frozen high stop rule."""

from paired_cases import Case


CASES = {
    "Q01": Case(
        "异步并发限流与取消交接",
        """修复 limiter.Limiter(limit) 的 await run(factory)。factory 为无参协程工厂。
同一实例最多有 limit 个 factory 同时运行，排队者按抵达顺序启动。
已排队者取消后永不启动。已运行者取消时，把取消传给自己的 factory，
并等其取消清理真正结束再让下一位开始。普通异常也要释放名额并传给调用者。
如果队首刚获得许可、尚未开始执行就被取消，也必须交还许可，不可饿死下一位。
limit 为正整数，保留现有类及入口。""",
        {"limiter.py": """
            class Limiter:
                def __init__(self, limit):
                    self.limit = limit
                async def run(self, factory):
                    return await factory()
            """},
        {"limiter.py": """
            import asyncio

            class Limiter:
                def __init__(self, limit):
                    self.slots = asyncio.Semaphore(limit)
                async def run(self, factory):
                    async with self.slots:
                        return await factory()
            """},
        """
        import asyncio
        from limiter import Limiter

        async def check():
            limiter = Limiter(1)
            events = []
            release = asyncio.Event()
            async def work(i):
                events.append(i)
                if i == 0: await release.wait()
                return i
            tasks = [asyncio.create_task(limiter.run(lambda i=i: work(i))) for i in range(4)]
            await asyncio.sleep(0)
            assert events == [0]
            release.set()
            assert await asyncio.gather(*tasks) == events == [0,1,2,3]
        asyncio.run(asyncio.wait_for(check(), 2))
        """,
        """
        import asyncio
        from limiter import Limiter

        async def check():
            async def bad(): raise LookupError('original')
            limiter = Limiter(1)
            try: await limiter.run(bad)
            except LookupError as e: assert str(e) == 'original'
            else: raise AssertionError('factory exception swallowed')
            assert await limiter.run(lambda: asyncio.sleep(0, result=9)) == 9
        asyncio.run(asyncio.wait_for(check(), 2))
        """,
        """
        import asyncio
        from limiter import Limiter

        async def cancelled(task):
            try: await task
            except asyncio.CancelledError: return
            raise AssertionError('cancellation did not reach caller')

        async def check():
            limiter = Limiter(1)
            release = asyncio.Event()
            reached = []
            async def old():
                await release.wait()
                # Callback runs after release of the permit but before the granted waiter.
                asyncio.get_running_loop().call_soon(granted.cancel)
            async def never(): reached.append('cancelled job')
            async def next_job(): reached.append('next')
            active = asyncio.create_task(limiter.run(old))
            granted = asyncio.create_task(limiter.run(never))
            queued = asyncio.create_task(limiter.run(never))
            successor = asyncio.create_task(limiter.run(next_job))
            await asyncio.sleep(0)
            queued.cancel()
            await cancelled(queued)
            release.set()
            await active
            await cancelled(granted)
            await successor
            assert reached == ['next']

            release_cleanup = asyncio.Event()
            started_cleanup = asyncio.Event()
            async def cleaning():
                try: await asyncio.Future()
                finally:
                    started_cleanup.set()
                    await release_cleanup.wait()
            active = asyncio.create_task(limiter.run(cleaning))
            await asyncio.sleep(0)
            waiting = asyncio.create_task(limiter.run(next_job))
            active.cancel()
            await started_cleanup.wait()
            await asyncio.sleep(0)
            assert not waiting.done() and reached == ['next']
            release_cleanup.set()
            await cancelled(active)
            await waiting
            assert reached == ['next','next']
        asyncio.run(asyncio.wait_for(check(), 2))
        """,
    ),
    "Q02": Case(
        "租约队列与过期令牌",
        """修复 leases.Queue。submit(id, payload) 首次登记任务返回 True，重复 ID（含已确认）返回 False。
claim(now, ttl) 领取最早提交的待处理任务，返回 (id, payload, token)，无任务返回 None。ttl 为正整数。
到期时刻 now>=deadline，原任务按首次提交顺序重新可领取。token 在同一任务每次领取时不同。
ack(id, token, now) 与 release(id, token, now) 只接受当前未过期的匹配令牌，成功返回 True；
ack 永久完成，release 立刻恢复待处理。无效令牌不改变其他任务的状态。now 单调不减，payload 可为 None。""",
        {"leases.py": """
            class Queue:
                def __init__(self):
                    self.rows = {}
                def submit(self, id, payload):
                    self.rows[id] = payload
                    return True
                def claim(self, now, ttl):
                    return None
                def ack(self, id, token, now):
                    return self.rows.pop(id, None) is not None
                def release(self, id, token, now):
                    return id in self.rows
            """},
        {"leases.py": """
            class Queue:
                def __init__(self):
                    self.rows = {}
                    self.done = set()
                    self.generation = 0
                def submit(self, id, payload):
                    if id in self.rows or id in self.done:
                        return False
                    self.rows[id] = payload, None, None
                    return True
                def claim(self, now, ttl):
                    for id, (payload, token, deadline) in self.rows.items():
                        if deadline is None or deadline <= now:
                            self.generation += 1
                            self.rows[id] = payload, self.generation, now + ttl
                            return id, payload, self.generation
                def _live(self, id, token, now):
                    return id in self.rows and self.rows[id][2] is not None and (
                        token == self.rows[id][1] and now < self.rows[id][2]
                    )
                def ack(self, id, token, now):
                    if not self._live(id, token, now): return False
                    del self.rows[id]
                    self.done.add(id)
                    return True
                def release(self, id, token, now):
                    if not self._live(id, token, now): return False
                    self.rows[id] = self.rows[id][0], None, None
                    return True
            """},
        """
        from leases import Queue
        q = Queue()
        assert q.submit('a', 4) and not q.submit('a', 5)
        a, payload, token = q.claim(0, 10)
        assert (a, payload) == ('a', 4)
        assert q.claim(1, 10) is None
        assert q.ack(a, token, 2)
        """,
        """
        from leases import Queue
        q = Queue()
        for k in ['a','b']: assert q.submit(k, k)
        a = q.claim(0, 2)
        b = q.claim(1, 2)
        assert (a[0], b[0]) == ('a','b')
        again = q.claim(2, 2)
        assert again[:2] == a[:2] and again[2] != a[2]
        assert not q.ack('a', a[2], 2)
        """,
        """
        from leases import Queue
        q = Queue()
        assert q.submit('a', None) and q.submit('b', False) and q.submit('c', 0)
        _, _, first = q.claim(3, 2)
        b = q.claim(3, 8)
        assert b[:2] == ('b', False)
        assert not q.release('a', first, 5)
        a = q.claim(5, 2)
        assert a[:2] == ('a', None)
        assert not q.ack('a', first, 5)
        assert q.release('a', a[2], 5)
        a2 = q.claim(5, 1)
        assert a2[:2] == a[:2] and len({a2[2], a[2], first}) == 3
        assert not q.release('a', a[2], 5)
        assert q.ack('a', a2[2], 5)
        assert not q.submit('a', 'must stay done')
        assert q.claim(5, 1)[:2] == ('c', 0)
        assert not q.ack('b', b[2], 11)
        assert q.claim(11, 1)[:2] == ('b', False)
        """,
    ),
    "Q03": Case(
        "优先级区间叠加与空洞",
        """修复 overlay.resolve(records)，返回按起点排序的 (start,end,value) 元组列表。
每条记录 (start,end,priority,value) 为整数半开区间，start<=end，可负坐标，零宽忽略。
任意位置由最高 priority 的记录胜出，同优先级时输入中靠后的记录胜出。
value 为字符串或 None，None 表示挖空，覆盖低优先级记录且自身不输出。
仅合并相邻且可见值相同的区间。保留输入，不能逐坐标遍历，跨度可达 10**12。""",
        {"overlay.py": """
            def resolve(records):
                return [(a,b,v) for a,b,_,v in sorted(records) if a < b and v is not None]
            """},
        {"overlay.py": """
            from collections import defaultdict
            from heapq import heappop, heappush

            def resolve(records):
                starts = defaultdict(list)
                points = set()
                for i,(a,b,p,v) in enumerate(records):
                    if a == b: continue
                    points.update((a,b))
                    starts[a].append((-p,-i,b,v))
                heap, result = [], []
                coords = sorted(points)
                for a,b in zip(coords, coords[1:]):
                    for row in starts[a]: heappush(heap, row)
                    while heap and heap[0][2] <= a: heappop(heap)
                    if not heap or heap[0][3] is None: continue
                    v = heap[0][3]
                    if result and result[-1][1:] == (a,v):
                        result[-1] = result[-1][0],b,v
                    else:
                        result.append((a,b,v))
                return result
            """},
        """
        from overlay import resolve
        assert resolve([(0,10,0,'a'),(2,4,1,'b')]) == [(0,2,'a'),(2,4,'b'),(4,10,'a')]
        """,
        """
        from overlay import resolve
        assert resolve([(-2,0,3,'a'),(0,3,1,'a'),(3,3,9,'z')]) == [(-2,3,'a')]
        """,
        """
        from overlay import resolve
        from random import Random
        assert resolve([(0,10,2,'a'),(3,5,2,None),(5,7,2,'b'),(7,9,2,'a')]) == [
            (0,3,'a'),(5,7,'b'),(7,10,'a')]
        assert resolve([(-10**12,10**12,-1,''),(2,3,9,None)]) == [(-10**12,2,''),(3,10**12,'')]
        rng = Random(60221)
        for i in range(130):
            src = [(a := rng.randrange(-9,9), a+rng.randrange(6), rng.randrange(-2,3),
                    rng.choice([None,'','a','b'])) for _ in range(rng.randrange(17))]
            old, expected = src[:], []
            for x in range(-9,15):
                active = [(p,j,v) for j,(a,b,p,v) in enumerate(src) if a<=x<b]
                if not active or (v:=max(active)[2]) is None: continue
                if expected and expected[-1][1:] == (x,v): expected[-1] = expected[-1][0],x+1,v
                else: expected.append((x,x+1,v))
            assert resolve(src) == expected and src == old, (i,src,expected)
        """,
    ),
    "Q04": Case(
        "增量 CSV 状态机与引号边界",
        """修复 records.Reader 的 feed(text)->list[tuple[str,...]] 和 finish()->list[tuple[str,...]]。
支持逗号、LF 或 CRLF 行结束，双引号只可在字段开头，内部双引号用两个引号转义。
引号内逗号和换行原样保留；引号闭合后只允许逗号、行结束或 EOF。
引号外 CR 必须接 LF。输入按任意字符切分，结尾可无换行。空输入无记录，空行是一个空字段。
发现非法输入抛 ValueError，实例永久失败。finish 成功后重复调用返回 []，之后 feed（含空串）抛 ValueError。""",
        {"records.py": """
            class Reader:
                def __init__(self):
                    self.buffer = ''
                def feed(self,text):
                    chunks = (self.buffer+text).split('\\n')
                    self.buffer = chunks.pop()
                    return [tuple(line.split(',')) for line in chunks]
                def finish(self):
                    return [tuple(self.buffer.split(','))] if self.buffer else []
            """},
        {"records.py": """
            class Reader:
                def __init__(self):
                    self.field = []
                    self.row = []
                    self.state = 'start'
                    self.bad = self.closed = False
                def _fail(self):
                    self.bad = True
                    raise ValueError('invalid csv')
                def _end_field(self):
                    self.row.append(''.join(self.field))
                    self.field.clear()
                    self.state = 'start'
                def _emit(self, out):
                    self._end_field()
                    out.append(tuple(self.row))
                    self.row.clear()
                def feed(self, text):
                    if self.bad or self.closed: raise ValueError('reader unavailable')
                    out = []
                    for char in text:
                        if self.state == 'cr':
                            if char != '\\n': self._fail()
                            self._emit(out)
                        elif self.state == 'quoted':
                            if char == '"': self.state = 'quote'
                            else: self.field.append(char)
                        elif char == '"' and self.state == 'quote':
                            self.field.append(char)
                            self.state = 'quoted'
                        elif char == ',':
                            self._end_field()
                        elif char == '\\n':
                            self._emit(out)
                        elif char == '\\r':
                            self.state = 'cr'
                        elif char == '"' and self.state == 'start':
                            self.state = 'quoted'
                        elif char == '"' or self.state == 'quote':
                            self._fail()
                        else:
                            self.field.append(char)
                            self.state = 'plain'
                    return out
                def finish(self):
                    if self.bad or self.state in ('quoted','cr'): self._fail()
                    if self.closed: return []
                    self.closed = True
                    out = []
                    if self.state != 'start' or self.row: self._emit(out)
                    return out
            """},
        """
        from records import Reader
        r = Reader()
        assert r.feed('a,"b,c"\\n') == [('a','b,c')]
        assert r.finish() == []
        """,
        """
        from records import Reader
        r = Reader()
        assert r.feed('a,b\\r') == []
        assert r.feed('\\nc') == [('a','b')]
        assert r.finish() == [('c',)]
        """,
        """
        from records import Reader
        from random import Random
        r = Reader()
        wire = '"a""b","x\\r\\ny"\\r\\n\\n"雪",'
        expected = [('a"b','x\\r\\ny'),('',),('雪','')]
        for seed in range(30):
            r, got, rng = Reader(), [], Random(seed)
            i=0
            while i < len(wire):
                n=rng.randrange(1,5)
                got.extend(r.feed(wire[i:i+n]))
                i+=n
            assert got+r.finish() == expected
            assert r.finish() == []
            try: r.feed('')
            except ValueError: pass
            else: raise AssertionError('feed after finish')
        for text, at_eof in [('ab"c',False),('"x" ',False),('a\\rx',False),('a\\r',True),('"a',True)]:
            r=Reader()
            try:
                r.feed(text)
                if at_eof: r.finish()
            except ValueError: pass
            else: raise AssertionError((text,'accepted'))
            try: r.feed('a\\n')
            except ValueError: pass
            else: raise AssertionError('accepted after invalid input')
        """,
    ),
    "Q05": Case(
        "乱序库存快照与删除版本",
        """修复 inventory.Inventory 的 apply(events) 和 snapshot()。每条事件是
(sku, warehouse, revision, quantity)，revision 为非负整数，quantity 为非负整数或 None（删除）。
每个 (sku,warehouse) 只接受严格更新的 revision，revision 相同时保留首次看到的事件。
删除也保留版本屏障，旧事件不可复活；更新版本可重建，0 数量仍是存在的记录。
snapshot() 返回 {sku: {warehouse: quantity}} 的独立拷贝，空 sku 不出现。
apply 可多次调用，events 可为一次性迭代器，返回本次实际接受的事件数。""",
        {"inventory.py": """
            class Inventory:
                def __init__(self):
                    self.rows = {}
                def apply(self, events):
                    count = 0
                    for sku, site, rev, qty in events:
                        self.rows.setdefault(sku, {})[site] = qty
                        count += 1
                    return count
                def snapshot(self):
                    return self.rows
            """},
        {"inventory.py": """
            class Inventory:
                def __init__(self):
                    self.rows = {}
                def apply(self, events):
                    count = 0
                    for sku, site, rev, qty in events:
                        key = sku, site
                        if key not in self.rows or rev > self.rows[key][0]:
                            self.rows[key] = rev, qty
                            count += 1
                    return count
                def snapshot(self):
                    result = {}
                    for (sku, site), (_, qty) in self.rows.items():
                        if qty is not None:
                            result.setdefault(sku, {})[site] = qty
                    return result
            """},
        """
        from inventory import Inventory
        s = Inventory()
        assert s.apply([('a','x',2,10),('a','x',1,12)]) == 1
        assert s.snapshot() == {'a':{'x':10}}
        """,
        """
        from inventory import Inventory
        s = Inventory()
        assert s.apply(iter([('a','x',1,5),('a','x',2,None)])) == 2
        assert s.snapshot() == {}
        assert s.apply([('a','x',3,0)]) == 1
        assert s.snapshot() == {'a':{'x':0}}
        """,
        """
        from random import Random
        from inventory import Inventory
        rng, s, history = Random(7474), Inventory(), []
        for i in range(200):
            events = [(str(rng.randrange(4)),str(rng.randrange(7)),rng.randrange(22),
                       rng.choice([None,None,0,1,2])) for _ in range(rng.randrange(15))]
            expected_count = 0
            for event in events:
                old = [e[2] for e in history if e[:2] == event[:2]]
                expected_count += not old or event[2] > max(old)
                history.append(event)
            assert s.apply(iter(events)) == expected_count, i
            expected = {}
            for sku, site in {e[:2] for e in history}:
                row = max((e for e in history if e[:2] == (sku,site)), key=lambda e:e[2])
                if row[3] is not None: expected.setdefault(sku,{})[site] = row[3]
            result = s.snapshot()
            assert result == expected, i
            for nested in result.values(): nested.clear()
            result['mutated'] = {}
            assert s.snapshot() == expected
        """,
    ),
    "Q06": Case(
        "配置树合并与两级拷贝边界",
        """修复 config.merge(layers)。从低到高合并可一次性迭代的字典层。
键为字符串，值可以是字典、列表、None、标量或 config.DELETE。两个字典递归合并；
若新值为字典而原值不是字典，从空字典开始合并。DELETE 只在字典键对应位置表示删除；
删除缺失键为空操作。列表整体替换，列表内的字典与 DELETE 都作为字面数据完整保留。
None 是普通值。返回值中的所有字典和列表与输入独立，重复调用不可污染输入或以前的结果。
保留 config.DELETE 单例身份。没有循环引用。""",
        {"config.py": """
            DELETE = object()
            def merge(layers):
                out = {}
                for layer in layers:
                    out.update(layer)
                return out
            """},
        {"config.py": """
            DELETE = object()
            def _copy(value):
                if isinstance(value, dict): return {k:_copy(v) for k,v in value.items()}
                if isinstance(value, list): return [_copy(v) for v in value]
                return value
            def _merge(dst, src):
                for key, value in src.items():
                    if value is DELETE: dst.pop(key,None)
                    elif isinstance(value,dict):
                        base = dst.get(key)
                        if not isinstance(base,dict): base = {}
                        dst[key] = base
                        _merge(base,value)
                    else: dst[key] = _copy(value)
            def merge(layers):
                out = {}
                for layer in layers: _merge(out,layer)
                return out
            """},
        """
        from config import DELETE, merge
        a={'net':{'host':'local','port':9}}
        b={'net':{'port':DELETE,'tls':True}}
        assert merge(iter([a,b])) == {'net':{'host':'local','tls':True}}
        """,
        """
        from config import merge
        a={'cache':[1,2],'known':'old'}
        assert merge([a,{'cache':[3],'known':None}]) == {'cache':[3],'known':None}
        """,
        """
        from config import DELETE, merge
        layer={'top':{'x':[{'sentinel':DELETE,'nested':[1]}]},'scalar':False}
        patch={'top':{'gone':DELETE},'scalar':{'deleted':DELETE}}
        first = merge(iter([layer,patch]))
        assert first == {'top':{'x':[{'sentinel':DELETE,'nested':[1]}]},'scalar':{}}
        assert first['top']['x'][0]['sentinel'] is DELETE
        second = merge([first,{'top':{'more':[first['top']['x']]}}])
        second['top']['x'][0]['nested'].append(5)
        second['top']['more'][0][0]['nested'].append(7)
        assert first['top']['x'][0]['nested'] == [1]
        assert layer['top']['x'][0]['nested'] == [1]
        assert merge([{'a':{'x':3}},{'a':None},{'a':{'x':DELETE}}]) == {'a':{}}
        assert merge([{'a':[{'keep':DELETE}]}])['a'][0]['keep'] is DELETE
        """,
    ),
    "Q07": Case(
        "双资源选择与稳定并列",
        """修复 allocation.choose(items, money, weight)。items 中每项是
(唯一字符串 ID, 费用, 重量, 价值)，各数字为整数，费用与重量非负，价值可负。
在两个预算内选子集，每项最多一次。按价值最大、费用最少、重量最少、项目数最少、排序后的 ID 元组字典序最小择优。
返回 (价值,费用,重量,排序后的 ID 元组)，空集合法。输入不变。约 160 项、两个预算各 35，不能穷举子集。""",
        {"allocation.py": """
            def choose(items, money, weight):
                chosen = []
                value = cost = mass = 0
                for ident,c,w,v in items:
                    if v > 0 and cost+c <= money and mass+w <= weight:
                        value,cost,mass = value+v,cost+c,mass+w
                        chosen.append(ident)
                return value,cost,mass,tuple(sorted(chosen))
            """},
        {"allocation.py": """
            def choose(items, money, weight):
                best = {(0,0):(0,0,())}
                for ident,c,w,v in sorted(items):
                    new = best.copy()
                    for (cost,mass), (value, n, ids) in best.items():
                        key = cost+c,mass+w
                        if key[0] > money or key[1] > weight: continue
                        candidate = value-v,n+1,ids+(ident,)
                        if key not in new or candidate < new[key]: new[key]=candidate
                    best = new
                value,cost,mass,_,ids = min((v,c,w,n,ids) for (c,w),(v,n,ids) in best.items())
                return -value,cost,mass,ids
            """},
        """
        from allocation import choose
        assert choose([('a',2,2,4),('b',2,0,3),('c',0,2,3)],2,2) == (6,2,2,('b','c'))
        """,
        """
        from allocation import choose
        assert choose([('u',0,0,5),('x',0,0,0),('v',0,1,3)],0,0) == (5,0,0,('u',))
        assert choose([],0,0) == (0,0,0,())
        """,
        """
        from allocation import choose
        from random import Random
        rng = Random(518)
        for i in range(100):
            items = [(str(j),rng.randrange(4),rng.randrange(4),rng.randrange(-1,4)) for j in range(rng.randrange(11))]
            rng.shuffle(items)
            money, weight = rng.randrange(7), rng.randrange(7)
            expected = (0,0,0,0,())
            for mask in range(1<<len(items)):
                rows = [x for j,x in enumerate(items) if mask >> j & 1]
                c, w = sum(x[1] for x in rows), sum(x[2] for x in rows)
                if c <= money and w <= weight:
                    expected = min(expected,(-sum(x[3] for x in rows),c,w,len(rows),tuple(sorted(x[0] for x in rows))))
            copy = items[:]
            assert choose(items,money,weight) == (-expected[0],expected[1],expected[2],expected[4]), (i,items,expected)
            assert copy == items
        items = [(f'{i:03}',1,1,1) for i in range(159,-1,-1)]
        assert choose(items,35,35) == (35,35,35,tuple(f'{i:03}' for i in range(35)))
        """,
    ),
    "Q08": Case(
        "依赖重建、边更新与稳定顺序",
        """修复 rebuild.Registry。replace(name, deps) 替换这个组件的全部直接依赖；
重复依赖去重，名称为字符串。affected(changed) 返回从 changed 沿反向依赖可达的组件及 changed，
每个组件一次，且在受影响子图中依赖先于消费者。每次选择当前可输出的字典序最小名称。
未登记的名字可作为叶子或 changed；没有受影响项返回 []。仅当受影响子图含环时抛 ValueError。
changed 和 deps 可以是一次性迭代器。调用失败后对象继续可用。""",
        {"rebuild.py": """
            class Registry:
                def __init__(self):
                    self.rows = {}
                def replace(self,name,deps):
                    self.rows.setdefault(name,set()).update(deps)
                def affected(self,changed):
                    return sorted(set(changed))
            """},
        {"rebuild.py": """
            from collections import defaultdict
            from heapq import heapify, heappop, heappush
            class Registry:
                def __init__(self):
                    self.rows = {}
                def replace(self,name,deps):
                    self.rows[name] = set(deps)
                def affected(self,changed):
                    consumers = defaultdict(set)
                    for name,deps in self.rows.items():
                        for dep in deps: consumers[dep].add(name)
                    active = set(changed)
                    todo = list(active)
                    while todo:
                        for name in consumers[todo.pop()]:
                            if name not in active:
                                active.add(name)
                                todo.append(name)
                    incoming = {name:len(self.rows.get(name,set()) & active) for name in active}
                    ready = [name for name,count in incoming.items() if not count]
                    heapify(ready)
                    out = []
                    while ready:
                        name = heappop(ready)
                        out.append(name)
                        for consumer in consumers[name] & active:
                            incoming[consumer] -= 1
                            if not incoming[consumer]: heappush(ready,consumer)
                    if len(out) != len(active): raise ValueError('cycle')
                    return out
            """},
        """
        from rebuild import Registry
        g=Registry()
        g.replace('app',['lib'])
        g.replace('lib',['source'])
        assert g.affected(['source']) == ['source','lib','app']
        """,
        """
        from rebuild import Registry
        g=Registry()
        g.replace('app',['old'])
        g.replace('app',iter(['new','new']))
        assert g.affected(iter(['old'])) == ['old']
        assert g.affected(['new']) == ['new','app']
        """,
        """
        from rebuild import Registry
        from random import Random
        g=Registry()
        g.replace('cycle-a',['cycle-b'])
        g.replace('cycle-b',['cycle-a'])
        g.replace('b',['z'])
        g.replace('a',['b','y'])
        assert g.affected(['z','y','other','y']) == ['other','y','z','b','a']
        try: g.affected(['cycle-a','z'])
        except ValueError: pass
        else: raise AssertionError('cycle accepted')
        g.replace('cycle-b',[])
        assert g.affected(['cycle-b']) == ['cycle-b','cycle-a']
        rng = Random(2219)
        names = list('abcdefgh')
        deps = {}
        g = Registry()
        for _ in range(100):
            node = rng.choice(names)
            children = set(rng.sample(names,rng.randrange(4)))
            g.replace(node, iter(children))
            deps[node] = children
            changed = rng.sample(names+['leaf'],rng.randrange(4))
            impacted = set(changed)
            while True:
                new = impacted | {k for k,d in deps.items() if d & impacted}
                if new == impacted: break
                impacted = new
            expected, pending = [], impacted.copy()
            while ready := sorted(n for n in pending if not deps.get(n,set()) & pending):
                expected.append(ready[0])
                pending.remove(ready[0])
            if pending:
                try: g.affected(iter(changed))
                except ValueError: pass
                else: raise AssertionError((deps,changed))
            else:
                assert g.affected(iter(changed)) == expected, (deps,changed,expected)
        """,
    ),
}
