"""Original fixtures and disjoint gates for the paired high experiment."""

from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class Case:
    title: str
    prompt: str
    files: dict
    fixed: dict
    public: str
    regression: str
    hidden: str


def clean(code):
    return dedent(code).lstrip("\n")


CASES = {
    "P01": Case(
        "异步请求合并与失效隔离",
        """修复 fetcher.Fetcher。await get(key, load) 调用无参数协程工厂 load。
同一 key 的并发 get 共享一次加载，并缓存成功值（含 None）。一个等待者取消不可取消共享加载。
异常传给等待者，后续 get 可重试。同步 invalidate(key) 删除缓存并使旧加载脱离当前代；
旧等待者仍能拿到旧结果，但失效后来的请求用新工厂，旧加载完成或异常都不能覆盖或移除新代。
不同 key 独立，不使用时间判断。保留 Fetcher 的现有入口。""",
        {"fetcher.py": """
            from store import Values

            class Fetcher:
                def __init__(self):
                    self.values = Values()

                async def get(self, key, load):
                    if key not in self.values.data:
                        self.values.data[key] = await load()
                    return self.values.data[key]

                def invalidate(self, key):
                    self.values.data.pop(key, None)
            """, "store.py": """
            class Values:
                def __init__(self):
                    self.data = {}
            """},
        {"fetcher.py": """
            import asyncio
            from store import Values

            class Fetcher:
                def __init__(self):
                    self.values = Values()
                    self.pending = {}

                async def get(self, key, load):
                    if key in self.values.data:
                        return self.values.data[key]
                    task = self.pending.get(key)
                    if task is None:
                        task = asyncio.create_task(self._load(key, load))
                        self.pending[key] = task
                        task.add_done_callback(self._observe)
                    return await asyncio.shield(task)

                @staticmethod
                def _observe(task):
                    if not task.cancelled():
                        task.exception()

                async def _load(self, key, load):
                    task = asyncio.current_task()
                    try:
                        value = await load()
                        if self.pending.get(key) is task:
                            self.values.data[key] = value
                        return value
                    finally:
                        if self.pending.get(key) is task:
                            del self.pending[key]

                def invalidate(self, key):
                    self.values.data.pop(key, None)
                    self.pending.pop(key, None)
            """},
        """
        import asyncio
        from fetcher import Fetcher

        async def main():
            calls = 0
            release = asyncio.Event()
            async def load():
                nonlocal calls
                calls += 1
                await release.wait()
                return None
            f = Fetcher()
            a = asyncio.create_task(f.get('x', load))
            b = asyncio.create_task(f.get('x', load))
            for _ in range(4): await asyncio.sleep(0)
            release.set()
            assert await asyncio.gather(a, b) == [None, None]
            assert await f.get('x', load) is None and calls == 1
        asyncio.run(asyncio.wait_for(main(), 2))
        """,
        """
        import asyncio
        from fetcher import Fetcher

        async def main():
            f = Fetcher()
            entered, release = asyncio.Event(), asyncio.Event()
            async def old():
                entered.set()
                await release.wait()
                return 1
            async def new(): return 2
            a = asyncio.create_task(f.get('x', old))
            await entered.wait()
            f.invalidate('x')
            assert await f.get('x', new) == 2
            release.set()
            assert await a == 1
            assert await f.get('x', old) == 2
        asyncio.run(asyncio.wait_for(main(), 2))
        """,
        """
        import asyncio
        from fetcher import Fetcher

        async def main():
            f = Fetcher()
            start, fail, fresh_started, fresh = (asyncio.Event() for _ in range(4))
            loads = []
            async def obsolete():
                loads.append('old')
                start.set()
                await fail.wait()
                raise LookupError('old')
            async def loader():
                loads.append('new')
                fresh_started.set()
                await fresh.wait()
                return False
            async def unexpected(): raise AssertionError('duplicated load')
            x = asyncio.create_task(f.get('same', obsolete))
            await start.wait()
            y = asyncio.create_task(f.get('same', unexpected))
            await asyncio.sleep(0)
            x.cancel()
            try: await x
            except asyncio.CancelledError: pass
            else: raise AssertionError('cancel did not propagate')
            f.invalidate('same')
            z = asyncio.create_task(f.get('same', loader))
            await fresh_started.wait()
            assert await f.get('other', lambda: asyncio.sleep(0, result=0)) == 0
            fail.set()
            try: await y
            except LookupError as exc: assert str(exc) == 'old'
            else: raise AssertionError('old exception missing')
            follow = asyncio.create_task(f.get('same', unexpected))
            await asyncio.sleep(0)
            fresh.set()
            assert await asyncio.gather(z, follow) == [False, False]
            assert loads == ['old', 'new']
            assert await f.get('same', unexpected) is False
            async def bad(): raise ValueError('retry')
            try: await f.get('broken', bad)
            except ValueError: pass
            else: raise AssertionError('failure missing')
            assert await f.get('broken', lambda: asyncio.sleep(0, result=7)) == 7
        asyncio.run(asyncio.wait_for(main(), 2))
        """,
    ),
    "P02": Case(
        "字节流分帧与错误状态",
        """修复 decoder.Decoder(max_bytes) 的 feed(chunk: bytes)->list[str] 与 finish()->None。
帧格式为十进制字节数、冒号、恰好该长度的 UTF-8 内容、换行。0 合法，其余长度不能有前导零，
长度不得超过 max_bytes。内容允许换行或冒号，长度按字节。feed 可任意切分、可一次返回多帧。
非法字节、超长、错误终止符或 finish 时存在残片，抛 ValueError 并将实例置为失败状态，后续操作也抛 ValueError。
成功 finish 后可重复 finish，之后 feed 抛 ValueError。空 feed 合法。不要按换行切分帧。""",
        {"decoder.py": """
            from header import parse

            class Decoder:
                def __init__(self, max_bytes):
                    self.limit = max_bytes
                    self.buffer = b''

                def feed(self, chunk):
                    self.buffer += chunk
                    lines = self.buffer.split(b'\\n')
                    self.buffer = lines.pop()
                    return [line.split(b':', 1)[1].decode() for line in lines]

                def finish(self):
                    if self.buffer:
                        raise ValueError('incomplete')
            """, "header.py": """
            def parse(raw):
                return int(raw)
            """},
        {"decoder.py": """
            class Decoder:
                def __init__(self, max_bytes):
                    self.limit = max_bytes
                    self.size = 0
                    self.digits = 0
                    self.payload = bytearray()
                    self.in_payload = False
                    self.failed = False
                    self.closed = False

                def _fail(self):
                    self.failed = True
                    raise ValueError('invalid frame')

                def feed(self, chunk):
                    if self.closed or self.failed:
                        raise ValueError('closed decoder')
                    frames = []
                    for byte in chunk:
                        if not self.in_payload:
                            if byte == 58 and self.digits:
                                self.in_payload = True
                            elif 48 <= byte <= 57 and (not self.digits or self.size):
                                self.digits += 1
                                self.size = self.size * 10 + byte - 48
                                if self.size > self.limit: self._fail()
                            else:
                                self._fail()
                        elif len(self.payload) < self.size:
                            self.payload.append(byte)
                        else:
                            if byte != 10: self._fail()
                            try: frames.append(self.payload.decode('utf-8'))
                            except UnicodeDecodeError: self._fail()
                            self.payload.clear()
                            self.digits = self.size = 0
                            self.in_payload = False
                    return frames

                def finish(self):
                    if self.failed or self.digits:
                        self._fail()
                    self.closed = True
            """},
        """
        from decoder import Decoder
        d = Decoder(12)
        assert d.feed(b'4:a\\n') == []
        assert d.feed(b':b\\n0:\\n') == ['a\\n:b', '']
        d.finish()
        """,
        """
        from decoder import Decoder
        d = Decoder(3)
        wire = b'3:' + '雪'.encode() + b'\\n'
        assert sum((d.feed(bytes([x])) for x in wire), []) == ['雪']
        d.finish()
        d.finish()
        try: d.feed(b'')
        except ValueError: pass
        else: raise AssertionError('feed accepted after finish')
        """,
        """
        from decoder import Decoder
        from random import Random
        rng = Random(818)
        chars = ['雪', '🌿', ':', '\\n', 'a', '\\0']
        for _ in range(75):
            values = [''.join(rng.choices(chars, k=rng.randrange(8))) for _ in range(rng.randrange(12))]
            frames = [s.encode() for s in values]
            data = b''.join(str(len(x)).encode() + b':' + x + b'\\n' for x in frames)
            d = Decoder(28)
            actual = []
            while data:
                n = rng.randrange(1, 12)
                assert d.feed(b'') == []
                actual.extend(d.feed(data[:n]))
                data = data[n:]
            d.finish()
            assert actual == values
        def fails(action):
            try: action()
            except ValueError: return
            raise AssertionError('invalid frame accepted')
        for data in (b'00:\\n', b':\\n', b'-1:a\\n', b'9', b'1:a!', b'1:\\xff\\n', b'0:\\n1:b!'):
            d = Decoder(4)
            fails(lambda: d.feed(data))
            fails(lambda: d.feed(b'0:\\n'))
            fails(d.finish)
        for data in (b'2', b'2:', b'2:a', b'2:aa'):
            d = Decoder(4)
            assert d.feed(data) == []
            fails(d.finish)
            fails(lambda: d.feed(b'0:\\n'))
        """,
    ),
    "P03": Case(
        "带 TTL 的标签缓存索引",
        """修复 catalog.Catalog 的双向标签缓存。put(key,value,tags,ttl,now) 覆盖记录及旧标签，标签去重；
ttl 为非负整数，截止 now+ttl，到期时刻即不可见。get(key,now) 缺失或过期抛 KeyError，
None 和其他假值均可缓存。invalidate(tag,now) 删除所有带该标签的记录，返回按 key 排序的未过期删除项；
被删记录必须从所有标签中移除。sweep(now) 删除并返回按 key 排序的全部过期记录。
过期项若已被 get 或 invalidate 删除，sweep 不重复报告。调用 now 单调不减。
保留按标签定位的反向索引，invalidate 不扫描整个缓存。键与标签均为字符串。返回类型为 tuple。""",
        {"catalog.py": """
            from tag_index import TagIndex

            class Catalog:
                def __init__(self):
                    self.rows = {}
                    self.tags = TagIndex()

                def put(self, key, value, tags, ttl, now):
                    self.rows[key] = value, now + ttl
                    self.tags.add(key, tags)

                def get(self, key, now):
                    return self.rows[key][0]

                def invalidate(self, tag, now):
                    keys = tuple(sorted(self.tags.index.get(tag, ())))
                    for key in keys: self.rows.pop(key, None)
                    return keys

                def sweep(self, now):
                    return ()
            """, "tag_index.py": """
            class TagIndex:
                def __init__(self):
                    self.index = {}

                def add(self, key, tags):
                    for tag in tags: self.index.setdefault(tag, set()).add(key)
            """},
        {"catalog.py": """
            from tag_index import TagIndex

            class Catalog:
                def __init__(self):
                    self.rows = {}
                    self.tags = TagIndex()

                def put(self, key, value, tags, ttl, now):
                    labels = set(tags)
                    self._remove(key)
                    self.rows[key] = value, now + ttl, labels
                    self.tags.add(key, labels)

                def _remove(self, key):
                    row = self.rows.pop(key, None)
                    if row is not None:
                        for tag in row[2]:
                            keys = self.tags.index[tag]
                            keys.remove(key)
                            if not keys: del self.tags.index[tag]

                def get(self, key, now):
                    value, expiry, _ = self.rows[key]
                    if expiry <= now:
                        self._remove(key)
                        raise KeyError(key)
                    return value

                def invalidate(self, tag, now):
                    result = []
                    for key in sorted(self.tags.index.get(tag, ())):
                        if self.rows[key][1] > now: result.append(key)
                        self._remove(key)
                    return tuple(result)

                def sweep(self, now):
                    expired = sorted(k for k, row in self.rows.items() if row[1] <= now)
                    for key in expired: self._remove(key)
                    return tuple(expired)
            """},
        """
        from catalog import Catalog
        c = Catalog()
        c.put('a', 1, ['red'], 10, 0)
        c.put('a', None, ['green', 'green'], 2, 1)
        assert c.invalidate('red', 1) == ()
        assert c.get('a', 1) is None
        assert c.sweep(3) == ('a',)
        assert c.invalidate('green', 3) == ()
        """,
        """
        from catalog import Catalog
        c = Catalog()
        c.put('z', 0, iter(['a', 'b']), 4, 0)
        c.put('x', 1, ['b'], 1, 0)
        c.put('a', 2, ['a', 'b'], 8, 0)
        assert c.invalidate('b', 1) == ('a', 'z')
        assert c.invalidate('a', 1) == () and c.sweep(2) == ()
        c.put('x', False, ['b'], 0, 2)
        try: c.get('x', 2)
        except KeyError: pass
        else: raise AssertionError('zero TTL visible')
        assert c.sweep(2) == ()
        """,
        """
        from catalog import Catalog
        from random import Random
        rng, c, rows, now = Random(839), Catalog(), {}, 0
        for i in range(1600):
            now += rng.choice([0, 0, 0, 1])
            key, tag = str(rng.randrange(22)), str(rng.randrange(9))
            action = rng.choices(('put','get','invalidate','sweep'), (50, 23, 21, 6))[0]
            if action == 'put':
                tags = rng.choices(list('012345678'), k=rng.randrange(7))
                value, ttl = rng.choice([None, False, '', (i,)]), rng.randrange(15)
                c.put(key, value, iter(tags), ttl, now)
                rows[key] = value, set(tags), now + ttl
            elif action == 'get':
                row = rows.get(key)
                if row is None or row[2] <= now:
                    rows.pop(key, None)
                    try: c.get(key, now)
                    except KeyError: pass
                    else: raise AssertionError((i, key, 'expired or absent'))
                else:
                    assert c.get(key, now) is row[0], (i, key, row)
            else:
                removed = {k for k, row in rows.items() if (tag in row[1] if action == 'invalidate' else row[2] <= now)}
                expected = tuple(sorted(k for k in removed if action == 'sweep' or rows[k][2] > now))
                actual = c.invalidate(tag, now) if action == 'invalidate' else c.sweep(now)
                assert actual == expected, (i, action, tag, actual, expected)
                for k in removed: del rows[k]
        """,
    ),
    "P04": Case(
        "有序检查点与单次优惠的图优化",
        """修复 routes.plan(edges,start,goal,checkpoints)。edges 每项是 (唯一字符串 ID, 起点, 终点, 时间, 通行费)；
有向图，时间和费用均为非负整数。沿途必须按序完成 checkpoints，可回访，出发和到达节点也计入。
每次到访至多消费一个检查点，包括重复的相邻检查点。至多一次将经过边的通行费降为 0。
在满足条件的路径中按 (总时间, 折扣后总费用, 边数, 按行进顺序的边 ID 元组) 字典序取最小。
返回 (时间, 费用, 边 ID 元组)，不可达返回 None，输入不变。需处理零时间环，禁止枚举全部路径。""",
        {"routes.py": """
            from graph import outgoing

            def plan(edges, start, goal, checkpoints):
                for edge_id, a, b, time, toll in outgoing(edges).get(start, ()):
                    if b == goal: return time, toll, (edge_id,)
                return None
            """, "graph.py": """
            def outgoing(edges):
                adj = {}
                for edge in edges: adj.setdefault(edge[1], []).append(edge)
                return adj
            """},
        {"routes.py": """
            from heapq import heappop, heappush
            from itertools import count
            from graph import outgoing

            def plan(edges, start, goal, checkpoints):
                needed = tuple(checkpoints)
                graph = outgoing(edges)
                serial = count()

                def visit(node, index):
                    return index + int(index < len(needed) and node == needed[index])

                state = start, visit(start, 0), False
                zero = (0, 0, 0, ())
                best = {state: zero}
                heap = [(zero, next(serial), state)]
                while heap:
                    score, _, state = heappop(heap)
                    if score != best.get(state): continue
                    node, i, used = state
                    time, toll, hops, ids = score
                    if node == goal and i == len(needed):
                        return time, toll, ids
                    for ident, _, target, dt, cost in graph.get(node, ()):
                        for free in ((False,) if used else (False, True)):
                            state2 = target, visit(target, i), used or free
                            score2 = time + dt, toll + (0 if free else cost), hops + 1, ids + (ident,)
                            if state2 not in best or score2 < best[state2]:
                                best[state2] = score2
                                heappush(heap, (score2, next(serial), state2))
            """},
        """
        from routes import plan
        edges = [('direct','s','g',1,0), ('sc','s','c',1,9), ('cg','c','g',1,3)]
        assert plan(edges,'s','g',['c']) == (2,3,('sc','cg'))
        assert plan(edges,'s','g',['missing']) is None
        """,
        """
        from routes import plan
        edges = [('z','s','c',0,3), ('a','s','c',0,3), ('loop','c','c',0,0),
                 ('g','c','t',1,3), ('wait','c','s',0,0)]
        assert plan(edges,'s','t',['c','c']) == (1,3,('a','loop','g'))
        assert plan(edges,'s','s',['s']) == (0,0,())
        """,
        """
        from random import Random
        from routes import plan

        def oracle(edges, start, goal, needed):
            def visit(n, i):
                return i + (i < len(needed) and n == needed[i])
            nodes = set([start, goal] + list(needed) + [n for edge in edges for n in edge[1:3]])
            best = {(start, visit(start, 0), 0): (0,0,0,())}
            for _ in range(len(nodes) * (len(needed) + 1) * 2):
                updated = dict(best)
                for eid, u, v, time, cost in edges:
                    for (node, i, used), row in best.items():
                        if node != u: continue
                        for rebate in range(2 - used):
                            k = v, visit(v,i), used + rebate
                            score = row[0]+time, row[1]+cost*(1-rebate), row[2]+1, row[3]+(eid,)
                            if k not in updated or score < updated[k]: updated[k] = score
                if best == updated: break
                best = updated
            out = [row for (node,i,_),row in best.items() if node == goal and i == len(needed)]
            if out:
                time, cost, _, ids = min(out)
                return time, cost, ids
        rng = Random(661)
        for i in range(90):
            edges = [(str(j),rng.randrange(6),rng.randrange(6),rng.randrange(3),rng.randrange(5)) for j in range(21)]
            needed = rng.choices(range(6), k=rng.randrange(5))
            expected = oracle(edges,0,5,needed)
            before = edges[:], needed[:]
            assert plan(edges,0,5,needed) == expected, (i, needed, expected)
            assert (edges, needed) == before
        edges = [(f'{i}-a',i,i+1,1,4) for i in range(300)] + [(f'{i}-z',i,i+1,1,4) for i in range(300)]
        assert plan(edges,0,300,[80,140,230]) == (300,1196,tuple(f'{i}-a' for i in range(300)))
        """,
    ),
}
