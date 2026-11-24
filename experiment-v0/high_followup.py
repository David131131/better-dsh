"""Regrade archived high-run workspaces against additional local edge cases."""

import argparse
import json
from pathlib import Path
import tempfile
from textwrap import dedent

from bench import write_json
from high_cases import make_cases
from tasks import REPAIRS, check_python, prepare, write_reference


CHECKS = {
    "R01": """
        import difflib
        from random import Random

        r = Random(762)
        lines = ['a prefix for '+str(n)+'\\n' for n in range(7)] + ['?', '\\n', '雪\\n']
        for _ in range(96):
            a = r.choices(lines, k=r.randrange(24))
            b = r.choices(lines, k=r.randrange(24))
            delta = list(difflib.ndiff(a, b))
            assert list(difflib.restore(delta, 1)) == a
            assert list(difflib.restore(delta, 2)) == b
    """,
    "R02": """
        from graphlib import CycleError, TopologicalSorter

        graph = TopologicalSorter({'c': ['a', 'b'], 'd': ['c']})
        graph.prepare()
        assert graph.is_active()
        graph.prepare()
        assert set(graph.get_ready()) == {'a', 'b'}
        graph.done('a')
        assert graph.get_ready() == ()
        graph.done('b')
        assert graph.get_ready() == ('c',)
        graph.done('c')
        assert graph.get_ready() == ('d',)
        graph.done('d')
        assert not graph

        cyclic = TopologicalSorter({'a': ['b'], 'b': ['a']})
        for _ in range(2):
            try:
                cyclic.prepare()
            except CycleError as e:
                assert e.args[1][0] == e.args[1][-1]
            else:
                raise AssertionError('cycle hidden by repeated prepare')
            assert cyclic.get_ready() == ()
    """,
    "R03": """
        import asyncio
        from local_queue import queues

        async def cancel(waiter):
            waiter.cancel()
            try:
                await waiter
            except asyncio.CancelledError:
                return
            raise AssertionError('waiter was not cancelled')

        async def main():
            q = queues.Queue(1)
            abandoned = asyncio.create_task(q.get())
            receiver = asyncio.create_task(q.get())
            await asyncio.sleep(0)
            await cancel(abandoned)
            await q.put('in progress')
            assert await asyncio.wait_for(receiver, 0.5) == 'in progress'
            await q.put('discard')
            abandoned = asyncio.create_task(q.put('cancelled'))
            sender = asyncio.create_task(q.put('closed'))
            await asyncio.sleep(0)
            await cancel(abandoned)
            q.shutdown(immediate=True)
            q.shutdown(immediate=True)
            try:
                await asyncio.wait_for(sender, 0.5)
            except queues.QueueShutDown:
                pass
            else:
                raise AssertionError('closed queue accepted a waiting put')
            join = asyncio.create_task(q.join())
            await asyncio.sleep(0)
            assert not join.done(), 'double shutdown lost an in-flight task'
            q.task_done()
            await asyncio.wait_for(join, 0.5)
        asyncio.run(main())
    """,
    "R04": """
        from configparser import ConfigParser, UNNAMED_SECTION
        from io import StringIO

        options = dict(allow_unnamed_section=True, delimiters=('::',))
        original = ConfigParser(**options)
        original.read_string('prefix:: %(common)s!\\n[DEFAULT]\\ncommon:: yes\\n[regular]\\nplain:: value')
        stream = StringIO()
        original.write(stream, space_around_delimiters=False)
        data = stream.getvalue()
        assert data.startswith('prefix::%(common)s!\\n'), repr(data)
        again = ConfigParser(**options)
        again.read_string(data)
        assert again.get(UNNAMED_SECTION, 'prefix') == 'yes!'
        assert again.get('regular', 'plain') == 'value'
        assert again.defaults() == {'common': 'yes'}
    """,
    "R05": """
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        a, b, stable = Version('2.2a1'), Version('2.6rc1'), Version('2.4.post2')
        s = SpecifierSet('>=2,<3,!=2.3.*')
        with_stable = list(s.filter(x for x in (a, '2.3a1', b, stable)))
        assert len(with_stable) == 1 and with_stable[0] is stable
        fallback = list(s.filter(x for x in (b, '2.3a1', a, a)))
        assert len(fallback) == 3 and all(x is y for x, y in zip(fallback, (b, a, a)))
        assert list(s.filter(x for x in ('3.0rc1', '1.9rc1'))) == []
    """,
    "R06": """
        from packaging.markers import Marker

        assert not Marker("'1.0' == os_name").evaluate(environment={'os_name':'1.0.0'})
        assert Marker("'3.10' <= python_full_version").evaluate(environment={'python_full_version':'3.10.0'})
        assert not Marker("platform_machine > 'a'").evaluate(environment={'platform_machine':'z'})
        assert Marker("platform_machine >= 'z'").evaluate(environment={'platform_machine':'z'})
        assert Marker("'arm' in platform_machine").evaluate(environment={'platform_machine':'arm64'})
    """,
    "R07": """
        from packaging.requirements import Requirement

        written = ['Demo[y,x]>2.0.0,<=4.0.0,!=3.0.0',
                   'demo[x,y]!=3.0,>2,<=4.0', 'demo[y,x]<=4,!=3,>2.0']
        group = [Requirement(text) for text in written]
        assert all(row == group[0] for row in group)
        assert len({hash(row) for row in group}) == len(set(group)) == 1
        assert len({**dict.fromkeys(group), Requirement('demo[x,y]>2,<4,!=3'): None}) == 2
        assert str(group[0]) != str(group[1]), 'existing serialization was normalized'
    """,
    "R08": """
        from random import Random
        from scheduler import solve

        r = Random(21105)
        for _ in range(96):
            jobs = [(f'job-{i}', (s := r.randrange(-4, 8)), s + r.randrange(1, 5),
                     r.choice((0, 1, 1, 2)), r.randrange(3)) for i in range(r.randrange(10))]
            budget = r.randrange(6)
            r.shuffle(jobs)
            ordered = sorted(jobs, key=lambda x: x[1])
            best = (0, 0, 0, ())
            for mask in range(1 << len(ordered)):
                candidate = [job for i, job in enumerate(ordered) if mask >> i & 1]
                cost = sum(job[4] for job in candidate)
                if cost > budget or any(a[2] > b[1] for a, b in zip(candidate, candidate[1:])):
                    continue
                score = -sum(job[3] for job in candidate)
                best = min(best, (score, cost, len(candidate), tuple(job[0] for job in candidate)))
            expected = -best[0], best[1], best[3]
            assert solve(jobs, budget) == expected, (jobs, budget, expected)
    """,
}


def use_prompt_order(workspace):
    # CPython 3.14.0 writes DEFAULT first. The task explicitly asks for the
    # unnamed section first so it can be read back as an unnamed section.
    module = workspace / "configparser.py"
    source = module.read_text()
    before = source.index("        if self._defaults:\n", source.index("    def write("))
    unnamed = source.index("        if UNNAMED_SECTION in self._sections", before)
    after = source.index("\n\n        for section", unnamed)
    source = source[:before] + source[unnamed:after] + "\n" + source[before:unnamed].rstrip("\n") + source[after:]
    module.write_text(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path, nargs="?")
    args = parser.parse_args()
    REPAIRS.update(make_cases())
    with tempfile.TemporaryDirectory(prefix="better-dsh-followup-") as tmp:
        for case, check in CHECKS.items():
            reference = Path(tmp) / case
            prepare(case, reference)
            write_reference(case, reference)
            if case == "R04":
                assert not check_python(dedent(check), reference)[0]
                use_prompt_order(reference)
            passed, detail = check_python(dedent(check), reference)
            if not passed:
                raise ValueError(f"{case} 参考实现未通过补充验收：{detail}")
    print(f"{len(CHECKS)} 个补充验收均已核对参考实现。")
    if args.batch is None:
        return
    manifest = json.loads((args.batch / "manifest.json").read_text())
    rows = []
    for case, repeat in manifest["schedule"]:
        directory = args.batch / f"{case}-high-{repeat}"
        path = directory / "result.json"
        if not path.is_file():
            continue
        original = json.loads(path.read_text())
        if not (directory / "workspace").is_dir() or not original["route_ok"]:
            continue
        archived_passed, archived_detail = check_python(REPAIRS[case].check, directory / "workspace")
        passed, detail = check_python(dedent(CHECKS[case]), directory / "workspace")
        rows.append({"case": case, "repeat": repeat, "primary_passed": original["passed"],
                     "outcome": original["outcome"], "archived_primary_passed": archived_passed,
                     "archived_primary_detail": archived_detail, "followup_passed": passed,
                     "followup_detail": detail})
    write_json(args.batch / "followup.json", rows)
    print(f"{sum(row['followup_passed'] for row in rows)}/{len(rows)} 次通过补充验收。")


if __name__ == "__main__":
    main()
