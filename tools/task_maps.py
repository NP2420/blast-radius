"""Per-query coverage for heavy executor tests (tpcds): which queries run which lines.

usage: task_maps.py REPO OUT_JSON
Runs TestExecutor.test_execute_tpcds in-process (syncpool) under coverage and
switches context per query: "pre<i>" (parse before submit), "task<i>" (pool task),
"cmp<i>" (comparison in subtestHelper). Everything else is "rest". lru caches
are cleared before every task so one query's cache cannot hide another's
dependency. Output: {test_id: {"n": N, "rest": {path: [lines]},
                               "q": {path: {line: [query indices]}}}}
"""
import functools
import gc
import json
import os
import sys
import unittest

repo, out = sys.argv[1], sys.argv[2]
sys.path[:0] = [repo, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector")]
os.chdir(repo)
os.environ["SKIP_INTEGRATION"] = "0"
import coverage  # noqa: E402

cov = coverage.Coverage(data_file=None, source=[os.path.join(repo, "sqlglot")])
import syncpool  # noqa: E402

syncpool.install()
import tests.test_executor as TE  # noqa: E402

state = {"i": 0}


def clear_caches():
    for obj in gc.get_objects():
        if isinstance(obj, functools._lru_cache_wrapper):
            mod = getattr(obj, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                obj.cache_clear()


orig_submit = syncpool.SyncExecutor.submit


def submit(self, fn, /, *args, **kwargs):
    i = state["i"]
    clear_caches()
    cov.switch_context(f"task{i}")
    try:
        return orig_submit(self, fn, *args, **kwargs)
    finally:
        state["i"] = i + 1
        cov.switch_context(f"pre{i + 1}")


syncpool.SyncExecutor.submit = submit
orig_helper = TE.TestExecutor.subtestHelper


def helper(self, i, table, tpch=True):
    cov.switch_context(f"cmp{i}")
    try:
        return orig_helper(self, i, table, tpch=tpch)
    finally:
        cov.switch_context("rest")


TE.TestExecutor.subtestHelper = helper
orig_mp = TE.TestExecutor._mp_execute


def mp(self, *a, **k):
    state["i"] = 0
    cov.switch_context("pre0")
    try:
        return orig_mp(self, *a, **k)
    finally:
        cov.switch_context("rest")


TE.TestExecutor._mp_execute = mp

result = {}
for name in ("test_execute_tpcds",):
    tid = f"tests/test_executor.py::TestExecutor::{name}"
    TE.TestExecutor.setUpClass()
    cov.erase()
    cov.start()
    cov.switch_context("rest")
    r = unittest.TestResult()
    TE.TestExecutor(name).run(r)
    cov.stop()
    TE.TestExecutor.tearDownClass()
    print(name, "errors", len(r.errors), "failures", len(r.failures), file=sys.stderr)
    data = cov.get_data()
    rest: dict = {}
    q: dict = {}
    nq = state["i"]
    for f in data.measured_files():
        rel = os.path.relpath(f, repo)
        for ln, ctxs in data.contexts_by_lineno(f).items():
            for c in ctxs:
                if c in ("rest", ""):
                    rest.setdefault(rel, set()).add(ln)
                else:
                    idx = int(c.lstrip("pretaskcmp"))
                    q.setdefault(rel, {}).setdefault(ln, set()).add(idx)
    result[tid] = {"n": nq, "rest": {k: sorted(v) for k, v in rest.items()},
                   "q": {k: {ln: sorted(v) for ln, v in d.items()} for k, d in q.items()}}
json.dump(result, open(out, "w"))
