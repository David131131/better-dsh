"""Pinned upstream regressions and an independently scored optimization task."""

from pathlib import Path
from textwrap import dedent

from tasks import Repair


SOURCES = Path(__file__).resolve().parent / "upstream-sources"
LOCAL_ONLY = "\n请修改当前目录中的实现。不要用已安装版本替换或代理该模块。"
ORIGINS = {
    "R01": ("CPython difflib", "https://github.com/python/cpython/issues/119105"),
    "R02": ("CPython graphlib", "https://github.com/python/cpython/pull/131317"),
    "R03": ("CPython asyncio 队列", "https://docs.python.org/3.14/library/asyncio-queue.html#asyncio.Queue.shutdown"),
    "R04": ("CPython 配置序列化", "https://docs.python.org/3.14/library/configparser.html#configparser.InvalidWriteError"),
    "R05": ("PyPA 版本候选过滤", "https://github.com/pypa/packaging/pull/897"),
    "R06": ("PyPA 环境标记", "https://github.com/pypa/packaging/pull/939"),
    "R07": ("PyPA 需求对象相等与哈希", "https://github.com/pypa/packaging/pull/1232"),
    "R08": ("预算约束区间选择", None),
}


def cpython(category, old, path, prompt, check, extras=None):
    original = SOURCES / "cpython" / old
    gold = SOURCES / "cpython" / "3.14.0"
    name = Path(path).name
    return Repair(
        category, prompt + LOCAL_ONLY,
        {name: (original / path).read_text(), "LICENSE": (original / "LICENSE").read_text(), **(extras or {})},
        dedent(check), {name: (gold / path).read_text()},
    )


def package_files(version):
    path = SOURCES / "packaging" / version
    return {
        p.relative_to(path).as_posix(): p.read_text()
        for p in sorted(path.rglob("*"))
        if p.is_file() and (p.suffix == ".py" or p.name.startswith("LICENSE") or p.name == "py.typed")
    }


def packaging(category, old, fixed, prompt, check):
    return Repair(category, prompt + LOCAL_ONLY, package_files(old), dedent(check), package_files(fixed))


def make_cases():
    return {
        "R01": cpython(
            "diff-algorithm", "3.12.0", "Lib/difflib.py",
            """修复本目录 difflib.Differ.compare / ndiff 在大量相似但不完全相同的重复行上的递归问题。
处理这类输入时不能随着行数线性加深 Python 调用栈，不要调整全局递归上限。
保留 delta 的原有前缀协议，restore(delta, 1/2) 必须精确重建原输入，常见的相似单行仍给出行内标记。
允许在病态输入上调整类似行的配对策略，不要修改公开函数签名。""",
            """\
            import difflib, sys
            normal = list(difflib.ndiff(['alpha beta gamma\\n'], ['alpha zeta gamma\\n']))
            assert any(row.startswith('? ') for row in normal), normal
            old = sys.getrecursionlimit()
            for size, left, right in ((83,'setting color=white','setting color=black'),
                                     (92,'prefix status open','prefix status done')):
                a, b = [left+'\\n']*size, [right+'\\n']*size
                try:
                    sys.setrecursionlimit(52)
                    d = list(difflib.Differ().compare(a,b))
                    assert sys.getrecursionlimit()==52
                finally:
                    sys.setrecursionlimit(old)
                assert list(difflib.restore(d,1))==a
                assert list(difflib.restore(d,2))==b
                assert all(row[:2] in ('  ', '- ', '+ ', '? ') for row in d)
            """,
        ),
        "R02": cpython(
            "graph-lifecycle", "3.13.0", "Lib/graphlib.py",
            """修复本目录 TopologicalSorter 的准备状态。只要还没有实际发出任何节点，允许重复调用 prepare()；
重复准备不应重建已准备状态、解除 add 限制，或吞掉 CycleError。get_ready() 返回空元组时仍可准备；
发出节点后 prepare 必须报 ValueError。预先 prepare 后可以调用 static_order。保留循环图仍可逐步处理非循环节点的行为。""",
            """\
            from graphlib import CycleError, TopologicalSorter
            def expect(exc, fn, *args):
                try: fn(*args)
                except exc: return
                raise AssertionError((exc,fn))
            ts=TopologicalSorter({'job':{'boot'}, 'last':{'job'}})
            ts.prepare()
            ts.prepare()
            expect(ValueError,ts.add,'extra')
            assert tuple(ts.static_order()) == ('boot','job','last')
            expect(ValueError,ts.prepare)
            ts=TopologicalSorter()
            ts.prepare()
            ts.prepare()
            assert ts.get_ready()==()
            ts.prepare()
            ts=TopologicalSorter({'a':{'b'},'b':{'a'},'done':set()})
            expect(CycleError,ts.prepare)
            expect(CycleError,ts.prepare)
            assert ts.get_ready()==('done',)
            ts.done('done')
            assert not ts.is_active()
            expect(ValueError,ts.prepare)
            """,
        ),
        "R03": cpython(
            "async-cancellation", "3.12.0", "Lib/asyncio/queues.py",
            """在本地 queues.py 实现 asyncio Queue.shutdown(immediate=False) 及 QueueShutDown。
关闭后所有新 put、已阻塞的 put 都抛 QueueShutDown。默认关闭仍可取出已有元素，队列为空时 get 抛该异常；
关停须唤醒等待的 getter 和 putter。immediate=True 丢弃所有待取元素，并从未完成任务计数中只减去丢弃的数量。
已经取出的任务仍需 task_done 才能让 join 结束。保留普通队列的 FIFO、QueueFull、QueueEmpty 和取消语义。
测试通过 local_queue.py 加载本地源码；请勿改这个加载器。""",
            """\
            import asyncio
            from local_queue import queues as qmod
            async def expect_async(exc, task):
                try: await asyncio.wait_for(task, 0.5)
                except exc: return
                raise AssertionError(exc)
            def expect_sync(exc, fn, *args):
                try: fn(*args)
                except exc: return
                raise AssertionError(exc)
            async def main():
                q=qmod.Queue(2)
                q.put_nowait('work')
                q.put_nowait('later')
                p=asyncio.create_task(q.put('blocked'))
                await asyncio.sleep(0)
                assert not p.done()
                q.shutdown()
                await expect_async(qmod.QueueShutDown,p)
                expect_sync(qmod.QueueShutDown,q.put_nowait,'new')
                assert await q.get()=='work'
                assert q.get_nowait()=='later'
                expect_sync(qmod.QueueShutDown,q.get_nowait)
                q.task_done()
                q.task_done()
                await asyncio.wait_for(q.join(),0.5)
                q=qmod.Queue()
                q.put_nowait(1)
                q.put_nowait(2)
                assert await q.get()==1
                j=asyncio.create_task(q.join())
                await asyncio.sleep(0)
                q.shutdown(immediate=True)
                await asyncio.sleep(0)
                assert q.empty() and not j.done()
                await expect_async(qmod.QueueShutDown,q.get())
                q.task_done()
                await asyncio.wait_for(j,0.5)
                q=qmod.Queue()
                g=asyncio.create_task(q.get())
                await asyncio.sleep(0)
                q.shutdown()
                await expect_async(qmod.QueueShutDown,g)
                q=qmod.Queue(1)
                expect_sync(qmod.QueueEmpty,q.get_nowait)
                await q.put(3)
                expect_sync(qmod.QueueFull,q.put_nowait,4)
                assert await q.get()==3
            asyncio.run(main())
            """,
            {"local_queue.py": dedent("""\
                import asyncio
                from importlib.util import module_from_spec, spec_from_file_location
                from pathlib import Path

                spec = spec_from_file_location('asyncio._bench_queues', Path(__file__).with_name('queues.py'))
                queues = module_from_spec(spec)
                spec.loader.exec_module(queues)
                """)},
        ),
        "R04": cpython(
            "config-roundtrip", "3.13.0", "Lib/configparser.py",
            """修复本地 configparser 的无标题节写回和不可重读键校验。启用 allow_unnamed_section 后，
RawConfigParser.add_section(UNNAMED_SECTION) 可创建该节，未启用则抛 UnnamedSectionDisabledError；
序列化时无标题节的键放在普通节和 DEFAULT 之前，不输出虚构标题，读回应得到相同数据。
写入会被误当成节标题的键，或包含配置分隔符的键时，抛 InvalidWriteError。
普通节、默认值、无值选项、插值和自定义分隔符原有行为应保留。""",
            """\
            import configparser as c
            from io import StringIO
            def write(cfg):
                f=StringIO(); cfg.write(f); return f.getvalue()
            def expect(exc, fn, *args):
                try: fn(*args)
                except exc: return
                raise AssertionError((exc,fn))
            raw=c.RawConfigParser(allow_unnamed_section=True, allow_no_value=True)
            raw.add_section('normal')
            raw.add_section(c.UNNAMED_SECTION)
            raw.set(c.UNNAMED_SECTION,'setting','yes')
            raw.set(c.UNNAMED_SECTION,'empty',None)
            raw.set('normal','text','value')
            rendered=write(raw)
            assert rendered.startswith('setting = yes\\nempty\\n'), repr(rendered)
            after=c.RawConfigParser(allow_unnamed_section=True,allow_no_value=True)
            after.read_string(rendered)
            assert dict(after[c.UNNAMED_SECTION])=={'setting':'yes','empty':None}
            assert after['normal']['text']=='value'
            expect(c.UnnamedSectionDisabledError,c.RawConfigParser().add_section,c.UNNAMED_SECTION)
            for key in ('[trap]suffix','a::b'):
                cfg=c.ConfigParser(delimiters=('::',))
                cfg.add_section('valid'); cfg.set('valid',key,'yes')
                expect(c.InvalidWriteError,write,cfg)
            cfg=c.ConfigParser(delimiters=('::',))
            cfg.read_string('[DEFAULT]\\nx:: ok\\n[a]\\ny:: %(x)s!\\na:b:: value')
            other=c.ConfigParser(delimiters=('::',)); other.read_string(write(cfg))
            assert other['a']['y']=='ok!' and other['a']['a:b']=='value'
            """,
        ),
        "R05": packaging(
            "prerelease-filtering", "25.0", "26.0",
            """修复 SpecifierSet.filter 的预发行候选决策。默认未明确设置 prereleases 时，
若有符合整个版本约束的正式版或后发行版，则返回这些正式候选；若没有，则允许符合的预发行版。
显式允许时输出所有符合项，显式禁止时排除预发行版。可在规格本身设定或调用时覆盖。
多个约束应共同判定；保持输入元素原形与顺序，包括 Version 对象和只能迭代一次的输入。保持严格版本边界。""",
            """\
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version
            s=SpecifierSet('>=1,<2')
            assert list(s.filter(iter(['1.3a1','1.9rc2','2.2','0.9'])))==['1.3a1','1.9rc2']
            assert list(s.filter(iter(['1.3a1','1.1','1.8.post1','1.9rc2'])))==['1.1','1.8.post1']
            assert list(s.filter(['1.3a1'],prereleases=False))==[]
            assert list(s.filter(['1.3a1','1.2'],prereleases=True))==['1.3a1','1.2']
            assert list(SpecifierSet('>=1,<2',prereleases=False).filter(['1.3a1']))==[]
            assert list(SpecifierSet('>=1,<2',prereleases=False).filter(['1.3a1'],prereleases=True))==['1.3a1']
            obj=Version('1.7a1')
            assert list(s.filter(iter([obj,'2.1'])))==[obj]
            assert list(s.filter(['2.0rc1'],prereleases=True))==[]
            """,
        ),
        "R06": packaging(
            "typed-marker-comparison", "25.0", "26.0",
            """修复 packaging.markers 的比较类型。python_version、python_full_version、
implementation_version 和 platform_release 继续支持版本约束。os_name、sys_platform、
platform_machine 等字符串字段用精确字符串相等/不等和原有 in/not in 语义。
当字符串值不相等时有序比较返回 False；相等时 <= 和 >= 可为 True。
保留逻辑组合与括号、extra 的规范化行为，不要把像版本号的普通字符串做版本归一化。""",
            """\
            from packaging.markers import Marker
            def m(s,**env): return Marker(s).evaluate(environment=env)
            assert not m("os_name == '1.0'",os_name='1.0.0')
            assert m("os_name != '1.0'",os_name='1.0.0')
            assert not m("sys_platform < 'z'",sys_platform='a')
            assert m("sys_platform <= 'a'",sys_platform='a')
            assert m("os_name in 'python, posix'",os_name='posix')
            assert m("os_name not in 'nt'",os_name='posix')
            assert m("python_full_version == '3.13.*' and (platform_machine == 'arm64' or os_name == 'nt')",
                     python_full_version='3.13.7',platform_machine='arm64',os_name='posix')
            assert m("platform_release >= '5.4'",platform_release='5.10.0')
            assert m("extra == 'some-group'",extra='Some_Group')
            """,
        ),
        "R07": packaging(
            "hash-equality-contract", "26.2", "26.3",
            """修复 packaging.requirements.Requirement 的哈希契约：
相等的 Requirement 必须哈希相同，即使版本约束只是尾随零写法不同，或 specifier 顺序不同。
名称规范化、同样的 extras 与 marker 等已有的相等行为应保留。集合去重和用等价对象查字典必须正常。
不同的约束（含通配前缀与兼容版本边界）仍保持已有相等语义，不改变 str() 或公开 API。""",
            """\
            from packaging.requirements import Requirement
            pairs=[
                ('Some_Project[x]>=1.2.0,<3.0; python_version >= "3.10"',
                 'some-project[x]<3,>=1.2; python_version >= "3.10"'),
                ('foo==1.0.0','foo==1'),
                ('foo!=2.0.0','foo!=2'),
                ('foo>=1!2.0.0','foo>=1!2.0'),
            ]
            for a,b in pairs:
                lhs,rhs=Requirement(a),Requirement(b)
                assert lhs==rhs,(a,b)
                assert hash(lhs)==hash(rhs),(a,b)
                assert len({lhs,rhs})==1 and {lhs:9}[rhs]==9
                assert Requirement(str(lhs))==lhs
            assert Requirement('foo==1.*') != Requirement('foo==1.0.*')
            assert Requirement('foo~=1.2') != Requirement('foo~=1.2.0')
            assert Requirement('foo[x]==1') != Requirement('foo[y]==1')
            """,
        ),
        "R08": schedule_case(),
    }


def schedule_case():
    order = "def ordered(jobs):\n    return sorted(jobs,key=lambda j:(j[1],j[2],j[0]))\n"
    return Repair(
        "constrained-optimization",
        """修复 scheduler.solve(jobs,budget)。jobs 中每项是 (唯一 ID, 开始, 结束, 收益, 成本)，整数半开区间 start<end。
从中选一组互不重叠的任务，总成本不能超过 budget（非负整数）。目标按优先级为：收益最大、成本最小、任务数最少、按时间排列的 ID 元组字典序最小。
返回 (收益, 成本, 按开始时间排列的 ID 元组)，允许空解、负收益和零成本。输入可乱序，不得修改。
常规输入可有 250 个任务、预算 60，不能穷举全部子集。保持入口和返回形状。""",
        {
            "scheduler.py": "from choose import select\nfrom job_order import ordered\n\ndef solve(jobs,budget):\n    return select(ordered(jobs),budget)\n",
            "job_order.py": order,
            "choose.py": dedent("""\
                def select(jobs,budget):
                    reward,spent,chosen=0,0,[]
                    until=-10**100
                    for key,start,end,value,cost in jobs:
                        if start>=until and spent+cost<=budget and value>0:
                            reward+=value
                            spent+=cost
                            chosen.append(key)
                            until=end
                    return reward,spent,tuple(chosen)
                """),
        },
        dedent("""\
            from itertools import combinations
            from random import Random
            from scheduler import solve
            def oracle(jobs,budget):
                candidates=[]
                for n in range(len(jobs)+1):
                    for subset in combinations(jobs,n):
                        seq=sorted(subset,key=lambda row:(row[1],row[2],row[0]))
                        if any(a[2]>b[1] for a,b in zip(seq,seq[1:])): continue
                        cost=sum(row[4] for row in seq)
                        if cost<=budget:
                            candidates.append((-sum(row[3] for row in seq),cost,n,tuple(row[0] for row in seq)))
                reward,cost,_,ids=min(candidates)
                return -reward,cost,ids
            example=[('a',0,6,7,2),('b',0,2,5,2),('c',2,5,6,2),('d',5,8,4,1)]
            assert solve(example,5)==(15,5,('b','c','d'))
            assert solve([('a',0,1,2,0),('b',1,2,2,0),('z',0,2,4,0)],0)==(4,0,('z',))
            r=Random(1042)
            for i in range(64):
                jobs=[(str(j),a,a+r.randint(1,6),r.randint(-3,9),r.randint(0,4))
                      for j,a in enumerate(r.choices(range(-3,10),k=r.randint(0,10)))]
                r.shuffle(jobs); budget=r.randint(0,14); original=jobs[:]
                expected=oracle(jobs,budget)
                actual=solve(jobs,budget)
                assert actual==expected,(i,jobs,budget,expected,actual)
                assert jobs==original
            jobs=[(f'{i:03}',2*i,2*i+2,1,1) for i in range(250)]
            assert solve(list(reversed(jobs)),60)==(60,60,tuple(f'{i:03}' for i in range(60)))
            """),
        {"choose.py": dedent("""\
            from bisect import bisect_left
            from functools import lru_cache

            def select(jobs,budget):
                starts=[row[1] for row in jobs]
                following=[bisect_left(starts,row[2],i+1) for i,row in enumerate(jobs)]

                @lru_cache(None)
                def visit(i,left):
                    if i==len(jobs): return (0,0,0,())
                    best=visit(i+1,left)
                    key,_,_,value,cost=jobs[i]
                    if cost<=left:
                        gain,spent,size,ids=visit(following[i],left-cost)
                        best=min(best,(gain-value,spent+cost,size+1,(key,)+ids))
                    return best

                gain,cost,_,ids=visit(0,budget)
                return -gain,cost,ids
            """)},
    )
