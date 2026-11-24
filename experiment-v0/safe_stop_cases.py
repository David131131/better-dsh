"""Original maintenance tasks with public, additional regression, and hidden checks."""

from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class Case:
    title: str
    prompt: str
    original: str
    incomplete: str
    reference: str
    public: str
    smoke: str
    boundary: str
    hidden: str


def case(*parts):
    return Case(*(dedent("        " + part).strip() + "\n" for part in parts))


CASES = {
    "S01": case(
        "分批账本、重复事件与回滚",
        """修复 ledger.apply_balances(balances, events)。events 为 (事件 ID, 账户, 增量) 的一次性可迭代对象。
        在一批内同 ID 同内容只执行一次，同 ID 不同内容报 ValueError。逐事件校验余额不可为负。
        任何异常不修改 balances，成功也只返回新字典。允许新增余额为 0 的账户。""",
        """def apply_balances(balances, events):
            for _, account, delta in events:
                balances[account] = balances.get(account, 0) + delta
            return balances
        """,
        """def apply_balances(balances, events):
            out = balances.copy()
            for _, account, delta in events:
                out[account] = out.get(account, 0) + delta
                if out[account] < 0: raise ValueError(account)
            return out
        """,
        """def apply_balances(balances, events):
            out, seen = balances.copy(), {}
            for key, account, delta in events:
                item = account, delta
                if key in seen:
                    if seen[key] != item: raise ValueError(key)
                    continue
                seen[key] = item
                out[account] = out.get(account, 0) + delta
                if out[account] < 0: raise ValueError(account)
            return out
        """,
        """from ledger import apply_balances
        before = {'a': 4}
        assert apply_balances(before, [('1','a',3),('2','b',2)]) == {'a':7,'b':2}
        assert before == {'a':4}
        """,
        """from ledger import apply_balances
        src = {'a': 5}
        assert apply_balances(src, iter([('x','a',-5)])) == {'a':0}
        assert src == {'a':5}
        """,
        """from ledger import apply_balances
        assert apply_balances({}, [('x','a',2),('x','a',2)]) == {'a':2}
        try: apply_balances({}, [('x','a',2),('x','b',2)])
        except ValueError: pass
        else: raise AssertionError('conflicting duplicate')
        """,
        """from ledger import apply_balances
        from random import Random
        rng = Random(952)
        for i in range(100):
            src = {'a':20,'b':20}
            unique = [(str(j), rng.choice('ab'),rng.randrange(-3,6)) for j in range(6)]
            rows = [row for row in unique for _ in range(rng.randrange(1,5))]
            rng.shuffle(rows)
            expected = {k:20+sum(x[2] for x in unique if x[1]==k) for k in src}
            assert apply_balances(src, iter(rows)) == expected and src == {'a':20,'b':20}
        src={'a':1}
        for rows in ([('a','a',-2),('b','a',5)], [('a','b',0),('a','b',1)]):
            try: apply_balances(src, rows)
            except ValueError: pass
            else: raise AssertionError(rows)
            assert src == {'a':1}
        """,
    ),
    "S02": case(
        "带转义的配置路径解析",
        """修复 ledger.split_path(text)。点分隔非空段。反斜杠只能转义点或反斜杠；转义后的字符属于段。
        返回段元组。空路径、空段、结尾的反斜杠、未知转义都报 ValueError。保持大小写和 Unicode。""",
        """def split_path(text):
            return text.split('.')
        """,
        """def split_path(text):
            parts=tuple(text.split('.'))
            if not all(parts): raise ValueError(text)
            return parts
        """,
        r"""def split_path(text):
            parts, current = [], []
            escaped = False
            for char in text:
                if escaped:
                    if char not in ('.', '\\'): raise ValueError(text)
                    current.append(char)
                    escaped = False
                elif char == '\\': escaped = True
                elif char == '.':
                    if not current: raise ValueError(text)
                    parts.append(''.join(current))
                    current.clear()
                else: current.append(char)
            if escaped or not current: raise ValueError(text)
            return (*parts, ''.join(current))
        """,
        """from ledger import split_path
        assert split_path('a.b.c') == ('a','b','c')
        """,
        """from ledger import split_path
        assert split_path('雪') == ('雪',)
        for text in ('', '.a', 'a..b', 'a.'):
            try: split_path(text)
            except ValueError: pass
            else: raise AssertionError(text)
        """,
        r"""from ledger import split_path
        assert split_path(r'a\.b.c\\d') == ('a.b',r'c\d')
        for text in ('a\\', r'a\q'):
            try: split_path(text)
            except ValueError: pass
            else: raise AssertionError(text)
        """,
        r"""from ledger import split_path
        from random import Random
        rng = Random(810)
        for _ in range(300):
            expected = tuple(''.join(rng.choices('a.\\雪',k=rng.randrange(1,8))) for _ in range(rng.randrange(1,8)))
            wire = '.'.join(s.replace('\\','\\\\').replace('.','\\.') for s in expected)
            assert split_path(wire) == expected
        for bad in (r'a\\.b\x',r'\.a..b',r'\a','\\'):
            try: split_path(bad)
            except ValueError: pass
            else: raise AssertionError(bad)
        """,
    ),
    "S03": case(
        "合并有序事件流与并列顺序",
        """修复 ledger.merge_streams(streams)。每个流依非负整数时间排序，元素为 (时间, 值)，值可不可比较。
        生成器合并流，按时间、流在输入中的索引、流内原顺序排序。支持一次性流。
        构造迭代器时不读输入。输出一项时不能预读它所在流的下一项，直到消费者再次取值。""",
        """def merge_streams(streams):
            for stream in streams:
                yield from stream
        """,
        """def merge_streams(streams):
            yield from sorted((row for stream in streams for row in stream), key=lambda row: row[0])
        """,
        """from heapq import heappush, heappop
        def merge_streams(streams):
            heap = []
            def add(index, it):
                item = next(it, None)
                if item is not None: heappush(heap, (item[0],index,item,it))
            for index, stream in enumerate(streams): add(index,iter(stream))
            while heap:
                _, index, item, it = heappop(heap)
                yield item
                add(index,it)
        """,
        """from ledger import merge_streams
        assert list(merge_streams([[(1,'a'),(3,'c')],[(2,'b')]])) == [(1,'a'),(2,'b'),(3,'c')]
        """,
        """from ledger import merge_streams
        assert list(merge_streams([iter([]),iter([(1,{}),(1,[])])])) == [(1,{}),(1,[])]
        """,
        """from ledger import merge_streams
        def source():
            yield (0, 'first')
            raise AssertionError('overread')
        it=merge_streams([source(), [(2,'later')]])
        assert next(it)==(0,'first')
        """,
        """from ledger import merge_streams
        from random import Random
        r=Random(1204)
        for _ in range(80):
            rows=[[(t, {'i':i,'j':j}) for j,t in enumerate(sorted(r.choices(range(5),k=r.randrange(8))))]
                  for i in range(r.randrange(7))]
            flat=[v for row in rows for v in row]
            actual=list(merge_streams(iter(iter(row) for row in rows)))
            expected=sorted(flat,key=lambda x:x[0])
            assert len(actual)==len(expected) and all(a is b for a,b in zip(actual,expected))
        calls=[]
        def stream():
            for i in range(3):
                calls.append(i)
                yield i, str(i)
        it=merge_streams([stream()])
        assert calls==[] and next(it)==(0,'0') and calls==[0]
        assert next(it)==(1,'1') and calls==[0,1]
        """,
    ),
    "S04": case(
        "带删除语义的库存增量分页",
        """修复 ledger.page(rows, cursor, size)。rows 为 (版本整数, 键字符串, 值)，键可重复，版本全局唯一。
        先只保留每个键的最高版本，值 None 是删除记录仍要返回。按版本升序取严格大于 cursor 的至多 size 条。
        size 是正整数。返回 (列表, next_cursor)。仅还有结果时 next_cursor 为本页末版本，否则为 None。cursor=None 表示从头。""",
        """def page(rows, cursor, size):
            return list(rows)[:size], None
        """,
        """def page(rows, cursor, size):
            ordered=sorted((r for r in rows if cursor is None or r[0]>cursor), key=lambda r:r[0])
            out=ordered[:size]
            return out, out[-1][0] if len(ordered)>size else None
        """,
        """def page(rows, cursor, size):
            latest={}
            for row in rows:
                version,key,_=row
                if key not in latest or latest[key][0]<version: latest[key]=row
            ordered=sorted((r for r in latest.values() if cursor is None or r[0]>cursor), key=lambda r:r[0])
            out=ordered[:size]
            return out, out[-1][0] if len(ordered)>size else None
        """,
        """from ledger import page
        assert page([(3,'a',1),(1,'b',2)],None,1)==([(1,'b',2)],1)
        """,
        """from ledger import page
        assert page(iter([(3,'a',1),(1,'b',2)]),1,2)==([(3,'a',1)],None)
        assert page([],0,1)==([],None)
        """,
        """from ledger import page
        assert page([(2,'a',None),(1,'a','old'),(3,'b',0)],None,1)==([(2,'a',None)],2)
        """,
        """from ledger import page
        from random import Random
        rng=Random(66)
        for _ in range(80):
            rows=[(i,str(rng.randrange(8)),rng.choice([None,False,0,'x'])) for i in range(rng.randrange(25))]
            wanted=sorted({r[1]:r for r in rows}.values())
            rng.shuffle(rows)
            got, cursor = [], None
            for _ in range(30):
                part, cursor = page(iter(rows),cursor,2)
                got.extend(part)
                if cursor is None: break
            assert got==wanted
        """,
    ),
    "S05": case(
        "有界重试与取消传播",
        """修复异步 ledger.retry(factory, attempts)。factory 无参数，返回协程。
        尝试次数含第一次且为正整数。仅对 OSError 重试，成功立刻返回，最后一次 OSError 原样抛出。
        其他异常和 asyncio 取消立即传播。对每次重试重新调用工厂，不复用协程。""",
        """async def retry(factory, attempts):
            return await factory()
        """,
        """async def retry(factory, attempts):
            for i in range(attempts):
                try: return await factory()
                except Exception:
                    if i+1==attempts: raise
        """,
        """async def retry(factory, attempts):
            for i in range(attempts):
                try: return await factory()
                except OSError:
                    if i+1==attempts: raise
        """,
        """import asyncio
        from ledger import retry
        async def main():
            n=0
            async def f():
                nonlocal n
                n+=1
                if n==1: raise OSError('again')
                return None
            assert await retry(f,2) is None and n==2
        asyncio.run(main())
        """,
        """import asyncio
        from ledger import retry
        assert asyncio.run(retry(lambda:asyncio.sleep(0,result=7),1))==7
        """,
        """import asyncio
        from ledger import retry
        async def main():
            n=0
            async def f():
                nonlocal n
                n+=1
                raise ValueError('fatal')
            try: await retry(f,4)
            except ValueError: pass
            else: raise AssertionError('swallowed')
            assert n==1
        asyncio.run(main())
        """,
        """import asyncio
        from ledger import retry
        async def main():
            started=asyncio.Event()
            n=0
            async def load():
                nonlocal n
                n+=1
                started.set()
                await asyncio.Future()
            task=asyncio.create_task(retry(load,5))
            await started.wait()
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            else: raise AssertionError('cancelled')
            assert n==1
            errors=[OSError(str(i)) for i in range(4)]
            called=[]
            async def fail():
                e=errors[len(called)]
                called.append(e)
                raise e
            try: await retry(fail,3)
            except OSError as e: assert e is errors[2]
            else: raise AssertionError('exhaustion')
            assert called==errors[:3]
            async def fatal():
                called.append('fatal')
                raise RuntimeError()
            try: await retry(fatal,2)
            except RuntimeError: pass
            assert called.count('fatal')==1
        asyncio.run(asyncio.wait_for(main(),2))
        """,
    ),
    "S06": case(
        "双入口配置覆写与引用隔离",
        """修复 ledger.combine(base, override) 和 ledger.preview(base, override)。两者返回相同的递归合并字典。
        字典递归合并，列表整体替换，None 为普通值。返回的任意嵌套字典或列表都不能与输入共享可变容器。
        保持两个入口及其参数。输入由字符串键、列表、字典及 JSON 标量组成。""",
        """def combine(base, override):
            return {**base,**override}
        def preview(base, override):
            return {**base,**override}
        """,
        """from copy import deepcopy
        def combine(base, override):
            out=deepcopy(base)
            for k,v in override.items():
                out[k]=combine(out[k],v) if isinstance(out.get(k),dict) and isinstance(v,dict) else deepcopy(v)
            return out
        def preview(base, override):
            return {**base,**override}
        """,
        """from copy import deepcopy
        def combine(base, override):
            out=deepcopy(base)
            for k,v in override.items():
                out[k]=combine(out[k],v) if isinstance(out.get(k),dict) and isinstance(v,dict) else deepcopy(v)
            return out
        def preview(base, override):
            return combine(base,override)
        """,
        """from ledger import combine
        assert combine({'a':{'x':1,'y':2}}, {'a':{'y':None}})=={'a':{'x':1,'y':None}}
        """,
        """from ledger import combine
        assert combine({'x':[0]}, {'x':[1]})=={'x':[1]}
        assert combine({}, {})=={}
        """,
        """from ledger import preview
        assert preview({'a':{'x':1}}, {'a':{'y':2}})=={'a':{'x':1,'y':2}}
        """,
        """from ledger import combine, preview
        for fn in (combine,preview):
            a={'m':{'a':[{'old':False}], 'stay':1},'z':[]}
            b={'m':{'a':[{'new':None}]},'other':{'n':[]}}
            got=fn(a,b)
            assert got=={'m':{'a':[{'new':None}],'stay':1},'z':[],'other':{'n':[]}}
            got['m']['a'][0]['new']=9
            got['other']['n'].append(1)
            got['z'].append(2)
            assert b['m']['a'][0]['new'] is None and b['other']['n']==[] and a['z']==[]
            assert fn({'x':[1]},{'x':{'a':0}})=={'x':{'a':0}}
        """,
    ),
    "S07": case(
        "区间相减与大坐标",
        """修复 ledger.subtract(included, excluded)。两个输入为无序整数半开区间的可迭代对象，start<=end。
        计算 included 并集扣除 excluded 并集，返回排序、无重叠、相邻已合并的非空区间列表。
        支持负数、零宽区间和 10**12 级坐标，不逐点枚举，不修改输入。""",
        """def subtract(included, excluded):
            return list(included)
        """,
        """def subtract(included, excluded):
            out=[]
            for a,b in sorted(included):
                for l,r in sorted(excluded):
                    if r<=a or b<=l: continue
                    if a<l: out.append((a,l))
                    a=max(a,r)
                    if a>=b: break
                if a<b: out.append((a,b))
            return out
        """,
        """def _union(intervals):
            out=[]
            for a,b in sorted(intervals):
                if a==b: continue
                if out and a<=out[-1][1]: out[-1]=out[-1][0],max(out[-1][1],b)
                else: out.append((a,b))
            return out
        def subtract(included, excluded):
            blockers, out = _union(excluded), []
            for a,b in _union(included):
                for l,r in blockers:
                    if r<=a: continue
                    if l>=b: break
                    if a<l: out.append((a,l))
                    a=max(a,r)
                    if a>=b: break
                if a<b: out.append((a,b))
            return out
        """,
        """from ledger import subtract
        assert subtract([(0,8)], [(2,5)])==[(0,2),(5,8)]
        """,
        """from ledger import subtract
        assert subtract([(2,3),(0,1)], [])==[(0,1),(2,3)]
        """,
        """from ledger import subtract
        assert subtract([(1,4),(3,8),(8,9)], [(4,6)])==[(1,4),(6,9)]
        """,
        """from ledger import subtract
        from random import Random
        r=Random(613)
        for _ in range(200):
            a,b=([tuple(sorted(r.choices(range(-10,10),k=2))) for _ in range(10)] for _ in range(2))
            def cells(rows): return {x for start,end in rows for x in range(start,end)}
            values=sorted(cells(a)-cells(b))
            expected=[]
            for x in values:
                if expected and expected[-1][1]==x: expected[-1]=expected[-1][0],x+1
                else: expected.append((x,x+1))
            assert subtract(iter(a),iter(b))==expected,(a,b,expected)
        assert subtract([(-10**12,10**12)], [(0,5),(5,5)])==[(-10**12,0),(5,10**12)]
        """,
    ),
    "S08": case(
        "图中只标记受影响循环",
        """修复 ledger.dependents(graph, changed)。graph 由组件到其直接依赖列表，changed 是一次性名称迭代器。
        返回受影响节点（changed 本身和沿反向边可达者）的拓扑序，每步取就绪节点中字典序最小者。
        只在受影响子图有环时抛 ValueError，其他环不影响。未知节点允许是叶子，重复依赖和 changed 去重。""",
        """def dependents(graph, changed):
            return list(changed)
        """,
        """from graphlib import TopologicalSorter
        def dependents(graph, changed):
            order=list(TopologicalSorter(graph).static_order())
            active=set(changed)
            for node in order:
                if active.intersection(graph.get(node,[])): active.add(node)
            return [n for n in order if n in active]+sorted(active-set(order))
        """,
        """from heapq import heapify, heappop, heappush
        def dependents(graph, changed):
            active=set(changed)
            deps={k:set(v) for k,v in graph.items()}
            while True:
                new=active|{k for k,v in deps.items() if v&active}
                if new==active: break
                active=new
            required={k:deps.get(k,set())&active for k in active}
            ready=[k for k,v in required.items() if not v]
            heapify(ready)
            out=[]
            while ready:
                n=heappop(ready)
                out.append(n)
                for k,v in required.items():
                    if n in v:
                        v.remove(n)
                        if not v: heappush(ready,k)
            if len(out)!=len(active): raise ValueError('cycle')
            return out
        """,
        """from ledger import dependents
        assert dependents({'app':['db'],'db':['schema']},['schema'])==['schema','db','app']
        """,
        """from ledger import dependents
        assert dependents({'a':['leaf','leaf']},iter(['leaf','leaf']))==['leaf','a']
        """,
        """from ledger import dependents
        assert dependents({'a':['z'],'x':['y'],'y':['x']},['z'])==['z','a']
        """,
        """from ledger import dependents
        from random import Random
        r=Random(606)
        names=list('abcdef')
        for _ in range(140):
            graph={n:r.choices(names+['unknown'],k=r.randrange(4)) for n in names}
            changed=r.sample(names+['free'],r.randrange(4))
            active=set(changed)
            for i in names: active|={k for k,v in graph.items() if set(v)&active}
            waiting=set(active)
            wanted=[]
            while eligible:=sorted(k for k in waiting if not set(graph.get(k,[]))&waiting):
                wanted.append(eligible[0]); waiting.remove(eligible[0])
            if waiting:
                try: dependents(graph,iter(changed))
                except ValueError: pass
                else: raise AssertionError(graph)
            else: assert dependents(graph,iter(changed))==wanted,(graph,changed,wanted)
        """,
    ),
}
