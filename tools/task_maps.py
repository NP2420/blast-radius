"""Per-query coverage for heavy executor tests (tpcds): which queries run which lines.

usage: task_maps.py REPO OUT_JSON

Each pool task (one query) runs in its own forked process with a fresh coverage
tracer, starting from the parent's state, where no other task has run. So no
query can hide a dependency behind a cache another query filled, which matches
real pool workers (separate processes). Everything the test process does itself
(parsing every query, comparisons, setup) is "rest": a change there means the
test must run whole.

Output: {test_id: {"n": N, "rest": {path: [lines]}, "q": {path: {line: [query indices]}}}}
"""
import json
import os
import pickle
import sys
import unittest

repo, out = sys.argv[1], sys.argv[2]
sys.path[:0] = [repo, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector")]
os.chdir(repo)
os.environ["SKIP_INTEGRATION"] = "0"
import coverage  # noqa: E402
import syncpool  # noqa: E402

syncpool.install()
import tests.test_executor as TE  # noqa: E402

SRC = os.path.join(repo, "sqlglot")
task_lines: dict[int, dict[str, list]] = {}
state = {"i": 0}


def submit(self, fn, /, *args, **kwargs):
    import concurrent.futures as cf
    i = state["i"]
    state["i"] = i + 1
    fut = cf.Future()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            os.environ["COVERAGE_CORE"] = "sysmon"
            cov = coverage.Coverage(data_file=None, source=[SRC])
            cov.start()
            try:
                fn_, a_, k_ = pickle.loads(pickle.dumps((fn, args, kwargs)))
                res = (True, fn_(*a_, **k_))
            except BaseException as e:  # noqa: BLE001
                res = (False, e)
            cov.stop()
            data = cov.get_data()
            lines = {os.path.relpath(f, repo): sorted(data.lines(f) or []) for f in data.measured_files()}
            payload = pickle.dumps((res, lines))
        except BaseException as e:  # noqa: BLE001
            payload = pickle.dumps(((False, RuntimeError(repr(e))), {}))
        with os.fdopen(w, "wb") as fh:
            fh.write(payload)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as fh:
        payload = fh.read()
    os.waitpid(pid, 0)
    (ok, val), lines = pickle.loads(payload)
    task_lines[i] = lines
    if ok:
        fut.set_result(val)
    else:
        fut.set_exception(val)
    return fut


syncpool.SyncExecutor.submit = submit
orig_mp = TE.TestExecutor._mp_execute


def mp(self, *a, **k):
    state["i"] = 0
    return orig_mp(self, *a, **k)


TE.TestExecutor._mp_execute = mp

result = {}
for name in ("test_execute_tpcds",):
    tid = f"tests/test_executor.py::TestExecutor::{name}"
    os.environ["COVERAGE_CORE"] = "ctrace"
    parent_cov = coverage.Coverage(data_file=None, source=[SRC])
    parent_cov.start()  # setup + everything the test process itself runs
    TE.TestExecutor.setUpClass()
    r_ = unittest.TestResult()
    TE.TestExecutor(name).run(r_)
    TE.TestExecutor.tearDownClass()
    parent_cov.stop()
    print(name, "errors", len(r_.errors), "failures", len(r_.failures), "tasks", state["i"], file=sys.stderr)
    data = parent_cov.get_data()
    rest = {os.path.relpath(f, repo): sorted(data.lines(f) or []) for f in data.measured_files()}
    q: dict = {}
    for i, lines in task_lines.items():
        for path, ls in lines.items():
            d = q.setdefault(path, {})
            for ln in ls:
                d.setdefault(ln, []).append(i)
    result[tid] = {"n": state["i"], "rest": rest, "q": q}
json.dump(result, open(out, "w"))
