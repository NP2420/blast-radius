"""sqlglot functions the TPC-DS executor runs per row (outside code generation).

usage: tpc_dataphase.py REPO OUT.json
Runs every executed TPC-DS query on the full tables in-process and records, with
sys.monitoring, each sqlglot function (outside sqlglot/executor) started while the
executor runs a plan but not while it generates code.
"""
import json, os, sys

repo, dst = sys.argv[1], sys.argv[2]
os.chdir(repo)
sys.path.insert(0, repo)
sys.path.insert(0, "/home/claude/dev/br/selector")
import tpctrace  # noqa: E402
from sqlglot import find_tables, parse_one, planner  # noqa: E402
from sqlglot.executor import python as pyx  # noqa: E402
from sqlglot.executor.table import ensure_tables  # noqa: E402
from sqlglot.optimizer import optimize  # noqa: E402
from sqlglot.schema import ensure_schema  # noqa: E402

TID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
suite, schema, pairs = tpctrace.suites(repo)[TID]
tables = tpctrace.load_tables(repo, suite, schema, rows=10**9)
state = {"exec": 0, "gen": 0}
seen = {}
orig_gen = pyx.PythonExecutor.generate
orig_exec = pyx.PythonExecutor.execute


def gen(self, e):
    state["gen"] += 1
    try:
        return orig_gen(self, e)
    finally:
        state["gen"] -= 1


def exe(self, plan, outer_scope=None):
    state["exec"] += 1
    g, state["gen"] = state["gen"], 0  # a subquery plan run per row is run time again
    try:
        return orig_exec(self, plan, outer_scope)
    finally:
        state["exec"] -= 1
        state["gen"] = g


pyx.PythonExecutor.generate = gen
pyx.PythonExecutor.execute = exe
mon = sys.monitoring
TOOL = 3
mon.use_tool_id(TOOL, "dataphase")
root = os.path.join(repo, "sqlglot") + os.sep
skip = os.path.join(root, "executor") + os.sep


def on_start(code, off):
    f = code.co_filename
    if not f.startswith(root) or f.startswith(skip):
        return mon.DISABLE
    if state["exec"] and not state["gen"]:
        seen[(os.path.relpath(f, repo), code.co_qualname, code.co_firstlineno)] = 1
        return mon.DISABLE
    return None


mon.register_callback(TOOL, mon.events.PY_START, on_start)
for i, (meta, sql, _) in enumerate(pairs):
    if not meta.get("execute"):
        continue
    e = parse_one(sql)
    names = [t.name for t in find_tables(e)]
    tabs = ensure_tables({n: tables[n] for n in names})
    expression = optimize(e, ensure_schema(schema), leave_tables_isolated=True)
    plan = planner.Plan(expression)
    ex = pyx.PythonExecutor(tables=tabs)
    mon.set_events(TOOL, mon.events.PY_START)
    try:
        ex.execute(plan)
    finally:
        mon.set_events(TOOL, 0)
    print(i, len(seen), file=sys.stderr, flush=True)
json.dump(sorted(seen), open(dst, "w"), indent=0)
print(len(seen), "functions", file=sys.stderr)
