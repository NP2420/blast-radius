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

PRELOAD = ("duckdb", "pandas", "numpy", "dateutil", "dateutil.parser", "pytz", "json",
           "decimal", "datetime", "doctest", "typing", "collections", "itertools",
           "functools", "re", "math", "unittest.mock")


def preload() -> None:
    for name in PRELOAD:
        try:
            importlib.import_module(name)
        except Exception:
            pass


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


class _Result(unittest.TestResult):
    def __init__(self, out):
        super().__init__()
        self.out = out
        self.cur_fail = False

    def startTest(self, test):
        super().startTest(test)
        self.cur_fail = False

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


def _child(repo: str, farm: str, patched: dict, test_ids: list[str], wfd: int) -> None:
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    try:
        build_farm(repo, farm, patched)
        os.chdir(farm)
        sys.dont_write_bytecode = True
        os.environ["PYTHONPATH"] = farm
        os.environ["SKIP_INTEGRATION"] = "0"
        sys.path[:] = [farm] + [p for p in sys.path if p and os.path.realpath(p) != os.path.realpath(repo)]
        for name in list(sys.modules):
            if name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests.")):
                del sys.modules[name]
        importlib.invalidate_caches()
        import syncpool
        syncpool.install()
        result = _Result(wfd)
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
    except BaseException:
        pass
    finally:
        os._exit(0)


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
        results: dict[str, dict[str, bool]] = {c: {} for c, _, _ in jobs}
        pending = [(c, p, list(t)) for c, p, t in jobs if t]
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
                c, p, tids = pending.pop(0)
                self._spawn(active, c, p, tids)
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

    def _spawn(self, active, commit, patched, tids):
        rfd, wfd = os.pipe()
        farm = os.path.join(self.tmp, f"w_{commit}_{len(tids)}_{int(time.monotonic() * 1000) % 100000}")
        pid = os.fork()
        if pid == 0:
            os.close(rfd)
            _child(self.repo, farm, patched, tids, wfd)
        os.close(wfd)
        # First test also absorbs import time.
        active[rfd] = {"pid": pid, "commit": commit, "patched": patched, "left": list(tids),
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
            os.waitpid(st["pid"], 0)
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
                pending.insert(0, (st["commit"], st["patched"], left[1:]))
