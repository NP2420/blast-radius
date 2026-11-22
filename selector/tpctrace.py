"""Data-free trace of TestExecutor's TPC tests: what each query's result depends on.

For every executed query of a TPC suite the trace records what is derived from
sqlglot before any row is touched: the DuckDB reference SQL, the tables the worker
receives, the optimized tree, the logical plan (all subquery plans too) and every
piece of Python source the executor generates. A run on a few rows per table adds
the code paths the plan takes at run time. When a patched sqlglot gives the same
trace as the pristine one for a query, and the patch leaves the executor's run-time
code (sqlglot/executor, plus what it calls per row) alone, that query's result is
the same as on the pristine commit, which passes.

usage: tpctrace.py REPO OUT.pkl   (pristine traces, made at image build)
"""
from __future__ import annotations

import collections
import csv
import gzip
import os
import pickle
import re
import sys

ROWS = 4  # rows per table for the run-time pass
_SQ = re.compile(r"_sq_\d+")


def _render(v):
    from sqlglot import exp

    if isinstance(v, exp.Expr):
        return repr(v)
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_render(k)}: {_render(x)}" for k, x in sorted(v.items(), key=lambda kv: repr(kv[0]))) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_render(x) for x in v) + "]"
    if isinstance(v, (set, frozenset)):
        return "{" + ", ".join(sorted(_render(x) for x in v)) + "}"
    if isinstance(v, type):
        return v.__name__
    return repr(v)


def _step(step) -> str:
    parts = [type(step).__name__]
    for k in sorted(vars(step)):
        v = getattr(step, k)
        if k in ("dependencies", "dependents"):
            parts.append(f"{k}={sorted(str(getattr(d, 'name', None)) for d in v)}")
        else:
            parts.append(f"{k}={_render(v)}")
    return " ".join(parts)


def _exprs(step):
    from sqlglot import exp

    out = []

    def add(v):
        if isinstance(v, exp.Expr):
            out.append(v)
        elif isinstance(v, dict):
            for x in v.values():
                add(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                add(x)

    for k, v in vars(step).items():
        if k not in ("dependencies", "dependents", "source"):
            add(v)
    return out


def load_tables(repo: str, suite: str, schema: dict, rows: int = ROWS) -> dict:
    """First `rows` rows of each table, typed the way TestExecutor.setUpClass types them."""
    import ast

    from sqlglot.executor.table import Table

    d = os.path.join(repo, "tests", "fixtures", "optimizer", suite)
    tables = {}
    for table, columns in schema.items():
        with gzip.open(os.path.join(d, f"{table}.csv.gz"), "rt", encoding="utf-8") as f:
            reader = csv.reader(f, delimiter="|")
            next(reader)
            out, ctypes = [], []
            for row in reader:
                if not ctypes:
                    for v in row:
                        try:
                            ctypes.append(type(ast.literal_eval(v)))
                        except (ValueError, SyntaxError):
                            ctypes.append(str)
                out.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
                if len(out) >= rows:
                    break
        tables[table] = Table(columns=columns, rows=out)
    return tables


def trace_query(sql: str, schema, tables: dict) -> str:
    from sqlglot import find_tables, parse_one, planner, transpile
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema

    out = []

    def guard(tag, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            out.append(f"{tag} raised {type(e).__name__}: {e}")
            return None

    out.append("duckdb " + str(guard("duckdb", lambda: transpile(sql, write="duckdb")[0])))
    e = guard("parse", lambda: pickle.loads(pickle.dumps(parse_one(sql))))
    if e is None:
        return "\n".join(out)
    names = sorted(t.name for t in find_tables(e))
    out.append(f"tables {names}")
    sub = {n: tables[n] for n in names if n in tables}
    tables_ = ensure_tables(sub)
    sch = ensure_schema(schema)
    expression = guard("optimize", lambda: optimize(e, sch, leave_tables_isolated=True))
    if expression is None:
        return "\n".join(out)
    out.append("optimized " + repr(expression))
    plan = guard("plan", lambda: planner.Plan(expression))
    if plan is None:
        return "\n".join(out)
    ex = PythonExecutor(tables=tables_)
    gen = ex.generator
    orig = gen.generate
    code = collections.Counter()

    def rec(expr, *a, **k):
        s = orig(expr, *a, **k)
        code[s] += 1
        return s

    gen.generate = rec
    res = guard("execute", lambda: ex.execute(plan))
    if res is not None:
        out.append(f"columns {list(res.columns)}")
        out.append("rows " + repr(sorted(repr(r) for r in res.rows)))
    # Every step of every plan, generated whether or not the few rows reach it.
    seen = set()
    todo = [("root", plan)]
    while todo:
        name, p = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for st in sorted(p.dag, key=_step):
            out.append(f"step {name} {_step(st)}")
            for x in _exprs(st):
                guard("gen", lambda x=x: rec(ex._replace_subqueries(x)))
        for n, (sp, _) in list(ex._subquery_plans.items()):
            if n not in seen:
                todo.append((n, sp))
    gen.generate = orig
    # Subquery plan names follow registration order, which follows step order.
    by_name = {n: s for s, n in ex._plan_names_by_sql.items()}
    norm = lambda s: _SQ.sub(lambda m: "<" + by_name.get(m.group(0), m.group(0)) + ">", s)  # noqa: E731
    out = [norm(x) for x in out]
    out.extend(sorted(f"code {norm(s)} x{n}" for s, n in code.items()))
    return "\n".join(out)


def suites(repo: str):
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from tests.helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    return {
        "tests/test_executor.py::TestExecutor::test_execute_tpcds": (
            "tpc-ds", TPCDS_SCHEMA, list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))),
        "tests/test_executor.py::TestExecutor::test_execute_tpch": (
            "tpc-h", TPCH_SCHEMA, list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))),
    }


def trace_all(repo: str, only: str | None = None, indices=None) -> dict:
    """test id -> {query index: trace} (executed queries only, or those in `indices`)."""
    out = {}
    for tid, (suite, schema, pairs) in suites(repo).items():
        if only and tid != only:
            continue
        tables = load_tables(repo, suite, schema)
        out[tid] = {i: trace_query(sql, schema, tables) for i, (meta, sql, _) in enumerate(pairs)
                    if meta.get("execute") and (indices is None or i in indices)}
        out[tid][-1] = parse_all(pairs)
    return out


def parse_all(pairs) -> str:
    """The test parses and pickles every query (executed or not) to submit it."""
    from sqlglot import parse_one

    out = []
    for i, (_, sql, _) in enumerate(pairs):
        try:
            pickle.loads(pickle.dumps(parse_one(sql)))
            out.append(f"{i} ok")
        except Exception as e:  # noqa: BLE001
            out.append(f"{i} {type(e).__name__}: {e}")
    return "\n".join(out)


def digest(s: str) -> str:
    import hashlib

    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def digests(repo: str, tid: str, indices=None) -> dict:
    return {i: digest(t) for i, t in trace_all(repo, tid, indices).get(tid, {}).items()}


if __name__ == "__main__":
    repo, dst = sys.argv[1], sys.argv[2]
    os.chdir(repo)
    sys.path.insert(0, repo)
    t = {tid: {i: digest(x) for i, x in q.items()} for tid, q in trace_all(repo).items()}
    with open(dst, "wb") as f:
        pickle.dump(t, f, protocol=5)
    print({k: len(v) for k, v in t.items()}, file=sys.stderr)
