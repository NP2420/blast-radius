"""Run sqlglot tests locally on a patched tree, cheaply and safely.

The parent preloads third-party modules once. For each commit a child is
forked: it builds a symlink farm in /tmp (patched files real, the rest links
to the pristine checkout), drops every sqlglot/tests module, imports from the
farm and runs the requested unittest methods, streaming one result per test
over a pipe. A test that overruns its time limit kills the child and counts
as failing; the child is restarted for the tests still pending.
"""
from __future__ import annotations

import importlib
import json
import os
import select
import shutil
import signal
import sys
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))

PRELOAD = ("duckdb", "pandas", "numpy", "dateutil", "dateutil.parser", "pytz", "json",
           "decimal", "datetime", "doctest", "typing", "collections", "itertools",
           "functools", "re", "math", "unittest.mock")


def preload() -> None:
    for name in PRELOAD:
        try:
            importlib.import_module(name)
        except Exception:
            pass


import gc as _gc

_GC_DEFAULT = _gc.get_threshold()  # captured at import, before main raises it


def _child_gc() -> None:
    _gc.set_threshold(*_GC_DEFAULT)


FP_MAXKEYS = int(os.environ.get("BR_FP_MAXKEYS", "50"))

_WARM = [False]


def warm_import(repo: str) -> bool:
    """Import pristine sqlglot (every module) in this process, for hot-swap children."""
    if _WARM[0]:
        return True
    try:
        sys.path.insert(0, repo)
        try:
            import fingerprint
            fingerprint.import_all()
        finally:
            sys.path.remove(repo)
        _WARM[0] = True
    except Exception:
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot."):
                del sys.modules[name]
        _WARM[0] = False
    return _WARM[0]


_PREWARM: dict = {}
LAZY_RES: dict = {}  # commit -> dialect classes registered by a bare `import sqlglot` (fp child)
_EXEC_CLS = "tests/test_executor.py::TestExecutor"


def prewarm_executor(repo: str) -> bool:
    """Build TestExecutor's TPC-H/TPC-DS row tables once (pure csv parsing into
    sqlglot executor Tables); hot children reuse them instead of re-parsing ~1.5 s."""
    if "exec" in _PREWARM:
        return True
    try:
        sys.path.insert(0, repo)
        try:
            import tests.test_executor as te
            k = te.TestExecutor
            k.setUpClass()
            k.tearDownClass()
            _PREWARM["exec"] = {"tpch_tables": k.tpch_tables, "tpcds_tables": k.tpcds_tables,
                                "tpch_sqls": k.tpch_sqls, "tpcds_sqls": k.tpcds_sqls}
        finally:
            sys.path.remove(repo)
            for name in list(sys.modules):
                if name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
        return True
    except Exception:
        _PREWARM.pop("exec", None)
        return False


def _use_prewarmed_executor(mod) -> None:
    """In a hot child: TestExecutor.setUpClass opens fresh DuckDB views but takes the
    row tables from the parent."""
    data = _PREWARM.get("exec")
    k = getattr(mod, "TestExecutor", None)
    if data is None or k is None:
        return
    import duckdb

    def setUpClass(cls):
        cls.tpch_conn = duckdb.connect()
        cls.tpcds_conn = duckdb.connect()
        for conn, directory, schema in ((cls.tpch_conn, mod.DIR_TPCH, mod.TPCH_SCHEMA),
                                        (cls.tpcds_conn, mod.DIR_TPCDS, mod.TPCDS_SCHEMA)):
            for table, columns in schema.items():
                conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{directory}{table}.csv.gz', delim='|', header=True, columns={columns})
                """
                )
        cls.tpch_tables = data["tpch_tables"]
        cls.tpcds_tables = data["tpcds_tables"]
        cls.cache = {}
        cls.tpch_sqls = list(data["tpch_sqls"])
        cls.tpcds_sqls = list(data["tpcds_sqls"])

    k.setUpClass = classmethod(setUpClass)


def build_farm(repo: str, farm: str, patched: dict[str, str | None]) -> None:
    """Symlink farm of repo at farm, with patched files written for real."""
    if os.path.exists(farm):
        shutil.rmtree(farm)
    os.makedirs(farm)
    real_dirs = {""}
    for p in patched:
        parts = p.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))
    for d in sorted(real_dirs, key=len):
        src = os.path.join(repo, d)
        dst = os.path.join(farm, d)
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(src):
            rel = f"{d}/{name}" if d else name
            if rel in real_dirs or rel in patched:
                continue
            os.symlink(os.path.join(src, name), os.path.join(dst, name))
    for p, text in patched.items():
        if text is None:
            continue
        with open(os.path.join(farm, p), "w", encoding="utf-8", newline="") as f:
            f.write(text)


EARLY_STOP = os.environ.get("BR_EARLY_STOP", "1") == "1"
_LAZY_TESTS = {"tests/test_executor.py::TestExecutor::test_execute_tpcds",
               "tests/test_executor.py::TestExecutor::test_execute_tpch"}


class _Result(unittest.TestResult):
    # A failing subtest already fails its test: with failfast set, unittest ends the
    # test there instead of running its remaining subtests. stop() is a no-op so a
    # failure never ends the rest of the suite.
    @property
    def failfast(self):
        return EARLY_STOP

    @failfast.setter
    def failfast(self, value):
        pass

    def stop(self):
        pass

    def __init__(self, out):
        super().__init__()
        self.out = out
        self.cur_fail = False

    def startTest(self, test):
        super().startTest(test)
        self.cur_fail = False
        sp = getattr(self, "syncpool", None)
        if sp is not None:
            tid = getattr(test, "_bt_id", None)
            a = getattr(self, "allowed", {}).get(tid)
            sp.ALLOWED[0] = set(a) if isinstance(a, (list, set, tuple)) else None
            # TPC executor tests read pool results in submission order: compute each
            # query only when asked, so a test stopped at its first failure skips the rest.
            sp.LAZY[0] = EARLY_STOP and tid in _LAZY_TESTS

    def _fail(self):
        self.cur_fail = True

    def addError(self, test, err):
        super().addError(test, err)
        if isinstance(test, unittest.TestCase):
            self._fail()
        else:  # class/module fixture error (_ErrorHolder)
            self._emit_raw({"fixture_error": str(test)})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._fail()

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._fail()

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._fail()

    def stopTest(self, test):
        super().stopTest(test)
        self._emit_raw({"id": getattr(test, "_bt_id", None), "failed": self.cur_fail})

    def _emit_raw(self, obj):
        os.write(self.out, (json.dumps(obj) + "\n").encode())


def _child(repo: str, farm: str, patched: dict, test_ids: list[str], wfd: int, allowed: dict | None = None,
           swaps: list | None = None) -> None:
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    _child_gc()
    try:
        build_farm(repo, farm, patched)
        os.chdir(farm)
        sys.dont_write_bytecode = True
        os.environ["PYTHONPATH"] = farm
        os.environ["SKIP_INTEGRATION"] = "0"
        sys.path[:] = [farm, _HERE] + [p for p in sys.path if p and os.path.realpath(p) != os.path.realpath(repo)]
        hot = False
        if swaps is not None and _WARM[0]:
            # Pristine sqlglot is already imported here: patch function code in place;
            # tests (and their fixtures) come fresh from the farm.
            for name in list(sys.modules):
                if name == "tests" or name.startswith("tests."):
                    del sys.modules[name]
            try:
                import hotswap
                hot = hotswap.apply(swaps, sys.modules)
            except Exception:
                hot = False
        if not hot:
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
        importlib.invalidate_caches()
        reuse = hot and "exec" in _PREWARM and not any(
            p.startswith(("tests/", "sqlglot/executor/")) for p in patched)
        _run_tests(wfd, test_ids, allowed, prewarmed=reuse)
    except BaseException:
        pass
    finally:
        os._exit(0)


MAX_NODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"
PROXY = os.environ.get("BR_PROXY", "1") == "1"
PROXY_LIMIT_S = float(os.environ.get("BR_PROXY_LIMIT_S", "2.5"))


def _max_nodes_proxy() -> bool:
    """test_max_nodes at reduced width: the same assertions on the node limit, its
    error message and recovery, with limits scaled to the smaller statement (the
    real test parses ~2M nodes in ~25 s). A run much slower than usual counts as a
    failure, since the real test would time out. True = pass."""
    import resource
    t0 = resource.getrusage(resource.RUSAGE_SELF)
    try:
        from sqlglot import exp, parse_one
        from sqlglot.errors import ParseError
        if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
            return False
        cols = [f"col{i}" for i in range(500)]
        if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select):
            return False
        expr = "x" + "=x" * 100
        sql = "SELECT " + ",".join([expr] * 200)  # ~40k nodes
        for limit, needle in ((20_000, "Maximum number of AST nodes"), (5_000, "(5000)")):
            try:
                parse_one(sql, max_nodes=limit)
                return False
            except ParseError as e:
                if needle not in str(e):
                    return False
        try:
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
            return False
        except ParseError:
            pass
        if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
            return False
    except BaseException:  # noqa: BLE001
        return False
    t1 = resource.getrusage(resource.RUSAGE_SELF)
    return (t1.ru_utime + t1.ru_stime) - (t0.ru_utime + t0.ru_stime) <= PROXY_LIMIT_S


GEN_MEM_ID = "tests/test_generator.py::TestGenerator::test_generator_memory"


def _gen_mem_dialects(mods: list) -> list:
    """Dialects whose dialect/generator/parser/tokenizer classes come from, or whose
    modules import, one of the patched dialect-specific modules."""
    import sys as _sys
    from sqlglot.dialects import Dialect, Dialects
    out = []
    for name in Dialects:
        try:
            d = Dialect.get_or_raise(name)
            classes = [type(d), d.generator_class, d.parser_class, d.tokenizer_class]
            owners = {c.__module__ for k in classes for c in k.__mro__}
        except Exception:  # noqa: BLE001
            out.append(name)
            continue
        hit = bool(owners & set(mods))
        if not hit:
            for o in owners:
                f = getattr(_sys.modules.get(o), "__file__", None)
                try:
                    src = open(f, encoding="utf-8").read() if f else ""
                except OSError:
                    src = ""
                if any(m in src for m in mods):
                    hit = True
                    break
        if hit:
            out.append(name)
    return out


def _run_tests(wfd: int, test_ids: list, allowed: dict | None, prewarmed: bool = False) -> None:
    """Run unittest methods in this (already prepared) process, streaming results."""
    if PROXY and MAX_NODES_ID in test_ids:
        ok = _max_nodes_proxy()
        os.write(wfd, (json.dumps({"id": MAX_NODES_ID, "failed": not ok}) + "\n").encode())
        test_ids = [t for t in test_ids if t != MAX_NODES_ID]
    if True:
        import syncpool
        syncpool.install()
        result = _Result(wfd)
        result.allowed = allowed or {}
        result.syncpool = syncpool
        # Group by module and class, keeping order.
        groups: dict[tuple, list] = {}
        for tid in test_ids:
            file, _, rest = tid.partition("::")
            cls, _, meth = rest.partition("::")
            groups.setdefault((file, cls), []).append((tid, meth))
        for (file, cls), items in groups.items():
            modname = file[:-3].replace("/", ".")
            try:
                mod = importlib.import_module(modname)
                if prewarmed and f"{file}::{cls}" == _EXEC_CLS:
                    _use_prewarmed_executor(mod)
                gm = (allowed or {}).get(GEN_MEM_ID)
                if isinstance(gm, dict) and modname == "tests.test_generator":
                    # Only the dialects the patch can reach run the leak check.
                    mod.Dialects = _gen_mem_dialects(gm["mods"])
                klass = getattr(mod, cls)
                cases = []
                for tid, meth in items:
                    tc = klass(meth)
                    tc._bt_id = tid
                    cases.append(tc)
            except BaseException:
                for tid, _ in items:
                    result._emit_raw({"id": tid, "failed": True})
                continue
            suite = unittest.TestSuite(cases)
            before = len(result.errors)
            suite.run(result)
            # A failing setUpClass reports one error for the class and runs no
            # test: every test of the class fails.
            ran = {c._bt_id for c in cases}
            if any(not isinstance(t, unittest.TestCase) for t, _ in result.errors[before:]):
                for tid in ran:
                    result._emit_raw({"id": tid, "failed": True})


def _cls(tid: str) -> str:
    return "::".join(tid.split("::")[:2])


class LocalRunner:
    def __init__(self, repo: str, tmp: str, seconds: dict[str, float], log=None):
        self.repo = repo
        self.tmp = tmp
        self.seconds = seconds
        self.log = log or (lambda *a: None)

    def limit(self, tid: str) -> float:
        return 2.0 + 6.0 * self.seconds.get(tid, 1.0)

    def run_many(self, jobs: list[tuple[str, dict, list[str]]], parallel: int = 2,
                 budget_s: float = 1e9) -> dict[str, dict[str, bool]]:
        """jobs: (commit, patched, test_ids). Returns commit -> {test_id: failed}.

        Tests with no entry when the budget runs out are unknown (caller includes them).
        """
        results: dict[str, dict[str, bool]] = {j[0]: {} for j in jobs}
        pending = [(j[0], j[1], list(j[2]), j[3] if len(j) > 3 else None, j[4] if len(j) > 4 else None)
                   for j in jobs if j[2]]
        active: dict[int, dict] = {}  # rfd -> state
        stop_at = time.monotonic() + budget_s
        while pending or active:
            if time.monotonic() >= stop_at:
                self.log("local budget exhausted; remaining tests included unrun")
                for fd in list(active):
                    st = active.pop(fd)
                    try:
                        os.kill(st["pid"], signal.SIGKILL)
                        os.waitpid(st["pid"], 0)
                    except (ProcessLookupError, ChildProcessError):
                        pass
                    os.close(fd)
                    shutil.rmtree(st["farm"], ignore_errors=True)
                break
            while pending and len(active) < parallel:
                job = pending.pop(0)
                self._spawn(active, job[0], job[1], job[2], job[3], job[4])
            fds = list(active)
            now = time.monotonic()
            timeout = max(0.0, min(min(st["deadline"] for st in active.values()), stop_at) - now)
            ready, _, _ = select.select(fds, [], [], timeout)
            for fd in ready:
                st = active[fd]
                chunk = os.read(fd, 65536)
                if not chunk:
                    self._finish(active, fd, results, pending, eof=True)
                    continue
                st["buf"] += chunk
                while b"\n" in st["buf"]:
                    line, st["buf"] = st["buf"].split(b"\n", 1)
                    msg = json.loads(line)
                    tid = msg.get("id")
                    if tid is None:
                        continue
                    if tid in st["left"]:
                        st["left"].remove(tid)
                        results[st["commit"]][tid] = results[st["commit"]].get(tid, False) or msg["failed"]
                    if st["left"]:
                        nxt = st["left"][0]
                        extra = 15.0 if _cls(nxt) != _cls(tid) else 0.0  # setUpClass of a new class
                        st["deadline"] = time.monotonic() + self.limit(nxt) + extra
                    else:
                        st["deadline"] = time.monotonic() + 5.0
            now = time.monotonic()
            for fd in list(active):
                st = active[fd]
                if now >= st["deadline"]:
                    self._finish(active, fd, results, pending, eof=False)
        return results

    def _spawn(self, active, commit, patched, tids, allowed=None, swaps=None):
        rfd, wfd = os.pipe()
        farm = os.path.join(self.tmp, f"w_{commit}_{len(tids)}_{int(time.monotonic() * 1000) % 100000}")
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            _child(self.repo, farm, patched, tids, wfd, allowed, swaps)
        os.close(wfd)
        # First test also absorbs import time.
        active[rfd] = {"pid": pid, "commit": commit, "patched": patched, "left": list(tids), "allowed": allowed,
                       "swaps": swaps,
                       "buf": b"", "farm": farm,
                       "deadline": time.monotonic() + 15.0 + self.limit(tids[0])}

    def _finish(self, active, fd, results, pending, eof: bool):
        st = active.pop(fd)
        if not eof:
            try:
                os.kill(st["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            _, _, ru = os.wait4(st["pid"], 0)
            if self.log and os.environ.get("BR_CPU_LOG"):
                self.log(f"cpu child {st['commit']} {ru.ru_utime + ru.ru_stime:.2f}s tests={len(st.get('ntests', st['left']))}")
        except ChildProcessError:
            pass
        os.close(fd)
        shutil.rmtree(st["farm"], ignore_errors=True)
        left = st["left"]
        if left:
            # The test in progress failed (timeout/crash); retry the rest.
            bad = left[0]
            results[st["commit"]][bad] = True
            self.log(f"{st['commit']}: {bad} {'crashed' if eof else 'timed out'}")
            if left[1:]:
                pending.insert(0, (st["commit"], st["patched"], left[1:], st.get("allowed"), st.get("swaps")))


def _fp_child(repo: str, farm: str, patched: dict, base_path: str, wfd: int) -> None:
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    _child_gc()
    out = ["<error>"]
    try:
        build_farm(repo, farm, patched)
        os.chdir(farm)
        sys.dont_write_bytecode = True
        sys.path[:] = [farm, _HERE] + [p for p in sys.path if p and os.path.realpath(p) != os.path.realpath(repo)]
        for name in list(sys.modules):
            if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                del sys.modules[name]
        importlib.invalidate_caches()
        import fingerprint
        base = _FP_BASE[0]
        if base is None:
            import pickle
            with open(base_path, "rb") as f:
                base = pickle.load(f)
        out = fingerprint.diff(base, fingerprint.fingerprint(loose_only=True))
    except BaseException as e:  # noqa: BLE001
        out = ["<error> " + repr(e)[:200]]
    finally:
        try:
            os.write(wfd, (json.dumps(out[:FP_MAXKEYS]) + "\n").encode())
        finally:
            os._exit(0)


_FP_BASE = [None]


def fp_check_many(repo: str, tmp: str, base_path: str, jobs: list, parallel: int = 2,
                  timeout: float = 40.0, log=None) -> dict:
    """jobs: [(commit, patched)] -> {commit: list of changed state keys (["<error>"] on failure)}."""
    res: dict = {}
    if _FP_BASE[0] is None and jobs:
        import pickle
        with open(base_path, "rb") as f:
            _FP_BASE[0] = pickle.load(f)  # loaded once; forked checks share it
    pending = list(jobs)
    active: dict = {}
    while pending or active:
        while pending and len(active) < parallel:
            c, patched = pending.pop(0)
            rfd, wfd = os.pipe()
            farm = os.path.join(tmp, f"fp_{c}")
            pid = os.fork()
            if pid == 0:
                os.close(rfd)
                _fp_child(repo, farm, patched, base_path, wfd)
            os.close(wfd)
            active[rfd] = (pid, c, farm, time.monotonic() + timeout, [b""])
        ready, _, _ = select.select(list(active), [], [], 0.5)
        now = time.monotonic()
        for fd in list(active):
            pid, c, farm, deadline, buf = active[fd]
            done = False
            if fd in ready:
                chunk = os.read(fd, 1 << 20)
                if chunk:
                    buf[0] += chunk
                else:
                    done = True
            if not done and now < deadline:
                continue
            if not done:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass
            os.close(fd)
            shutil.rmtree(farm, ignore_errors=True)
            try:
                res[c] = json.loads(buf[0].decode().strip() or '["<error> empty"]')
            except Exception:
                res[c] = ["<error> bad output"]
            del active[fd]
    return res


def _fp_test_child(repo: str, farm: str, patched: dict, base_path: str, wfd: int, cmd_rfd: int) -> None:
    """Fingerprint the patched import, report it, then run the tests the parent sends."""
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    _child_gc()
    try:
        out = ["<error>"]
        try:
            build_farm(repo, farm, patched)
            os.chdir(farm)
            sys.dont_write_bytecode = True
            os.environ["PYTHONPATH"] = farm
            os.environ["SKIP_INTEGRATION"] = "0"
            sys.path[:] = [farm, _HERE] + [p for p in sys.path if p and os.path.realpath(p) != os.path.realpath(repo)]
            for name in list(sys.modules):
                if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                    del sys.modules[name]
            importlib.invalidate_caches()
            # What tests/dialects/test_dialect.py::test_lazy_load checks in a fresh
            # interpreter: importing sqlglot alone registers exactly one dialect class.
            try:
                import sqlglot
                lazy_n = len(sqlglot.Dialect._classes)
            except BaseException:  # noqa: BLE001
                lazy_n = -1
            import fingerprint
            base = _FP_BASE[0]
            if base is None:
                import pickle
                with open(base_path, "rb") as f:
                    base = pickle.load(f)
            out = fingerprint.diff(base, fingerprint.fingerprint(loose_only=True))
        except BaseException as e:  # noqa: BLE001
            out = ["<error> " + repr(e)[:200]]
        os.write(wfd, (json.dumps({"fp": out[:FP_MAXKEYS], "lazy": locals().get("lazy_n", -1)}) + "\n").encode())
        if out and out[0].startswith("<error>"):
            return  # cannot import: parent will list tests without local runs
        buf = b""
        while b"\n" not in buf:
            chunk = os.read(cmd_rfd, 1 << 20)
            if not chunk:
                return
            buf += chunk
        cmd = json.loads(buf.split(b"\n", 1)[0])
        if cmd.get("tests"):
            _run_tests(wfd, cmd["tests"], cmd.get("allowed"))
    except BaseException:
        pass
    finally:
        os._exit(0)


def fp_then_tests(repo: str, tmp: str, base_path: str, jobs: list, decide, seconds: dict,
                  parallel: int = 2, budget_s: float = 1e9, log=None):
    """Fingerprint + local tests in one process per commit.

    jobs: [(commit, patched)]; decide(commit, changed) -> (test_ids, allowed) to run locally.
    Returns (fp_results {commit: changed}, local_results {commit: {test: failed}}, retry_jobs)
    where retry_jobs are (commit, patched, test_ids, allowed) left unfinished after a timeout.
    """
    if _FP_BASE[0] is None and jobs:
        import pickle
        with open(base_path, "rb") as f:
            _FP_BASE[0] = pickle.load(f)
    fp_res: dict = {}
    loc_res: dict = {c: {} for c, _ in jobs}
    retry: list = []
    pending = list(jobs)
    active: dict = {}
    stop_at = time.monotonic() + budget_s

    def limit(tid):
        return 2.0 + 6.0 * seconds.get(tid, 1.0)

    while pending or active:
        while pending and len(active) < parallel:
            c, patched = pending.pop(0)
            rfd, wfd = os.pipe()
            crfd, cwfd = os.pipe()
            farm = os.path.join(tmp, f"fpt_{c}")
            pid = os.fork()
            if pid == 0:
                os.close(rfd)
                os.close(cwfd)
                _fp_test_child(repo, farm, patched, base_path, wfd, crfd)
            os.close(wfd)
            os.close(crfd)
            active[rfd] = {"pid": pid, "commit": c, "patched": patched, "cw": cwfd, "farm": farm,
                           "buf": b"", "stage": "fp", "left": [], "allowed": None,
                           "deadline": time.monotonic() + 40.0}
        if time.monotonic() >= stop_at:
            for fd in list(active):
                st = active.pop(fd)
                try:
                    os.kill(st["pid"], signal.SIGKILL)
                    os.waitpid(st["pid"], 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
                os.close(fd)
                try:
                    os.close(st["cw"])
                except OSError:
                    pass
                shutil.rmtree(st["farm"], ignore_errors=True)
                fp_res.setdefault(st["commit"], ["<error> budget"])
            break
        now = time.monotonic()
        timeout = max(0.0, min(min(st["deadline"] for st in active.values()), stop_at) - now)
        ready, _, _ = select.select(list(active), [], [], timeout)
        for fd in ready:
            st = active[fd]
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                st["eof"] = True
            st["buf"] += chunk
            while b"\n" in st["buf"]:
                line, st["buf"] = st["buf"].split(b"\n", 1)
                msg = json.loads(line)
                if "fp" in msg:
                    c = st["commit"]
                    fp_res[c] = msg["fp"]
                    LAZY_RES[c] = msg.get("lazy", -1)
                    tests, allowed = ([], None)
                    if not (msg["fp"] and msg["fp"][0].startswith("<error>")):
                        tests, allowed = decide(c, msg["fp"])
                    st["left"] = list(tests)
                    st["allowed"] = allowed
                    try:
                        os.write(st["cw"], (json.dumps({"tests": tests, "allowed": allowed}) + "\n").encode())
                    except OSError:
                        pass
                    st["stage"] = "tests"
                    st["deadline"] = time.monotonic() + (15.0 + limit(tests[0]) if tests else 10.0)
                    continue
                tid = msg.get("id")
                if tid is not None and tid in st["left"]:
                    st["left"].remove(tid)
                    loc_res[st["commit"]][tid] = loc_res[st["commit"]].get(tid, False) or msg["failed"]
                    if st["left"]:
                        nxt = st["left"][0]
                        extra = 15.0 if _cls(nxt) != _cls(tid) else 0.0
                        st["deadline"] = time.monotonic() + limit(nxt) + extra
                    else:
                        st["deadline"] = time.monotonic() + 5.0
        now = time.monotonic()
        for fd in list(active):
            st = active[fd]
            done = st.get("eof") or now >= st["deadline"]
            if not done:
                continue
            active.pop(fd)
            if not st.get("eof"):
                try:
                    os.kill(st["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                _, _, ru = os.wait4(st["pid"], 0)
                if log and os.environ.get("BR_CPU_LOG"):
                    log(f"cpu fpchild {st['commit']} {ru.ru_utime + ru.ru_stime:.2f}s stage={st['stage']}")
            except ChildProcessError:
                pass
            os.close(fd)
            try:
                os.close(st["cw"])
            except OSError:
                pass
            shutil.rmtree(st["farm"], ignore_errors=True)
            c = st["commit"]
            if st["stage"] == "fp":
                fp_res.setdefault(c, ["<error> no fingerprint"])
            elif st["left"]:
                bad = st["left"][0]
                loc_res[c][bad] = True
                if log:
                    log(f"{c}: {bad} {'crashed' if st.get('eof') else 'timed out'}")
                if st["left"][1:]:
                    retry.append((c, st["patched"], st["left"][1:], st["allowed"]))
    return fp_res, loc_res, retry
