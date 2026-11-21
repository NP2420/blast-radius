"""pytest plugin: lines of the dialect registry code each test runs when it starts
with an empty registry (the state tests/test_dialect_entry_points.py leaves behind
for every test pytest runs after it in the same process).

Each test runs in a forked child after Dialect._classes.clear(); only frames in the
registry files are traced. Writes REG_OUT/t/<nodeid>.json: {"nodeid", "lines": {file: [..]}}.
"""
import importlib
import json
import os
import pkgutil
import sys

OUT = os.environ.get("REG_OUT", "/tmp/reg")
ROOT = os.path.realpath(os.environ.get("MAP_ROOT", os.getcwd())) + os.sep
TARGETS = {ROOT + "sqlglot/dialects/dialect.py", ROOT + "sqlglot/dialects/__init__.py"}
_LINES: dict = {}


def _local(frame, event, arg):
    if event == "line":
        _LINES.setdefault(frame.f_code.co_filename, set()).add(frame.f_lineno)
    return _local


def _global(frame, event, arg):
    if frame.f_code.co_filename in TARGETS:
        return _local
    return None


def pytest_configure(config):
    sys.path.insert(0, os.environ.get("MAP_SELECTOR", "/home/claude/dev/br/selector"))
    import syncpool
    syncpool.install()
    os.makedirs(os.path.join(OUT, "t"), exist_ok=True)
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(m.name)
        except Exception:
            pass


def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            from sqlglot.dialects.dialect import Dialect
            Dialect._classes.clear()
            sys.settrace(_global)
            runtestprotocol(item, nextitem=None, log=False)
            sys.settrace(None)
            out = {"nodeid": item.nodeid,
                   "lines": {os.path.relpath(f, ROOT): sorted(v) for f, v in _LINES.items()}}
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
