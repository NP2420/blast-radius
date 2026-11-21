"""pytest plugin: comparison-operator value profile, one forked child per test.

Every single-operator comparison inside a sqlglot function or lambda is rewritten
to call br_cmp__(site, op, a, b). It returns the original result and, in a test
child, also evaluates the related operators on the same operands and records
which ones would have given a different result (or raised differently). A patch
that only swaps that operator behaves exactly like the pinned code for a test
until such a difference happens, so the swap can only affect those tests.

Writes CMP_OUT/t/<nodeid>.json: {"nodeid", "failed", "diff": [[site, alt]...],
"unknown": [site...]} and CMP_OUT/sites.json (every instrumented site).
"""
import ast
import builtins
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import operator
import os
import pkgutil
import sys

OUT = os.environ.get("CMP_OUT", "/tmp/cmp")
ROOT = os.path.realpath(os.environ.get("MAP_ROOT", os.getcwd())) + os.sep
SQLGLOT = ROOT + "sqlglot" + os.sep

OPS = {
    "Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
    "Gt": operator.gt, "GtE": operator.ge, "Is": operator.is_, "IsNot": operator.is_not,
    "In": lambda a, b: a in b, "NotIn": lambda a, b: a not in b,
}
RICH = ["Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"]
FAMILY = {
    "Eq": [o for o in RICH if o != "Eq"] + ["Is", "IsNot"],
    "NotEq": [o for o in RICH if o != "NotEq"] + ["Is", "IsNot"],
    "Lt": [o for o in RICH if o != "Lt"], "LtE": [o for o in RICH if o != "LtE"],
    "Gt": [o for o in RICH if o != "Gt"], "GtE": [o for o in RICH if o != "GtE"],
    "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
    "In": ["NotIn"], "NotIn": ["In"],
}
SAFE_CONTAINERS = (list, tuple, set, frozenset, dict, str, bytes, range)

_REC = [False]
_DIFF: set = set()
_UNKNOWN: set = set()
_DONE: set = set()  # sites whose every alternative is already recorded
_SITES: set = set()


def br_cmp__(site, op, a, b):
    r = OPS[op](a, b)
    if _REC[0] and site not in _DONE:
        alts = FAMILY[op]
        n = 0
        for alt in alts:
            key = (site, alt)
            if key in _DIFF:
                n += 1
                continue
            if alt in ("In", "NotIn") and not isinstance(b, SAFE_CONTAINERS):
                _UNKNOWN.add(site)
                n += 1
                continue
            try:
                ar = OPS[alt](a, b)
                same = type(ar) is type(r) and bool(ar == r)
            except Exception:
                same = False
            if not same:
                _DIFF.add(key)
                n += 1
        if n == len(alts):
            _DONE.add(site)
    return r


builtins.br_cmp__ = br_cmp__


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.depth = 0

    def _scope(self, node):
        self.depth += 1
        node = self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _scope

    def visit_Compare(self, node):
        node = self.generic_visit(node)
        if self.depth == 0 or len(node.ops) != 1:
            return node
        op = type(node.ops[0]).__name__
        if op not in OPS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        _SITES.add(site)
        call = ast.Call(func=ast.Name(id="br_cmp__", ctx=ast.Load()),
                        args=[ast.Constant(site), ast.Constant(op), node.left, node.comparators[0]],
                        keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.realpath(path)[len(ROOT):]
        tree = ast.parse(data, path)
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)

    def get_code(self, fullname):  # never use cached bytecode
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not os.path.realpath(spec.origin).startswith(SQLGLOT):
            return spec
        spec.loader = _Loader(name, spec.origin)
        return spec


def pytest_configure(config):
    sys.path.insert(0, os.environ.get("MAP_SELECTOR", "/home/claude/dev/br/selector"))
    import syncpool
    syncpool.install()
    os.makedirs(os.path.join(OUT, "t"), exist_ok=True)
    sys.meta_path.insert(0, _Finder())
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:  # pragma: no cover
            print("import fail", m.name, e, file=sys.stderr)
    with open(os.path.join(OUT, "sites.%d.json" % os.getpid()), "w") as f:
        json.dump(sorted(_SITES), f)


def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        status = 0
        try:
            _REC[0] = True
            reports = runtestprotocol(item, nextitem=None, log=False)
            _REC[0] = False
            out = {
                "nodeid": item.nodeid,
                "failed": any(r.failed for r in reports),
                "diff": sorted([s, a] for s, a in _DIFF),
                "unknown": sorted(_UNKNOWN),
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
