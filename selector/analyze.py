"""Map a patch to candidate tests using the precomputed dependency maps.

Every decision errs toward more candidates: a missed failure costs 300 s,
an extra candidate costs a local run or a slot on a worker.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from dataclasses import dataclass, field

from patchparse import ApplyError, FileDiff, apply

# Paths no test reads or imports at the pinned commit (checked against the
# file-open map at build time). Anything else unknown is "broad".
INERT_PREFIXES = ("benchmarks/", "posts/", "pdoc/", "docs/", ".github/", "sqlglot-integration-tests/")
INERT_SUFFIXES = (".md", ".png", ".svg", ".rst", ".txt")
# Repository metadata nobody imports or reads during tests (no pytest settings live in
# pyproject.toml or setup.cfg at the pinned commit). Readers in the maps still win.
ROOT_INERT = {".gitignore", ".gitmodules", ".gitattributes", ".gitpod.yml", ".pre-commit-config.yaml",
              "LICENSE", "MANIFEST.in", "Makefile", "pyproject.toml", "setup.cfg", "setup.py",
              "sqlglot/py.typed"}


def _root_inert(path: str) -> bool:
    return path in ROOT_INERT
ATTR_NARROW = os.environ.get("BR_ATTR_NARROW", "1") == "1"
KEY_NARROW = os.environ.get("BR_KEY_NARROW", "1") == "1"
OWNER_KEYS = os.environ.get("BR_OWNER_KEYS", "1") == "1"
BROKEN_NARROW = os.environ.get("BR_BROKEN_NARROW", "1") == "1"
TPC_FIXTURE = os.environ.get("BR_TPC_FIXTURE", "1") == "1"
TPC_FIXTURES = {"tests/fixtures/optimizer/tpc-ds/tpc-ds.sql": "tpcds", "tests/fixtures/optimizer/tpc-h/tpc-h.sql": "tpch"}
TPC_TEST = {"tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch"}
MODULE_NARROW = os.environ.get("BR_MODULE_NARROW", "1") == "1"
IMPORT_LOCAL = os.environ.get("BR_IMPORT_LOCAL", "1") == "1"
CLASS_HEADER_NAME = os.environ.get("BR_CLASS_HEADER_NAME", "1") == "1"
KEEP_TREES = os.environ.get("BR_KEEP_TREES", "0") == "1"
PURE_INSERT = os.environ.get("BR_PURE_INSERT", "1") == "1"
STRUCT = os.environ.get("BR_STRUCT", "1") == "1"
SKIP_BLANK_PINS = os.environ.get("BR_SKIP_BLANK_PINS", "1") == "1"
CMP_PROFILE = os.environ.get("BR_CMP_PROFILE", "1") == "1"
RENAME_NOOP = os.environ.get("BR_RENAME_NOOP", "1") == "1"


@dataclass
class Result:
    tests: int = 0                                 # bitset over map test indexes
    strong: int = 0                                # direct evidence: executed the changed statement / read the file
    medium: int = 0                                # read the changed class attribute by name (line level)
    strong_lines: list = field(default_factory=list)  # (path, lines) behind strong evidence
    file_readers: int = 0                          # tests that read a changed data file (not line-level)
    medium_names: set = field(default_factory=set)    # attribute names behind medium evidence
    import_fp: bool = False                        # touches code run at import: check state fingerprint
    import_heur: int = 0                           # import-time candidates by heuristics (used if no fingerprint)
    fp_func: bool = False                          # function code that also runs at import changed
    equiv: bool = False                            # every code change is an is/== swap against None/enum
    key_attrs: set = field(default_factory=set)    # names edited at import level (expected to differ in fingerprint)
    plain_key_attrs: set = field(default_factory=set)  # of those, any table of that name may differ
    key_owners: dict = field(default_factory=dict)     # key-narrowed attr -> owners ("module:Qual") edited
    state_guard: bool = False                      # class/module edit: fingerprint ripple check required
    state_guard_pending: bool = False
    broad: str | None = None                       # reason, if we must run everything
    tpc_queries: dict = field(default_factory=dict)  # TPC test id -> query indices a fixture edit changed
    broken: bool = False                           # a patched module cannot be imported: list res.tests as is
    patched: dict = field(default_factory=dict)   # path -> new text (None = deleted)
    notes: list = field(default_factory=list)
    trees: dict = field(default_factory=dict)      # path -> (old AST, new AST), for hot-swap planning
    unverified: int = 0                            # candidates a full-registry child cannot clear: list them


class Analyzer:
    def __init__(self, maps: dict, repo: str):
        self.m = maps
        self.repo = repo
        self.sets = maps["sets"]
        self._func_index: dict[str, list] = {}
        self.all_bits = (1 << len(maps["tests"])) - 1

    # ---- map lookups ---------------------------------------------------
    def cov_lines(self, path: str, lines) -> set:
        lm = self.m["cov"].get(path)
        out = 0
        if not lm:
            return out
        for ln in lines:
            sid = lm.get(ln)
            if sid is not None:
                out |= self.sets[sid]
        return out

    def collect_lines(self, path: str, lines) -> int:
        lm = self.m.get("collect", {}).get(path)
        out = 0
        if lm:
            for ln in lines:
                sid = lm.get(ln)
                if sid is not None:
                    out |= self.sets[sid]
        return out

    def collect_file(self, path: str) -> int:
        out = 0
        for sid in set(self.m.get("collect", {}).get(path, {}).values()):
            out |= self.sets[sid]
        return out

    def cov_file(self, path: str) -> set:
        out = 0
        for sid in set(self.m["cov"].get(path, {}).values()):
            out |= self.sets[sid]
        return out

    def recv(self, key: str) -> set:
        sid = self.m["recv"].get(key)
        return self.sets[sid] if sid is not None else 0

    def recv_module(self, module: str) -> set:
        out = 0
        pre = module + ":"
        for k, sid in self.m["recv"].items():
            if k.startswith(pre):
                out |= self.sets[sid]
        return out

    def opens(self, path: str) -> set:
        sid = self.m["opens"].get(path)
        return self.sets[sid] if sid is not None else 0

    # ---- top level -------------------------------------------------------
    def analyze(self, fds: list[FileDiff]) -> Result:
        res = Result()
        has_dirs = any(k.startswith("<dir>") for k in self.m["opens"])
        for fd in fds:
            why = self._pytest_config(fd)
            if why:
                res.broad = why
                return res
            if fd.is_new and not (fd.binary or fd.rename or fd.mode_change) and has_dirs:
                if self._new_file(fd, res):
                    continue
                return res
            if STRUCT and self._structural(fd, res):
                if res.broad:
                    return res
                continue
            if fd.binary or fd.rename or fd.is_new or fd.is_deleted or fd.mode_change:
                res.broad = f"structural change {fd.path}"
                return res
            path = fd.path
            src_path = os.path.join(self.repo, path)
            try:
                with open(src_path, encoding="utf-8", newline="") as f:
                    old = f.read()
                new = apply(old, fd)
            except (OSError, ApplyError, UnicodeDecodeError) as e:
                res.broad = f"apply failed {path}: {e}"
                return res
            res.patched[path] = new
            if path.endswith(".py"):
                self._python(path, old, new, fd, res)
            elif TPC_FIXTURE and path in TPC_FIXTURES and self._tpc_fixture(path, old, new, res):
                pass
            else:
                self._data_file(path, res)
            if res.broad:
                return res
            if not path.endswith(".py") and path not in ():
                res.nonequiv_files = getattr(res, "nonequiv_files", 0) + 1
        res.equiv = getattr(res, "equiv_files", 0) > 0 and getattr(res, "nonequiv_files", 0) == 0
        self._cache_decorators(res)
        return res

    _CACHE_DEC = re.compile(r"@\s*[\w.]*\b(?:lru_cache|cache|cached_property)\b")

    _PYTEST_FILES = {"pytest.ini", ".pytest.ini", "tox.ini", "conftest.py", "sitecustomize.py", "usercustomize.py"}

    def _pytest_config(self, fd: FileDiff) -> str | None:
        """Files pytest (or the interpreter) reads before any test: they can change
        every test's outcome."""
        names = {os.path.basename(p) for p in (fd.path, getattr(fd, "old_path", None) or fd.path)}
        if names & self._PYTEST_FILES or any(n.endswith(".pth") for n in names):
            return f"pytest/interpreter config {fd.path}"
        if fd.path not in ("pyproject.toml", "setup.cfg") or fd.binary:
            return None
        try:
            old = open(os.path.join(self.repo, fd.path), encoding="utf-8").read()
            new = apply(old, fd)
        except Exception:  # noqa: BLE001
            return f"pytest config? {fd.path}"

        def touched(text, lines):
            ls = text.split("\n")
            for ln in lines:
                if "pytest" in (ls[ln - 1] if 0 < ln <= len(ls) else "").lower():
                    return True
                for k in range(min(ln, len(ls)) - 1, -1, -1):
                    h = ls[k].strip()
                    if h.startswith("["):
                        if "pytest" in h.lower():
                            return True
                        break
            return False

        if touched(new, fd.added_new_lines()) or touched(old, fd.removed_old_lines()):
            return f"pytest settings in {fd.path}"
        return None

    @staticmethod
    def _inert_test_module(src: str) -> bool:
        """Only imports, definitions and plain constants at module and class level."""
        try:
            tree = ast.parse(src)
        except (SyntaxError, ValueError):
            return True  # fails to collect on its own; its tests are not ours to list

        def safe_call(n):
            try:
                name = ast.unparse(n.func)
            except Exception:  # noqa: BLE001
                return False
            return name in ("re.compile", "Path", "pathlib.Path", "unittest.skip", "unittest.skipIf",
                            "unittest.skipUnless") or name.startswith("os.path.")

        def const(v):
            calls = [n for n in ast.walk(v) if isinstance(n, ast.Call)]
            if not all(safe_call(c) for c in calls):
                return False
            return all(isinstance(n, (ast.Call, ast.keyword, ast.Constant, ast.Tuple, ast.List, ast.Dict, ast.Set,
                                      ast.Load, ast.Name, ast.Attribute, ast.UnaryOp, ast.unaryop, ast.BinOp,
                                      ast.operator, ast.BoolOp, ast.boolop, ast.Compare, ast.cmpop, ast.JoinedStr,
                                      ast.FormattedValue, ast.Subscript, ast.Slice, ast.Starred))
                       for n in ast.walk(v))

        def ok(stmts, top):
            for st in stmts:
                if isinstance(st, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass)):
                    continue
                if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant):
                    continue
                if isinstance(st, ast.ClassDef):
                    if st.decorator_list and not all(const(d) for d in st.decorator_list):
                        return False
                    if not ok(st.body, False):
                        return False
                    continue
                tg = st.targets if isinstance(st, ast.Assign) else [getattr(st, "target", None)]
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value is not None and const(st.value) \
                        and all(isinstance(t, ast.Name) for t in tg):
                    continue
                if top and isinstance(st, ast.If) and isinstance(st.test, ast.Compare) and \
                        isinstance(st.test.left, ast.Name) and st.test.left.id == "__name__":
                    continue
                return False
            return True

        return ok(tree.body, True)

    def _unimportable(self, path: str) -> int | None:
        """Tests that load a module that will not compile; None when every test may."""
        if path.startswith("sqlglot/"):
            mod = path[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            sid = self.m.get("broken_mod", {}).get(mod)
            return None if sid is None else self.sets[sid] | self.opens(path)
        if path.startswith("tests/") and os.path.basename(path).startswith("test_"):
            ft = self.m["file_tests"].get(path)
            if ft is None:
                return None
            mod = path[:-3].replace("/", ".")
            b = ft | self.opens(path)
            for k, bits in self.m.get("test_importers", {}).items():
                if k == mod or k.startswith(mod + "."):
                    b |= bits
            return b
        return None

    def _cache_decorators(self, res: Result) -> None:
        """A memoizing decorator added or removed: results then depend on what ran
        earlier in the process, so a local run of a subset can pass where the full
        suite fails. List every candidate without trusting local verdicts."""
        for path, new in res.patched.items():
            if not path.endswith(".py") or new is None:
                continue
            try:
                with open(os.path.join(self.repo, path), encoding="utf-8") as f:
                    old = f.read()
            except OSError:
                continue
            if self._CACHE_DEC.findall(old) != self._CACHE_DEC.findall(new):
                res.unverified = self.all_bits
                res.notes.append(f"{path}: memoizing decorator changed; local verdicts untrusted")
                return

    def _new_file(self, fd: FileDiff, res: Result) -> bool:
        """A file that did not exist: only code that lists its directory, or that a
        patched module makes import it, can see it. False -> res.broad set."""
        path = fd.path
        try:
            new = apply("", fd) if fd.hunks else ""
        except ApplyError as e:
            res.broad = f"apply failed {path}: {e}"
            return False
        res.patched[path] = new
        d = os.path.dirname(path)
        listers = 0
        while True:
            sid = self.m["opens"].get("<dir>" + d)
            if sid is not None:
                listers |= self.sets[sid]
            if not d:
                break
            d = os.path.dirname(d)
        res.tests |= listers
        res.strong |= listers
        if path.startswith("tests/") and path.endswith(".py") and not self._inert_test_module(new):
            # pytest imports every test module before any test runs: module-level code
            # beyond definitions can change what every later test sees.
            res.broad = f"new test module with import-time effects {path}"
            return False
        if path.startswith("tests/") or path.startswith(INERT_PREFIXES) or path.endswith(INERT_SUFFIXES):
            res.notes.append(f"{path}: new file, {bin(listers).count('1')} dir listers")
            return True
        if path.startswith("sqlglot/"):
            # Imported only by name; tests that pass names in (dialect names...) or walk
            # the package see it. Import-time names elsewhere are caught by the
            # importing file's own diff.
            res.tests |= self._proc(path)
            res.notes.append(f"{path}: new module, {bin(listers).count('1')} dir listers")
            return True
        res.broad = f"new file outside package {path}"
        return False

    def _structural(self, fd: FileDiff, res: Result) -> bool:
        """Mode changes, binary files, deletions and renames that need not run everything.
        True when handled here (res.broad may still be set)."""
        def inert(p):
            return (p.startswith(INERT_PREFIXES) or p.endswith(INERT_SUFFIXES)) and not self.opens(p) \
                and p not in self.m["parent_opens"]

        def listers(p):
            out, d = 0, os.path.dirname(p)
            while True:
                sid = self.m["opens"].get("<dir>" + d) if d else self.m["opens"].get("<dir>")
                if sid is not None:
                    out |= self.sets[sid]
                if not d:
                    break
                d = os.path.dirname(d)
            return out

        def gone(p):
            """p no longer exists (deleted, or renamed away)."""
            if inert(p):
                res.notes.append(f"{p}: inert path removed")
                return
            t = self.opens(p) | listers(p)
            if p.startswith("tests/") and p.endswith(".py"):
                mod = p[:-3].replace("/", ".")
                t |= self.m["file_tests"].get(p, 0) | self.m["test_importers"].get(mod, 0)
                t |= self.collect_file(p)
                res.notes.append(f"{p}: test module removed")
            elif p.startswith("tests/") and p not in self.m["parent_opens"]:
                res.notes.append(f"{p}: data file removed")
            elif p.startswith("sqlglot/") and p.endswith(".py") and BROKEN_NARROW and \
                    self._unimportable(p) is not None and not p.endswith("__init__.py"):
                # A module that no longer exists: like one that will not compile.
                b = self._unimportable(p) | listers(p)
                res.tests |= b
                res.strong |= b
                res.unverified |= b
                res.broken = True
                res.notes.append(f"{p}: module removed, {bin(b).count('1')} tests load it")
                return
            else:
                res.broad = f"removed {p}"
                return
            res.tests |= t
            res.strong |= t
            res.file_readers |= t

        if fd.mode_change and not (fd.binary or fd.rename or fd.is_new or fd.is_deleted) and not fd.hunks:
            res.notes.append(f"{fd.path}: mode change only")
            return True
        if fd.binary and not fd.rename:
            p = fd.path
            if inert(p):
                res.notes.append(f"{p}: inert binary")
                return True
            if p.startswith("tests/") and p not in self.m["parent_opens"] and not fd.is_deleted:
                t = self.opens(p) | listers(p)
                res.tests |= t
                res.strong |= t
                res.file_readers |= t
                res.notes.append(f"{p}: binary data, {bin(t).count('1')} readers")
                return True
            return False
        if fd.is_deleted and not fd.binary:
            gone(fd.old_path)
            return True
        if fd.rename and not fd.binary and not fd.hunks and fd.old_path and fd.new_path:
            gone(fd.old_path)
            if res.broad:
                return True
            np_ = fd.new_path
            if not (np_.startswith("tests/") or inert(np_)):
                res.broad = f"renamed into {np_}"
                return True
            t = listers(np_)
            res.tests |= t
            res.strong |= t
            res.notes.append(f"{np_}: renamed in")
            return True
        return False

    # ---- non-python files ------------------------------------------------
    def _tpc_fixture(self, path: str, old: str, new: str, res: Result) -> bool:
        """TestExecutor reads both TPC fixture files in setUpClass, so every test of
        the class "reads" them, but only the TPC tests use the queries. Split the file
        the way tests/helpers.py does and charge per query. False: fall back."""
        if "tests/helpers.py" in res.patched:
            return False
        try:
            # The loader's own two helpers, without importing tests.helpers here (the
            # warm parent must not hold a test module a child may need patched).
            with open(os.path.join(self.repo, "tests/helpers.py"), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            ns: dict = {}
            fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_extract_meta", "_filter_comments")]
            exec(compile(ast.Module(body=fns, type_ignores=[]), "tests/helpers.py", "exec"), ns)
            _extract_meta, _filter_comments = ns["_extract_meta"], ns["_filter_comments"]
        except Exception:  # noqa: BLE001
            return False

        def pairs(text):
            st = _filter_comments(text).split(";")
            out = []
            for i in range(0, len(st), 2):
                if i + 1 < len(st):
                    sql, meta = _extract_meta(st[i].strip())
                    out.append((meta, sql, st[i + 1].strip()))
            return out

        try:
            po, pn = pairs(old), pairs(new)
        except Exception:  # noqa: BLE001
            return False  # setUpClass itself may now fail: every reader
        kind = TPC_FIXTURES[path]
        readers = self.opens(path)
        ids = self.m["tests"]
        idx = {t: i for i, t in enumerate(ids)}
        n = max(len(po), len(pn))
        sqlchg, anychg = set(), set()
        for i in range(n):
            a = po[i] if i < len(po) else None
            b = pn[i] if i < len(pn) else None
            if a != b:
                anychg.add(i)
            if a is None or b is None or a[:2] != b[:2]:
                sqlchg.add(i)
        if not anychg:
            res.notes.append(f"{path}: no query changed")
            return True
        executed = {i for i in sqlchg if (i < len(po) and po[i][0].get("execute")) or
                    (i < len(pn) and pn[i][0].get("execute"))}
        keep = 0
        for t in ids:
            b = 1 << idx[t]
            if not readers & b:
                continue
            if not t.startswith("tests/test_executor.py::TestExecutor::"):
                keep |= b  # optimizer fixture tests etc.: per-pair work, keep
                continue
            name = t.rsplit("::", 1)[1]
            if kind == "tpcds" and name == "test_execute_tpcds" and executed:
                keep |= b
            elif kind == "tpch" and name == "test_execute_tpch" and executed:
                keep |= b
            elif kind == "tpch" and name == "test_optimized_tpch":
                keep |= b
        res.tests |= keep
        res.strong |= keep
        res.file_readers |= keep & ~self._tpc_exec_bits(kind)
        if executed:
            res.tpc_queries[TPC_TEST[kind]] = executed
        res.notes.append(f"{path}: queries {sorted(anychg)[:5]} changed ({len(executed)} executed): "
                         f"{bin(keep).count('1')} tests")
        return True

    def _tpc_exec_bits(self, kind: str) -> int:
        try:
            return 1 << self.m["tests"].index(TPC_TEST[kind])
        except ValueError:
            return 0

    def _data_file(self, path: str, res: Result) -> None:
        if path in self.m["parent_opens"]:
            res.broad = f"read at import/collection: {path}"
            return
        t = self.opens(path)
        if t:
            res.tests |= t
            res.strong |= t
            res.file_readers |= t
            res.notes.append(f"{path}: {bin(t).count('1')} readers")
            return
        if path.startswith(INERT_PREFIXES) or path.endswith(INERT_SUFFIXES) or _root_inert(path):
            res.notes.append(f"{path}: inert")
            return
        if path.startswith("tests/fixtures/"):
            # A fixture nobody opened at the pinned commit: nothing reads it.
            res.notes.append(f"{path}: unread fixture")
            return
        res.broad = f"unknown data file {path}"

    # ---- python files ----------------------------------------------------
    def _python(self, path: str, old: str, new: str, fd: FileDiff, res: Result) -> None:
        if not (path.startswith("sqlglot/") or path.startswith("tests/")):
            t = self.opens(path)
            if (path.startswith(INERT_PREFIXES) or _root_inert(path)) and not t and path not in self.m["parent_opens"]:
                res.notes.append(f"{path}: inert python")
                return
            res.broad = f"python outside package {path}"
            return
        try:
            cache = self.__dict__.setdefault("_old_cache", {})
            if path not in cache or cache[path][0] != old:
                ot = ast.parse(old)
                cache[path] = (old, ot, None)
            old_tree, old_dump = cache[path][1], cache[path][2]
            old_tree = ast.parse(old) if _MUTATES else old_tree
            new_tree = ast.parse(new)
            # Full compile: catches errors ast.parse accepts (misplaced __future__
            # imports, return outside function, bad nonlocal...). Such a module
            # cannot be imported at all.
            compile(new_tree, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            b = self._unimportable(path) if BROKEN_NARROW else None
            if b is None:
                res.broad = f"syntax error {path}"
                return
            # The module cannot be imported: every test that loads it fails at once.
            res.tests |= b
            res.strong |= b
            res.unverified |= b
            res.broken = True
            res.notes.append(f"{path}: syntax error, {bin(b).count('1')} tests load it")
            return
        # Text readers of the source (inspect.getsource, subprocess tools).
        res.tests |= self.opens(path)
        if path.startswith("tests/") and not path.startswith("tests/fixtures/") and not _ast_same(old_tree, new_tree):
            seen = {ast.dump(st) for st in old_tree.body}
            fresh = [st for st in new_tree.body if ast.dump(st) not in seen]
            if fresh and not self._inert_test_module(ast.unparse(ast.Module(body=fresh, type_ignores=[]))):
                # pytest imports every test module before running any test: new
                # import-time code here (monkeypatching, global settings) reaches tests
                # in other files, which a local run of those files alone would not see.
                res.broad = f"test module import-time code changed {path}"
                return
        if path.startswith("tests/") and os.path.basename(path) == "__init__.py" and not _ast_same(old_tree, new_tree):
            # A test package's __init__ runs before any test module under it loads.
            d = os.path.dirname(path) + "/"
            pkg = 0
            for f, bits in self.m["file_tests"].items():
                if f.startswith(d):
                    pkg |= bits
            res.tests |= pkg
            res.strong |= pkg
            res.notes.append(f"{path}: package init, {bin(pkg).count('1')} tests under it")
        res.trees[path] = (old_tree, new_tree) if KEEP_TREES else None
        if _ast_same(old_tree, new_tree):
            res.notes.append(f"{path}: AST unchanged")
            return
        if RENAME_NOOP and not path.startswith("sqlglot/typing/") and _locals_renamed_only(old_tree, new_tree):
            res.notes.append(f"{path}: only local variables renamed")
            return
        if CMP_PROFILE and path.startswith("sqlglot/") and self.m.get("cmp_sites"):
            if self._op_swap_tests(path, old_tree, new_tree, res):
                return
        if _maybe_swap(old, new) and swap_equivalent(old_tree, new_tree, path):
            res.equiv_files = getattr(res, "equiv_files", 0) + 1
            res.notes.append(f"{path}: is/== swap")
        else:
            res.nonequiv_files = getattr(res, "nonequiv_files", 0) + 1

        if PURE_INSERT and path.startswith("sqlglot/"):
            added = _pure_func_insertion(old_tree, new_tree)
            if added:
                # Only new, unreferenced module-level functions: nothing can call them.
                # The fingerprint still guards import-time effects (registries that
                # scan module globals); lines below shift, so first-line numbers move.
                res.tests |= self._proc(path)
                res.import_fp = True
                res.state_guard = True
                res.key_attrs |= set(added) | {"__firstlineno__"}
                res.plain_key_attrs |= set(added) | {"__firstlineno__"}
                res.notes.append(f"{path}: new functions {sorted(added)}")
                return

        old_idx = _ScopeIndex(old_tree)
        new_idx = _ScopeIndex(new_tree)
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]

        targets: list[tuple[str, int]] = []  # ("old", line) items to resolve
        for ln in fd.removed_old_lines():
            targets.append(("old", ln))
        for after, new_lines in fd.insertion_points():
            # Inserted code: resolve its scope in the new tree, and pin it to
            # the neighbouring old statements.
            kinds = {new_idx.kind(n) for n in new_lines if new_idx.stmt_at(n) is not None}
            if not kinds:
                continue  # comment/blank lines only
            for nb in (after, after + 1):
                # Pin to neighbouring statements; blank or comment lines at module
                # level carry no behaviour (the inserted code is resolved below).
                if nb >= 1 and not (SKIP_BLANK_PINS and old_idx.stmt_at(nb) is None):
                    targets.append(("old", nb))
            if any(k[0] != "func" for k in kinds):
                for n in new_lines:
                    k = new_idx.kind(n)
                    if k[0] != "func":
                        self._import_time(path, module, k, new_idx, n, res, tree="new")

        reg = self.m.get("regempty", {}).get(path)
        if reg:
            # Lines the dialect registry runs when it starts empty (as after
            # tests/test_dialect_entry_points.py in one pytest process): the tests
            # that reach them that way may fail only in that order.
            for _, ln in targets:
                sid = reg.get(ln)
                if sid is not None:
                    extra = self.sets[sid] & ~self.cov_lines(path, [ln])
                    res.tests |= extra
                    res.strong |= extra
                    res.unverified |= extra
        for _, ln in targets:
            kind = old_idx.kind(ln)
            if kind[0] == "func":
                stmt = old_idx.stmt_at(ln)
                # Only the statement's own lines (compound statements: header only).
                lines = old_idx.own_lines(stmt) if stmt is not None else [ln]
                imp = self.m["import_lines"].get(path, ())
                direct = self.cov_lines(path, lines) | self.collect_lines(path, lines)
                res.tests |= direct
                res.strong |= direct
                res.strong_lines.append((path, list(lines)))
                if any(x in imp for x in lines):
                    # Function code that also runs during import (metaclasses, table
                    # builders, parsing at import): if the patched import builds
                    # different state, every test is a candidate (decided later
                    # by fingerprinting the patched import).
                    res.import_fp = True
                    res.fp_func = True
                    res.tests |= self._proc(path)
                    res.notes.append(f"{path}:{ln} import-time function code")
            else:
                self._import_time(path, module, kind, old_idx, ln, res, tree="old")

    def _import_time(self, path, module, kind, idx, ln, res: Result, tree: str) -> None:
        """A change to code run at import: def headers, class bodies, module level."""
        k = kind[0]
        if k == "funchead":
            fn = kind[1]
            if tree == "old":
                rng = range(fn.lineno, (fn.end_lineno or fn.lineno) + 1)
                res.tests |= self.cov_lines(path, rng) | self.collect_lines(path, rng)
                imp = self.m["import_lines"].get(path, ())
                if any(x in imp for x in range(fn.body[0].lineno, (fn.end_lineno or fn.lineno) + 1)):
                    res.import_fp = True
                    res.fp_func = True
            else:
                res.tests |= self._func_by_name(path, kind[2])
            return
        if path.startswith("sqlglot/"):
            # Tests that spawn a fresh interpreter (import sqlglot) observe import-time behaviour.
            res.tests |= self._proc(path)
        if k in ("class", "module") and tree == "old":
            # Code inside the statement that runs later (lambda / comprehension
            # bodies in tables): tests that executed it at run time.
            node = idx.stmt_at(ln)
            span = [ln]
            if node is not None:
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                        a0, b0 = sub.lineno, getattr(sub, "end_lineno", sub.lineno) or sub.lineno
                        if a0 <= ln <= b0:
                            span = list(range(a0, b0 + 1))
            direct = self.cov_lines(path, span)
            res.tests |= direct
            res.strong |= direct
            res.strong_lines.append((path, list(span)))
        if path.startswith("sqlglot/") and k in ("class", "module"):
            # Import-time state change. Heuristic candidates are the base; the
            # fingerprint of the patched import guards against ripples (state that
            # changes beyond the edited names: other modules' tables, import errors).
            before = res.tests
            res.tests = 0
            res.state_guard_pending = True
            self._import_time_heur(path, module, kind, idx, ln, res, tree)
            res.state_guard_pending = False
            res.import_heur |= res.tests
            res.tests = before
            res.import_fp = True
            res.state_guard = True
            res.key_attrs |= idx.names_assigned(ln)
            res.plain_key_attrs |= idx.names_assigned(ln)
            return
        self._import_time_heur(path, module, kind, idx, ln, res, tree)

    def _import_time_heur(self, path, module, kind, idx, ln, res: Result, tree: str) -> None:
        k = kind[0]
        if k == "funchead":
            self._import_time(path, module, kind, idx, ln, res, tree)
            return
        if k == "class":
            qual = kind[2]
            cls_node = kind[1]
            keyed = self._key_narrow(idx, ln, f"{module}:{qual}") if KEY_NARROW and tree in ("old", "new") and path.startswith("sqlglot/") else None
            if keyed is not None:
                res.tests |= keyed
                res.medium |= keyed
                # Guard: the patched import may change other state through this table
                # (copies under other names, derived tables, values parsed at import).
                res.key_attrs.add(self._last_key_attr)
                res.key_owners.setdefault(self._last_key_attr, set()).add(self._last_key_owner)
                res.import_fp = True
                res.fp_func = True
                res.notes.append(f"{path}:{ln} key-narrowed {qual}.{self._last_key_attr}")
                return
            narrowed = self._attr_narrow(module, qual, idx.names_assigned(ln)) if ATTR_NARROW else None
            if narrowed is not None:
                res.tests |= narrowed
                res.medium |= narrowed
                res.medium_names |= self._alias_closure(idx.names_assigned(ln))
                res.notes.append(f"{path}:{ln} attr-narrowed {qual}")
            else:
                res.tests |= self.recv(f"{module}:{qual}")
                res.tests |= self._class_methods_cov(path, qual)
            if tree == "old" and cls_node is not None:
                res.tests |= self.collect_lines(path, range(cls_node.lineno, (cls_node.end_lineno or cls_node.lineno) + 1))
            res.tests |= self._test_class(module, qual)
            names = idx.names_assigned(ln)
            # Class attributes reach code through instances/subclasses of the class
            # (recv) or explicit Owner.ATTR references elsewhere.
            refs = self.m.get("attr_refs", {})
            for n in names:
                for key in (f"{qual}.{n}", f"{qual.split('.')[-1]}.{n}"):
                    sid = refs.get(key)
                    if sid is not None:
                        res.tests |= self.sets[sid]
            if path.startswith("tests/"):
                res.tests |= self._name_users(names)
            res.notes.append(f"{path}:{ln} class body {qual}")
            return
        if (k == "module" and path.startswith("sqlglot/") and res.state_guard_pending
                and (MODULE_NARROW or isinstance(idx.stmt_at(ln), ast.ClassDef))):
            # Module-level statement in sqlglot: runtime readers of the edited names
            # (line level), test files importing those names, the fresh-interpreter
            # test; the fingerprint ripple guard covers import-time effects.
            node_ = idx.stmt_at(ln)
            if isinstance(node_, ast.ClassDef) and CLASS_HEADER_NAME:
                # Header line of a class (bases, keywords, decorators): the class
                # object itself changes; its body tables are fingerprinted.
                names = {node_.name}
                res.tests |= self._class_ref_users(node_.name)
            else:
                names = self._alias_closure(idx.names_assigned(ln))
            nl = self.m.get("name_line_users", {})
            is_import = isinstance(idx.stmt_at(ln), (ast.Import, ast.ImportFrom))
            if is_import and IMPORT_LOCAL:
                # An import binds names in this module only: its run-time readers are
                # the lines of this file that read them (other modules' bindings and
                # import errors show in the fingerprint).
                res.tests |= self.cov_lines(path, self._read_lines(path, names))
            for n in names:
                sid = None if (is_import and IMPORT_LOCAL) else nl.get(n)
                if sid is not None:
                    res.tests |= self.sets[sid]
                sid = self.m.get("test_name_imports", {}).get(f"{module}:{n}")
                if sid is not None:
                    res.tests |= self.sets[sid]
                # A class header (bases, decorators) changes every instance's behaviour;
                # the fingerprint hashes class dicts, not MROs, so add the class's users.
                res.tests |= self.recv(f"{module}:{n}")
            res.tests |= self._proc(path)
            res.tests |= self.collect_file(path) if path.startswith("tests/") else 0
            # Names copied into widely shared class tables reach far: keep the
            # file-level set instead when it is cheaper (both are validated covers).
            narrow = res.tests
            res.tests = 0
            self._module_file_set(path, module, idx, ln, res)
            wide = res.tests
            node = idx.stmt_at(ln)
            plain = isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            if os.environ.get("BR_MN_DEBUG"):
                print("MNDBG", path, ln, type(node).__name__, bin(narrow).count("1"), round(self._weight(narrow)),
                      bin(wide).count("1"), round(self._weight(wide)), file=sys.stderr)
            if plain and self._weight(wide) < self._weight(narrow):
                res.tests = wide
                res.notes.append(f"{path}:{ln} module level (file set)")
            else:
                res.tests = narrow
                res.notes.append(f"{path}:{ln} module level (names {sorted(names)[:4]})")
            return
        if k == "module":
            self._module_file_set(path, module, idx, ln, res)
            res.notes.append(f"{path}:{ln} module level")
            return
        res.broad = f"unresolved scope {path}:{ln}"

    def _module_file_set(self, path, module, idx, ln, res: Result) -> None:
        res.tests |= self.cov_file(path)
        res.tests |= self.collect_file(path)
        res.tests |= self.recv_module(module)
        if path.startswith("tests/"):
            res.tests |= self.m["file_tests"].get(path, 0)
            res.tests |= self.m["test_importers"].get(module, 0)
        names = idx.names_assigned(ln)
        res.tests |= self._name_users(names)

    def _class_ref_users(self, name: str) -> int:
        """Receivers of sqlglot classes whose body names `name` at import (e.g. `Parser = X`)."""
        import glob
        bits = 0
        pat = re.compile(r"\b" + re.escape(name) + r"\b")
        for f in glob.glob(os.path.join(self.repo, "sqlglot", "**", "*.py"), recursive=True):
            try:
                src = open(f, encoding="utf-8").read()
            except OSError:
                continue
            if not pat.search(src):
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            rel = os.path.relpath(f, self.repo)
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]

            def walk(body, qual):
                nonlocal bits
                for node in body:
                    if isinstance(node, ast.ClassDef):
                        q = f"{qual}.{node.name}" if qual else node.name
                        refs = any(isinstance(n, ast.Name) and n.id == name
                                   for st in node.body if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))
                                   for n in ast.walk(st))
                        refs = refs or any(isinstance(n, ast.Name) and n.id == name
                                           for b in node.bases + node.keywords + node.decorator_list for n in ast.walk(b))
                        if refs:
                            bits |= self.recv(f"{mod}:{q}")
                        walk(node.body, q)
            walk(tree.body, "")
        return bits

    def _op_swap_tests(self, path, old_tree, new_tree, res) -> bool:
        """Only comparison operators changed (single-operator comparisons inside
        functions): charge each swap to the tests whose profiled operands give a
        different result under the new operator. False when the rule does not apply."""
        try:
            swaps = _op_swaps(old_tree, new_tree)
        except RecursionError:
            return False
        if not swaps:
            return False
        imp = set(self.m["import_lines"].get(path, ()))
        prof, unk = self.m.get("cmp", {}), self.m.get("cmp_unknown", {})
        untrusted = self.sets[self.m["cmp_untrusted"]] if "cmp_untrusted" in self.m else 0
        tests = 0
        for ln, col, old_op, new_op in swaps:
            site = f"{path}:{ln}:{col}" + (":bool" if old_op == "Bool" else "")
            if site not in self.m["cmp_sites"] or ln in imp:
                return False
            if old_op != "Bool" and new_op not in _CMP_FAMILY.get(old_op, ()):
                return False
            sid = prof.get(site, {}).get(new_op)
            if sid is not None:
                tests |= self.sets[sid]
            line = self.cov_lines(path, [ln])
            if site in unk:
                tests |= line  # operands the profile could not evaluate safely
            tests |= line & untrusted
            tests |= self.collect_lines(path, [ln])
            res.strong_lines.append((path, [ln]))  # per-query narrowing of heavy tests
            rsid = self.m.get("regempty", {}).get(path, {}).get(ln)
            if rsid is not None:
                extra = self.sets[rsid] & ~line
                tests |= extra
                res.unverified |= extra
        res.tests |= tests
        res.strong |= tests
        res.notes.append(f"{path}: {len(swaps)} operator swap(s), profile -> {bin(tests).count('1')} tests")
        return True

    def _proc(self, path: str) -> int:
        """Tests that start a fresh interpreter and run `import sqlglot` (test_lazy_load):
        only modules that bare import loads can change what they see."""
        if not CORE_ONLY_PROC or not path.startswith("sqlglot/"):
            return self.m["proc_tests"]
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        return self.m["proc_tests"] if mod in _CORE_MODULES else 0

    def _read_lines(self, path: str, names) -> list:
        """Lines of the pristine file that load any of names (bare names or attribute roots)."""
        cache = self.__dict__.setdefault("_read_cache", {})
        tree = cache.get(path)
        if tree is None:
            try:
                tree = ast.parse(open(os.path.join(self.repo, path), encoding="utf-8").read())
            except (OSError, SyntaxError, ValueError):
                tree = False
            cache[path] = tree
        if tree is False:
            return []
        names = set(names)
        out = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in names and isinstance(node.ctx, ast.Load):
                out.update(range(node.lineno, (getattr(node, "end_lineno", None) or node.lineno) + 1))
        # Include whole enclosing statements' first lines too (coverage records statement starts).
        stmts = []
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt):
                a0, b0 = node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno
                if any(a0 <= x <= b0 for x in out) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.If, ast.For, ast.While, ast.With, ast.Try)):
                    out.update(range(a0, b0 + 1))
        return sorted(out)

    def _weight(self, bits: int) -> float:
        """Total local run time (ms) of a test bitset."""
        w = getattr(self, "_w", None)
        if w is None:
            lm = self.m.get("local_ms", {})
            w = self._w = [float(lm.get(t, 100)) for t in self.m["tests"]]
        total, i = 0.0, 0
        while bits:
            if bits & 1:
                total += w[i]
            bits >>= 1
            i += 1
        return total

    def state_key_users(self, key: str) -> int | None:
        """Tests that can observe a changed import-time state entry; None = unknown."""
        if key.startswith("global:") or key.startswith("<"):
            return None
        module, _, qual = key.partition(":")
        parts = qual.split(".")
        path = module.replace(".", "/") + ".py"
        if path not in self.m["funcs"]:
            path = module.replace(".", "/") + "/__init__.py"
        bits = 0
        refs = self.m.get("attr_refs", {})
        # Longest class prefix that tests used (receivers) or that is a known class.
        for i in range(len(parts) - 1, 0, -1):
            cls = ".".join(parts[:i])
            ck = f"{module}:{cls}"
            if ck in self.m["recv"] or any(q.startswith(cls + ".") for q, _, _ in self.m["funcs"].get(path, [])):
                attr = parts[i]
                bits |= self.recv(ck)
                bits |= self._class_methods_cov(path, cls)
                for kk in (f"{cls}.{attr}", f"{parts[i - 1]}.{attr}"):
                    sid = refs.get(kk)
                    if sid is not None:
                        bits |= self.sets[sid]
                return bits
        name = parts[0]
        bits |= self._name_users({name})
        bits |= self.recv(f"{module}:{name}")  # a module-level class object itself
        return bits

    def _key_narrow(self, idx, ln, owner=None) -> int | None:
        """Tests that looked up the edited entry's key in its class table.

        Applies to a single dict entry of an UPPERCASE class attribute. None when the
        table is read by import-time function code, copied under another name, or the
        key is read with a constant at import (derived values), or the entry key is
        not a literal we can name.
        """
        ku = self.m.get("key_users")
        if not ku or len(self.m["tests"]) != self.m.get("key_tests", 0):
            return None
        node = idx.stmt_at(ln)
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Dict):
            return None
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name) or not targets[0].id.isupper():
            return None
        attr = targets[0].id
        d = node.value
        entry = None
        for k, v in zip(d.keys, d.values):
            a0 = (k or v).lineno
            b0 = max(getattr(k, "end_lineno", a0) or a0, getattr(v, "end_lineno", a0) or a0)
            if a0 <= ln <= b0:
                entry = k
                break
        if entry is None:  # the line is not inside one entry (braces, spread, comment between entries)
            return None
        if isinstance(entry, ast.Constant) and isinstance(entry.value, str):
            key = "str:" + entry.value
        elif isinstance(entry, ast.Attribute):
            base = entry.value
            base_name = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
            key = (f"enum:{base_name}.{entry.attr}" if entry.attr.isupper() else f"type:{entry.attr}")
        else:
            return None
        if f"{attr}|{key}" in self.m.get("import_const_keys", set()):
            return None
        out = 0
        self._last_key_attr = attr
        self._last_key_owner = None
        ou, tags = self.m.get("okey_users"), self.m.get("okey_tags")
        if OWNER_KEYS and ou is not None and owner and f"{owner}.{attr}" in tags:
            # Owner-level: lookups in tables an edit of this class's table can reach
            # (its own, subclasses' and copies'), not same-name tables elsewhere.
            tbl = ou.get(f"{owner}|{attr}", {})
            for kk in (key, "*"):
                sid = tbl.get(kk)
                if sid == -1:
                    sid = ku.get(f"{attr}|{kk}")
                if sid is not None:
                    out |= self.sets[sid]
            self._last_key_owner = owner
            return out
        for k in (f"{attr}|{key}", f"{attr}|*"):
            sid = ku.get(k)
            if sid is not None:
                out |= self.sets[sid]
        return out

    def opens_any(self, res: "Result", bit: int) -> bool:
        """Does the test read any patched file as text (source or data)?"""
        for path in res.patched:
            if self.opens(path) & bit:
                return True
        return False

    def _alias_closure(self, names: set) -> set:
        todo, seen = list(names), set()
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(self.m.get("aliases", {}).get(n, ()))
        return seen

    def partial_queries(self, tid: str, res: "Result", is_strong: bool, is_medium: bool) -> int | None:
        """Bitmask of queries of a heavy executor test that can observe the change.

        None: the test must run whole (change reaches code outside per-query work,
        or the evidence is not line-level).
        """
        if tid in res.tpc_queries and len(res.patched) == 1:
            q = 0
            for i in res.tpc_queries[tid]:
                q |= 1 << i
            return q
        tc = self.m.get("task_cov", {}).get(tid)
        if tc is None or not (is_strong or is_medium) or res.import_fp:
            return None
        try:
            bit = 1 << self.m["tests"].index(tid)
        except ValueError:
            return None
        if res.file_readers & bit or self.opens_any(res, bit):
            return None  # evidence is a file the test reads, not lines it runs
        q = 0

        def take(path, ln):
            # Line the test executes: must be per-query work, else run whole.
            if not (self.cov_lines(path, [ln]) & bit):
                return True  # the test never runs this line
            if ln in tc["rest"].get(path, ()) or (path, ln) not in tc["q"]:
                return False  # setUpClass / outside per-query work
            nonlocal q
            q |= tc["q"][(path, ln)]
            return True

        if is_strong:
            for path, lines in res.strong_lines:
                if self.collect_lines(path, lines) & bit:
                    return None  # reached through module state built at collection
                for ln in lines:
                    if not take(path, ln):
                        return None
        if is_medium:
            pos = self.m.get("task_name_pos", {})
            for n in res.medium_names:
                for (path, ln) in pos.get(n, ()):
                    if not take(path, ln):
                        return None
        return q

    def _attr_narrow(self, module: str, qual: str, names: set) -> int | None:
        """Tests that use class `qual` AND run code that reads one of `names`.

        None (fall back to every user of the class) when a name is read during
        import, copied under another name, or never read at run time by name.
        """
        if not names:
            return None
        imp = self.m.get("import_ref_names")
        if imp is None:
            return None
        todo, seen = list(names), set()
        while todo:
            n = todo.pop()
            if n in seen:
                continue
            seen.add(n)
            if n in imp:
                return None
            todo.extend(self.m["aliases"].get(n, ()))
        nl = self.m.get("name_line_users")
        users = 0
        for n in seen:
            sid = (nl if nl is not None else self.m["name_users"]).get(n)
            if sid is not None:
                users |= self.sets[sid]
        if not users:
            return None
        return self.recv(f"{module}:{qual}") & users

    def _func_by_name(self, path: str, qual: str) -> set:
        # Coverage of an old function with the same qualified name, if any.
        for (q, a, b) in self.m["funcs"].get(path, []):
            if q == qual:
                return self.cov_lines(path, range(a, b + 1))
        return 0

    def _class_methods_cov(self, path: str, qual: str) -> set:
        out = 0
        pre = qual + "."
        for (q, a, b) in self.m["funcs"].get(path, []):
            if q.startswith(pre):
                out |= self.cov_lines(path, range(a, b + 1))
        return out

    def _test_class(self, module: str, qual: str) -> set:
        if not module.startswith("tests"):
            return 0
        sid = self.m["tcls"].get(f"{module}:{qual}")
        return self.sets[sid] if sid is not None else 0

    def _name_users(self, names) -> set:
        out = 0
        for n in names:
            sid = self.m["name_users"].get(n)
            if sid is not None:
                out |= self.sets[sid]
        return out


def _is_enumish(n) -> bool:
    return isinstance(n, ast.Attribute) and n.attr.isupper() and len(n.attr) > 1


def _is_none(n) -> bool:
    return isinstance(n, ast.Constant) and n.value is None


class _SwapNorm(ast.NodeTransformer):
    """Map `is`/`is not` to `==`/`!=` where one side is None or an enum member."""

    def visit_Compare(self, node):
        self.generic_visit(node)
        operands = [node.left] + node.comparators
        ops = []
        for i, op in enumerate(node.ops):
            a, b = operands[i], operands[i + 1]
            if any(_is_none(x) or _is_enumish(x) for x in (a, b)):
                if isinstance(op, ast.Is):
                    op = ast.Eq()
                elif isinstance(op, ast.IsNot):
                    op = ast.NotEq()
            ops.append(op)
        node.ops = ops
        return node


def _top_names(tree: ast.Module) -> set:
    names = set()
    for node in tree.body:
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.alias):
                names.add((n.asname or n.name).split(".")[0])
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                names.add(n.value)
    return names


def _pure_func_insertion(old: ast.Module, new: ast.Module) -> list | None:
    """Names of module-level functions the new tree only adds (no decorators, constant
    or no defaults, no annotations, names unknown to the old file), else None."""
    o, n = old.body, new.body
    if len(n) <= len(o):
        return None
    known = _top_names(old)
    added, i = [], 0
    for node in n:
        if i < len(o) and _ast_same(node, o[i]):
            i += 1
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
            return None
        if node.name in known or node.returns is not None:
            return None
        a = node.args
        if any(x.annotation is not None for x in a.posonlyargs + a.args + a.kwonlyargs):
            return None
        if any(x.annotation is not None for x in (a.vararg, a.kwarg) if x is not None):
            return None
        if any(not isinstance(d, ast.Constant) for d in a.defaults + [d for d in a.kw_defaults if d is not None]):
            return None
        added.append(node.name)
    if i != len(o):
        return None
    return added or None


_INTROSPECT = {"locals", "vars", "eval", "exec", "globals", "dir"}


def _normalize_locals(fn: ast.AST) -> ast.AST | None:
    """Copy of a function with its plain local variables renamed canonically, or None
    when renaming could be observable (introspection, closures, global/nonlocal)."""
    import copy
    params = {a.arg for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs}
    params |= {a.arg for a in (fn.args.vararg, fn.args.kwarg) if a is not None}
    stored, order = set(), []
    for n in ast.walk(fn):
        if n is not fn and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return None
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            return None
        if isinstance(n, ast.Name):
            if n.id in _INTROSPECT or n.id in ("super", "__class__"):
                return None
            if isinstance(n.ctx, ast.Store) and n.id not in params:
                stored.add(n.id)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id in stored and n.id not in order:
            order.append(n.id)
    mapping = {name: f"_L{i}" for i, name in enumerate(order)}
    fn2 = copy.deepcopy(fn)
    for n in ast.walk(fn2):
        if isinstance(n, ast.Name) and n.id in mapping:
            n.id = mapping[n.id]
    return fn2


def _locals_renamed_only(old: ast.Module, new: ast.Module) -> bool:
    """True when the modules differ only inside functions, and each differing function
    is identical after canonical renaming of its plain local variables."""
    found = []

    def same(a, b):
        if type(a) is not type(b):
            return False
        if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _ast_same(a, b):
                return True
            if a.name != b.name:
                return False
            na, nb = _normalize_locals(a), _normalize_locals(b)
            if na is None or nb is None or not _ast_same(na, nb):
                return False
            found.append(a.name)
            return True
        if isinstance(a, ast.AST):
            return all(same(getattr(a, f, None), getattr(b, f, None)) for f in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
        return a == b

    try:
        return same(old, new) and bool(found)
    except RecursionError:
        return False


def _load_core() -> set:
    import json
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "core_modules.json")) as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


_CORE_MODULES = _load_core()  # modules a bare `import sqlglot` loads at the pinned commit
CORE_ONLY_PROC = os.environ.get("BR_CORE_ONLY_PROC", "1") == "1" and bool(_CORE_MODULES)

_RICH = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE")
_CMP_FAMILY = {
    "Eq": [o for o in _RICH if o != "Eq"] + ["Is", "IsNot"],
    "NotEq": [o for o in _RICH if o != "NotEq"] + ["Is", "IsNot"],
    "Lt": [o for o in _RICH if o != "Lt"], "LtE": [o for o in _RICH if o != "LtE"],
    "Gt": [o for o in _RICH if o != "Gt"], "GtE": [o for o in _RICH if o != "GtE"],
    "Is": ["IsNot", "Eq", "NotEq"], "IsNot": ["Is", "Eq", "NotEq"],
    "In": ["NotIn"], "NotIn": ["In"],
}


def _op_swaps(old: ast.AST, new: ast.AST) -> list | None:
    """[(line, col, old_op, new_op)] when the trees differ only in the operator of
    single-operator comparisons inside functions or lambdas; else None."""
    out = []

    def same(a, b, depth):
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            d = depth + isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if isinstance(a, ast.BoolOp) and len(a.values) == 2 == len(b.values) and type(a.op) is not type(b.op):
                if depth == 0:
                    return False
                out.append((a.lineno, a.col_offset, "Bool", "BoolSwap"))
                return all(same(x, y, d) for x, y in zip(a.values, b.values))
            if isinstance(a, ast.Compare) and len(a.ops) == 1 == len(b.ops) and type(a.ops[0]) is not type(b.ops[0]):
                if depth == 0:
                    return False
                out.append((a.lineno, a.col_offset, type(a.ops[0]).__name__, type(b.ops[0]).__name__))
                return same(a.left, b.left, d) and same(a.comparators, b.comparators, d)
            return all(same(getattr(a, f, None), getattr(b, f, None), d) for f in a._fields)
        if isinstance(a, list):
            return len(a) == len(b) and all(same(x, y, depth) for x, y in zip(a, b))
        return a == b

    if not same(old, new, 0) or not out:
        return None
    return out


_MUTATES = False  # set True if anything below ever mutates old_tree in place
_SWAP_RE = re.compile(r"\bis\b|==|!=")


def _maybe_swap(old: str, new: str) -> bool:
    """Cheap necessary condition for an is/== swap: the changed region has such an operator."""
    a, b = old.splitlines(), new.splitlines()
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    j = 0
    while j < min(len(a), len(b)) - i and a[-1 - j] == b[-1 - j]:
        j += 1
    ra = "\n".join(a[i:len(a) - j])
    rb = "\n".join(b[i:len(b) - j])
    return bool(_SWAP_RE.search(ra)) and bool(_SWAP_RE.search(rb))


def swap_equivalent(old_tree, new_tree, path: str) -> bool:
    """True if the trees differ only by is/== swaps against None or enum members.

    sqlglot's __eq__ implementations all return False for None and enum members
    compare by identity; executor.env.reverse_key.__eq__ would raise, so the
    executor's env module is excluded.
    """
    if path.endswith("executor/env.py"):
        return False
    try:
        return _swap_same(old_tree, new_tree)
    except RecursionError:
        import copy
        return ast.dump(_SwapNorm().visit(copy.deepcopy(old_tree))) == ast.dump(_SwapNorm().visit(copy.deepcopy(new_tree)))


def _norm_ops(node):
    operands = [node.left] + node.comparators
    out = []
    for i, op in enumerate(node.ops):
        t = type(op)
        if any(_is_none(x) or _is_enumish(x) for x in (operands[i], operands[i + 1])):
            t = {ast.Is: ast.Eq, ast.IsNot: ast.NotEq}.get(t, t)
        out.append(t)
    return out


def _ast_same(a, b) -> bool:
    """ast.dump(a) == ast.dump(b), without building the strings."""
    try:
        return _same(a, b)
    except RecursionError:
        return ast.dump(a) == ast.dump(b)


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            if not _same(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _swap_same(a, b) -> bool:
    """Structural equality of two ASTs (fields only, as ast.dump) with _SwapNorm's op mapping."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        if isinstance(a, ast.Compare):
            return (_swap_same(a.left, b.left) and _swap_same(a.comparators, b.comparators)
                    and _norm_ops(a) == _norm_ops(b))
        for f in a._fields:
            if not _swap_same(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_swap_same(x, y) for x, y in zip(a, b))
    return a == b


class _ScopeIndex:
    """Line -> innermost statement and enclosing scope for one module AST."""

    def __init__(self, tree: ast.Module):
        self.stmts: list[tuple[int, int, ast.stmt, tuple]] = []
        self._walk(tree.body, ("module",), "")

    def _walk(self, body, scope, qual):
        for node in body:
            end = node.end_lineno or node.lineno
            start = node.lineno
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
                start = min(d.lineno for d in node.decorator_list)
            self.stmts.append((start, end, node, scope))
            q = f"{qual}.{node.name}" if qual and hasattr(node, "name") else getattr(node, "name", qual)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk(node.body, ("func", node, q), q)
            elif isinstance(node, ast.ClassDef):
                self._walk(node.body, ("class", node, q), q)
            else:
                for attr in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(node, attr, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], (ast.stmt, ast.excepthandler)):
                        self._walk(sub, scope, qual)
                if isinstance(node, ast.Match):
                    for case in node.cases:
                        self._walk(case.body, scope, qual)
                if isinstance(node, ast.Try) or getattr(ast, "TryStar", None) and isinstance(node, getattr(ast, "TryStar")):
                    pass

    def _innermost(self, ln):
        best = None
        for (a, b, node, scope) in self.stmts:
            if a <= ln <= b and (best is None or (b - a) <= (best[1] - best[0])):
                best = (a, b, node, scope)
        return best

    def stmt_at(self, ln):
        b = self._innermost(ln)
        return b[2] if b else None

    def kind(self, ln) -> tuple:
        b = self._innermost(ln)
        if b is None:
            return ("module", None, "")
        a, e, node, scope = b
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_start = node.body[0].lineno
            if ln < body_start:  # decorator / signature / defaults
                encl = scope
                q = node.name if encl[0] == "module" else f"{encl[2]}.{node.name}"
                if encl[0] == "func":
                    return ("func", encl[1], encl[2])  # nested def runs at call time
                return ("funchead", node, q)
        if isinstance(node, ast.ClassDef):
            body_start = node.body[0].lineno
            if ln < body_start:  # class header / decorators / bases
                if scope[0] == "func":
                    return ("func", scope[1], scope[2])
                return ("module", None, "") if scope[0] == "module" else ("class", scope[1], scope[2])
        return scope if scope[0] != "module" else ("module", None, "")

    def own_lines(self, node) -> list[int]:
        """Lines belonging to a statement, excluding nested statement bodies."""
        start = node.lineno
        end = node.end_lineno or start
        children = []
        for attr in ("body", "orelse", "finalbody", "handlers"):
            sub = getattr(node, attr, None)
            if isinstance(sub, list):
                children.extend(sub)
        if isinstance(node, ast.Match):
            for c in node.cases:
                children.extend(c.body)
        if children:
            first = min(c.lineno for c in children)
            end = max(start, first - 1)
            # orelse/else headers live between bodies; include the whole span of
            # headers by collecting gaps between child statements.
            lines = set(range(start, end + 1))
            spans = sorted(((c.lineno, c.end_lineno or c.lineno) for c in children))
            for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
                lines.update(range(b1 + 1, a2))
            return sorted(lines)
        return list(range(start, end + 1))

    def names_assigned(self, ln) -> set:
        node = self.stmt_at(ln)
        names: set = set()
        if node is None:
            return names
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        for t in targets:
            for sub in ast.walk(t):
                if isinstance(sub, ast.Name):
                    names.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    names.add(sub.attr)
        return names
