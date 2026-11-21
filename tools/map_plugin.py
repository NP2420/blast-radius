"""pytest plugin: per-test dependency maps, each test in its own forked child.

Parent: coverage runs from configure through collection plus an eager import
of every sqlglot module; those lines are "import-time" (MAP_OUT/import.json).
Child (one per test, forked after imports, so no cache from earlier tests can
hide a dependency): line coverage, files opened, subprocesses, receiver classes
(sqlglot MRO of type(self)/cls of every sqlglot method call), outcome.
Writes MAP_OUT/t/<n>.json per test.
"""
import importlib
import json
import os
import pkgutil
import sys

import pytest

OUT = os.environ.get("MAP_OUT", "/tmp/maps")
ROOT = os.path.realpath(os.environ.get("MAP_ROOT", os.getcwd())) + os.sep
SQLGLOT = ROOT + "sqlglot" + os.sep
TRACE_RECV = os.environ.get("MAP_RECV", "1") == "1"

_parent_cov = None
_opens: set = set()
_procs: list = []
_recv_types: set = set()
_auditing = [False]


def _rel(path):
    try:
        p = os.path.realpath(os.fspath(path))
    except Exception:
        return None
    return p[len(ROOT):] if p.startswith(ROOT) else None


def _audit(event, args):
    if not _auditing[0]:
        return
    if event == "open":
        a = args[0] if args else None
        if isinstance(a, (str, bytes, os.PathLike)):
            rel = _rel(a)
            if rel and "__pycache__" not in rel:
                _opens.add(rel)
    elif event == "subprocess.Popen":
        try:
            _procs.append([str(x) for x in args[1]][:30])
        except Exception:
            _procs.append(["?"])


def _profile(frame, event, arg):
    if event != "call":
        return
    code = frame.f_code
    if not code.co_argcount or not code.co_filename.startswith(SQLGLOT):
        return
    first = code.co_varnames[0]
    if first == "self":
        _recv_types.add(type(frame.f_locals.get("self")))
    elif first == "cls":
        obj = frame.f_locals.get("cls")
        if isinstance(obj, type):
            _recv_types.add(obj)


def _lines(cov) -> dict:
    data = cov.get_data()
    res = {}
    for f in data.measured_files():
        rel = _rel(f)
        if rel:
            ls = data.lines(f)
            if ls:
                res[rel] = sorted(ls)
    return res


def pytest_configure(config):
    global _parent_cov
    sys.path.insert(0, os.environ.get("MAP_SELECTOR", "/home/claude/dev/br/selector"))
    import syncpool
    syncpool.install()
    os.makedirs(os.path.join(OUT, "t"), exist_ok=True)
    sys.addaudithook(_audit)
    import coverage
    _parent_cov = coverage.Coverage(data_file=None, source=[ROOT + "sqlglot", ROOT + "tests"])
    _auditing[0] = True
    _parent_cov.start()
    # Phase 1: sqlglot's own import (module bodies, class bodies, metaclasses).
    _parent_cov.switch_context("sqlglot-import")
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # pragma: no cover
            print("import fail", m.name, e, file=sys.stderr)
    _parent_cov.switch_context("collect:?")


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    # Phase 2: each test module's import during collection, by file.
    if _parent_cov is not None and isinstance(collector, pytest.Module):
        rel = _rel(collector.path) or str(collector.path)
        _parent_cov.switch_context("collect:" + rel)
        yield
        _parent_cov.switch_context("collect:?")
    else:
        yield


def pytest_collection_finish(session):
    global _parent_cov
    _parent_cov.stop()
    _auditing[0] = False
    parent_opens = sorted(_opens)
    _opens.clear()
    _procs.clear()
    with open(os.path.join(OUT, "parent_opens.%d.json" % os.getpid()), "w") as f:
        json.dump(parent_opens, f)
    if "PYTEST_XDIST_WORKER" not in os.environ or os.environ.get("PYTEST_XDIST_WORKER") == "gw0":
        data = _parent_cov.get_data()
        by_ctx: dict = {}
        for f in data.measured_files():
            rel = _rel(f)
            if not rel:
                continue
            for ln, ctxs in data.contexts_by_lineno(f).items():
                for c in ctxs:
                    by_ctx.setdefault(c, {}).setdefault(rel, []).append(ln)
        with open(os.path.join(OUT, "import.json"), "w") as f:
            json.dump(by_ctx.get("sqlglot-import", {}), f)
        with open(os.path.join(OUT, "collect.json"), "w") as f:
            json.dump({c[len("collect:"):]: v for c, v in by_ctx.items() if c.startswith("collect:")}, f)
    _parent_cov.erase()
    _parent_cov = None


def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol
    import coverage

    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            cov = coverage.Coverage(data_file=None, source=[ROOT + "sqlglot", ROOT + "tests"])
            _auditing[0] = True
            cov.start()
            if TRACE_RECV:
                sys.setprofile(_profile)
            reports = runtestprotocol(item, nextitem=None, log=False)
            sys.setprofile(None)
            cov.stop()
            _auditing[0] = False
            recv = set()
            for t in _recv_types:
                for c in getattr(t, "__mro__", ()):
                    mod = getattr(c, "__module__", "") or ""
                    if mod.startswith("sqlglot"):
                        recv.add(mod + ":" + c.__qualname__)
            out = {
                "nodeid": item.nodeid,
                "failed": any(r.failed for r in reports),
                "lines": _lines(cov),
                "opens": sorted(_opens),
                "procs": _procs,
                "recv": sorted(recv),
                "tcls": [c.__module__ + ":" + c.__qualname__ for c in getattr(item.cls, "__mro__", ())
                         if (c.__module__ or "").startswith("tests")],
            }
            safe = item.nodeid.replace("/", "_").replace("::", "__")
            with open(os.path.join(OUT, "t", safe[:200] + ".json"), "w") as f:
                json.dump(out, f)
        except BaseException as e:  # pragma: no cover
            print("child error", item.nodeid, repr(e), file=sys.stderr)
            status = 1
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(status)
    os.waitpid(pid, 0)
    return True
