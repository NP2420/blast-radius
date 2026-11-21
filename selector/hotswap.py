"""Run local tests for function-body-only patches without re-importing sqlglot.

A forked child of a process that already imported pristine sqlglot replaces the
__code__ of each changed function with the patched code. That is equivalent to a
fresh import of the patched tree only when:
  * every patched file is a sqlglot module whose AST differs only inside the bodies
    of existing functions or methods (same signatures, defaults, decorators),
  * no nested function or lambda differs (closures made earlier would keep old code),
  * the live function is reachable through module / class dicts, and its free
    variables are unchanged.
plan() returns None whenever any of this does not hold; callers then fall back to a
fresh import in a symlink farm.
"""
from __future__ import annotations

import ast
import os
import types


def _same_sig(a, b) -> bool:
    """Structural AST equality where function bodies are ignored (signatures kept)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            if f == "body" and isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _same_sig(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(_same_sig(x, y) for x, y in zip(a, b))
    return a == b


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        return all(_same(getattr(a, f, None), getattr(b, f, None)) for f in a._fields)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _funcs(tree: ast.Module):
    """[(qualname, def)] for every function not nested in another function, in source
    order, walking into if/try/with/for blocks at module and class level."""
    out = []

    def walk(node, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{qual}{child.name}", child))
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{qual}{child.name}.")
            elif isinstance(child, (ast.Lambda,)):
                continue
            elif isinstance(child, ast.stmt) or isinstance(child, (ast.excepthandler, ast.match_case)):
                walk(child, qual)

    walk(tree, "")
    return out


def _has_nested_diff(a: ast.AST, b: ast.AST) -> bool:
    """True if any nested def/lambda inside the two function bodies differs (or count differs)."""
    def nested(f):
        return [n for n in ast.walk(f) if n is not f and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef))]
    na, nb = nested(a), nested(b)
    if len(na) != len(nb):
        return True
    return any(not _same(x, y) for x, y in zip(na, nb))


_OLD: dict = {}


def plan(repo: str, patched: dict, trees: dict | None = None) -> list | None:
    """[(module, path, new_source, [qualnames])] or None if not hot-swappable.

    trees: optional {path: (old_tree, new_tree)} already parsed by the analyzer.
    """
    out = []
    for path, new in patched.items():
        if new is None or not path.startswith("sqlglot/") or not path.endswith(".py"):
            return None
        try:
            if trees and trees.get(path):
                ot, nt = trees[path]
            else:
                if path not in _OLD:
                    _OLD[path] = ast.parse(open(os.path.join(repo, path), encoding="utf-8").read())
                ot, nt = _OLD[path], ast.parse(new)
        except (OSError, SyntaxError, ValueError):
            return None
        try:
            if not _same_sig(ot, nt):
                return None
        except RecursionError:
            return None
        of, nf = _funcs(ot), _funcs(nt)
        if [q for q, _ in of] != [q for q, _ in nf]:
            return None
        if len({q for q, _ in of}) != len(of):
            return None  # duplicate qualnames (conditional definitions): ambiguous
        changed = []
        for (q, a), (_, b) in zip(of, nf):
            if not _same(a, b):
                if _has_nested_diff(a, b):
                    return None
                changed.append(q)
        mod = path[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        out.append((mod, path, new, changed))
    return out


def _codes(code: types.CodeType, out: dict) -> None:
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            out.setdefault(c.co_qualname, c)
            _codes(c, out)


def apply(swaps: list, modules: dict) -> bool:
    """Swap code objects in place (in a forked child). False if any swap is impossible."""
    for mod, path, new, changed in swaps:
        m = modules.get(mod)
        if m is None:
            return False
        if not changed:
            continue
        codes: dict = {}
        _codes(compile(new, getattr(m, "__file__", path) or path, "exec", dont_inherit=True), codes)
        for q in changed:
            nc = codes.get(q)
            if nc is None:
                return False
            parts = q.split(".")
            obj = m.__dict__.get(parts[0])
            for p in parts[1:]:
                if not isinstance(obj, type):
                    return False
                obj = obj.__dict__.get(p)
            if isinstance(obj, (staticmethod, classmethod)):
                obj = obj.__func__
            seen = 0
            while not isinstance(obj, types.FunctionType) or obj.__code__.co_qualname != q:
                obj = getattr(obj, "__wrapped__", None)
                seen += 1
                if obj is None or seen > 10:
                    return False
            if obj.__code__.co_freevars != nc.co_freevars:
                return False
            obj.__code__ = nc
    return True
