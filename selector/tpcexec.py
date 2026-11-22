"""Executor outputs of TestExecutor's TPC-DS queries, for patches that change only
sqlglot/executor.

test_execute_tpcds compares, per executed query, the Python executor's result table
with DuckDB's result for the same SQL (transpiled by sqlglot). When a patch touches
only the executor, parsing and transpiling are the pinned ones, so DuckDB's side is
unchanged; if the patched executor returns the same table as the pinned executor for
every executed query, the test passes as it does at the pinned commit. This module
runs the queries exactly as the test's workers do (its setUpClass tables, its
initializer and mp_execute) and digests each result table.

usage: tpcexec.py REPO OUT.pkl   (pinned digests, made at image build)
"""
from __future__ import annotations

import hashlib
import os
import pickle
import sys

TID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def _digest(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def outputs(repo: str, indices=None, want: dict | None = None, caps: dict | None = None, times: dict | None = None) -> dict:
    """query index -> digest of the executor's result. With `want`, stops at the first
    query whose digest differs from it (that entry is still returned)."""
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from sqlglot import parse_one

    import tests.test_executor as te
    from tests.helpers import TPCDS_SCHEMA

    cls = te.TestExecutor
    cls.setUpClass()
    try:
        te.initializer(TPCDS_SCHEMA, cls.tpcds_tables)
        out = {}
        for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
            if not meta.get("execute") or (indices is not None and i not in indices):
                continue
            import resource
            import signal

            t0 = resource.getrusage(resource.RUSAGE_SELF).ru_utime
            lim = (caps or {}).get(i)

            def _on(sig, frm):
                raise TimeoutError(f"query {i} over {lim}s CPU")

            if lim:
                old = signal.signal(signal.SIGPROF, _on)
                signal.setitimer(signal.ITIMER_PROF, lim)
            try:
                t = te.mp_execute(parse_one(sql), meta)
                s = repr((list(t.columns), [tuple(r) for r in t.rows]))
            except Exception as e:  # noqa: BLE001
                s = f"raised {type(e).__name__}: {e}"
            finally:
                if lim:
                    signal.setitimer(signal.ITIMER_PROF, 0)
                    signal.signal(signal.SIGPROF, old)
            if times is not None:
                times[i] = resource.getrusage(resource.RUSAGE_SELF).ru_utime - t0
            out[i] = _digest(s)
            if want is not None and want.get(i) != out[i]:
                break
        return out
    finally:
        try:
            cls.tearDownClass()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    repo, dst = sys.argv[1], sys.argv[2]
    os.chdir(repo)
    times: dict = {}
    d = outputs(repo, times=times)
    with open(dst, "wb") as f:
        pickle.dump({TID: d, "times": {TID: times}}, f, protocol=5)
    print(f"{len(d)} executed queries", file=sys.stderr)
