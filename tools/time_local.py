"""Measure the CPU each test costs when run locally the way runner.py runs it.

usage: time_local.py REPO TESTS_JSON OUT_JSON
Output: {"tests": {id: cpu_ms}, "class_setup": {"file::Class": cpu_ms}}
Process-pool work runs in-process (syncpool), as in local runs.
"""
import importlib
import json
import os
import sys
import time
import unittest

repo, tests_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path[:0] = [repo, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "selector")]
os.chdir(repo)
os.environ["SKIP_INTEGRATION"] = "0"
import syncpool  # noqa: E402

syncpool.install()

tests = json.load(open(tests_json))["tests"]
groups = {}
for t in tests:
    f, _, rest = t["id"].partition("::")
    c, _, m = rest.partition("::")
    groups.setdefault((f, c), []).append((t["id"], m))

res = {"tests": {}, "class_setup": {}}


class R(unittest.TestResult):
    pass


for (f, c), items in groups.items():
    mod = importlib.import_module(f[:-3].replace("/", "."))
    klass = getattr(mod, c)
    t0 = time.process_time()
    try:
        klass.setUpClass()
    except Exception:
        pass
    res["class_setup"][f"{f}::{c}"] = round((time.process_time() - t0) * 1000)
    for tid, m in items:
        tc = klass(m)
        t0 = time.process_time()
        r = R()
        tc.run(r)
        res["tests"][tid] = round((time.process_time() - t0) * 1000)
    try:
        klass.tearDownClass()
    except Exception:
        pass
    print(f, c, file=sys.stderr, flush=True)

json.dump(res, open(out, "w"))
