"""Map a patch to candidate tests using the precomputed dependency maps.

Every decision errs toward more candidates: a missed failure costs 300 s,
an extra candidate costs a local run or a slot on a worker.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field

from patchparse import ApplyError, FileDiff, apply

# Paths no test reads or imports at the pinned commit (checked against the
# file-open map at build time). Anything else unknown is "broad".
INERT_PREFIXES = ("benchmarks/", "posts/", "pdoc/", "docs/", ".github/", "sqlglot-integration-tests/")
INERT_SUFFIXES = (".md", ".png", ".svg", ".rst", ".txt")


@dataclass
class Result:
    tests: int = 0                                 # bitset over map test indexes
    strong: int = 0                                # direct evidence: executed the changed statement / read the file
    import_fp: bool = False                        # touches code run at import: check state fingerprint
    import_heur: int = 0                           # import-time candidates by heuristics (used if no fingerprint)
    fp_func: bool = False                          # function code that also runs at import changed
    broad: str | None = None                       # reason, if we must run everything
    patched: dict = field(default_factory=dict)   # path -> new text (None = deleted)
    notes: list = field(default_factory=list)


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
        for fd in fds:
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
            else:
                self._data_file(path, res)
            if res.broad:
                return res
        return res

    # ---- non-python files ------------------------------------------------
    def _data_file(self, path: str, res: Result) -> None:
        if path in self.m["parent_opens"]:
            res.broad = f"read at import/collection: {path}"
            return
        t = self.opens(path)
        if t:
            res.tests |= t
            res.strong |= t
            res.notes.append(f"{path}: {bin(t).count('1')} readers")
            return
        if path.startswith(INERT_PREFIXES) or path.endswith(INERT_SUFFIXES):
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
            if path.startswith(INERT_PREFIXES) and not t and path not in self.m["parent_opens"]:
                res.notes.append(f"{path}: inert python")
                return
            res.broad = f"python outside package {path}"
            return
        try:
            old_tree = ast.parse(old)
            new_tree = ast.parse(new)
        except SyntaxError:
            res.broad = f"syntax error {path}"
            return
        # Text readers of the source (inspect.getsource, subprocess tools).
        res.tests |= self.opens(path)
        if ast.dump(old_tree) == ast.dump(new_tree):
            res.notes.append(f"{path}: AST unchanged")
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
                if nb >= 1:
                    targets.append(("old", nb))
            if any(k[0] != "func" for k in kinds):
                for n in new_lines:
                    k = new_idx.kind(n)
                    if k[0] != "func":
                        self._import_time(path, module, k, new_idx, n, res, tree="new")

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
                if any(x in imp for x in lines):
                    # Function code that also runs during import (metaclasses, table
                    # builders, parsing at import): if the patched import builds
                    # different state, every test is a candidate (decided later
                    # by fingerprinting the patched import).
                    res.import_fp = True
                    res.fp_func = True
                    res.tests |= self.m["proc_tests"]
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
            res.tests |= self.m["proc_tests"]
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
        if path.startswith("sqlglot/") and k in ("class", "module"):
            # Import-time state change: the fingerprint of the patched import says
            # exactly which attributes changed; these heuristics are the fallback.
            before = res.tests
            res.tests = 0
            self._import_time_heur(path, module, kind, idx, ln, res, tree)
            res.import_heur |= res.tests
            res.tests = before
            res.import_fp = True
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
        if k == "module":
            res.tests |= self.cov_file(path)
            res.tests |= self.collect_file(path)
            res.tests |= self.recv_module(module)
            if path.startswith("tests/"):
                res.tests |= self.m["file_tests"].get(path, 0)
                res.tests |= self.m["test_importers"].get(module, 0)
            names = idx.names_assigned(ln)
            res.tests |= self._name_users(names)
            res.notes.append(f"{path}:{ln} module level")
            return
        res.broad = f"unresolved scope {path}:{ln}"

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
