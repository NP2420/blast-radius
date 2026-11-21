"""Fingerprint of sqlglot's import-time state.

After importing every sqlglot module, hash each module global and each class
attribute (recursively: containers, enums, closures and defaults of functions,
expressions by repr). Two trees whose fingerprints match built the same
import-time state, so a patch to code that runs during import only matters
through the code paths tests execute at run time.

usage (baseline): python fingerprint.py REPO OUT.pkl
"""
from __future__ import annotations

import enum
import hashlib
import importlib
import pickle
import pkgutil
import sys
import types

_MAX_DEPTH = 8


_LOOSE = [False]
_MEMO: dict = {}  # id(obj) -> (obj, digest-string) for functions within one fingerprint run


def _h(x, depth: int, seen: set) -> str:
    t = type(x)
    if t is types.FunctionType:
        hit = _MEMO.get((id(x), _LOOSE[0]))
        if hit is not None and hit[0] is x:
            return hit[1]
        r = _h_inner(x, depth, seen)
        _MEMO[(id(x), _LOOSE[0])] = (x, r)
        return r
    return _h_inner(x, depth, seen)


def _h_inner(x, depth: int, seen: set) -> str:
    t = type(x)
    if x is None or t in (bool, int, float, complex, str, bytes):
        return t.__name__ + ":" + repr(x)
    if depth > _MAX_DEPTH:
        return t.__name__ + ":deep"
    if isinstance(x, enum.Enum):
        return f"enum:{t.__module__}.{t.__qualname__}.{x.name}"
    if isinstance(x, type):
        return f"type:{x.__module__}.{x.__qualname__}"
    if isinstance(x, types.ModuleType):
        return "mod:" + x.__name__
    oid = id(x)
    if oid in seen:
        return "cycle:" + t.__name__
    seen.add(oid)
    try:
        if t in (tuple, list):
            hs = [_h(v, depth + 1, seen) for v in x]
            return t.__name__ + "(" + ",".join(sorted(hs) if _LOOSE[0] else hs) + ")"
        if t in (set, frozenset):
            return t.__name__ + "{" + ",".join(sorted(_h(v, depth + 1, seen) for v in x)) + "}"
        if isinstance(x, dict):
            items = [(_h(k, depth + 1, seen), _h(v, depth + 1, seen)) for k, v in x.items()]
            return t.__name__ + "{" + ",".join(sorted(f"{k}={v}" for k, v in items)) + "}"
        if isinstance(x, (types.FunctionType,)):
            parts = [f"fn:{x.__module__}.{x.__qualname__}"]
            if x.__defaults__:
                parts.append(_h(x.__defaults__, depth + 1, seen))
            if x.__kwdefaults__:
                parts.append(_h(x.__kwdefaults__, depth + 1, seen))
            if x.__closure__:
                cells = []
                for c in x.__closure__:
                    try:
                        cells.append(_h(c.cell_contents, depth + 1, seen))
                    except ValueError:
                        cells.append("empty")
                parts.append("[" + ",".join(cells) + "]")
            return "|".join(parts)
        if isinstance(x, (types.MethodType,)):
            return "meth:" + _h(x.__func__, depth + 1, seen) + "@" + _h(x.__self__, depth + 1, seen)
        if isinstance(x, (staticmethod, classmethod)):
            return t.__name__ + ":" + _h(x.__func__, depth + 1, seen)
        if isinstance(x, property):
            return "prop:" + ",".join(_h(f, depth + 1, seen) for f in (x.fget, x.fset, x.fdel))
        if isinstance(x, types.BuiltinFunctionType):
            return f"builtin:{getattr(x, '__module__', '')}.{getattr(x, '__qualname__', repr(x))}"
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot") and hasattr(x, "args") and hasattr(x, "sql"):
            return "expr:" + repr(x)  # sqlglot expression trees repr deterministically
        if hasattr(x, "pattern") and hasattr(x, "flags"):  # compiled regex
            return f"re:{x.pattern!r}:{x.flags}"
        r = repr(x)
        if " at 0x" not in r and "object at" not in r:
            return f"{mod}.{t.__qualname__}:{r}"
        d = getattr(x, "__dict__", None)
        if isinstance(d, dict):
            return f"obj:{mod}.{t.__qualname__}" + _h(d, depth + 1, seen)
        slots = getattr(t, "__slots__", None)
        if slots:
            vals = {s: getattr(x, s, None) for s in ([slots] if isinstance(slots, str) else slots)}
            return f"obj:{mod}.{t.__qualname__}" + _h(vals, depth + 1, seen)
        return f"opaque:{mod}.{t.__qualname__}"
    finally:
        seen.discard(oid)


def import_all() -> list:
    import sqlglot
    mods = [sqlglot]
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            mods.append(importlib.import_module(m.name))
        except Exception:
            pass
    return mods


def fingerprint(loose_only: bool = False) -> dict[str, tuple]:
    """{"module:name" or "module:Class.attr": (strict, loose) digests} for all sqlglot state.

    loose_only (run time): skip the strict digest; diff() compares loose digests only.
    """
    _MEMO.clear()
    out: dict[str, tuple] = {}

    def one(v) -> str:
        return hashlib.blake2b(_h(v, 0, set()).encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()

    def dig(v) -> tuple:
        strict = None if loose_only else one(v)
        _LOOSE[0] = True
        try:
            loose = one(v)
        finally:
            _LOOSE[0] = False
        return (strict, loose)

    def do_class(modname, qual, cls, depth=0):
        for k, v in list(cls.__dict__.items()):
            if k in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__"):
                continue
            key = f"{modname}:{qual}.{k}"
            if isinstance(v, type) and v.__module__ == modname and v.__qualname__ == f"{qual}.{k}" and depth < 4:
                do_class(modname, f"{qual}.{k}", v, depth + 1)
                out[key] = "class"
            else:
                out[key] = dig(v)

    mods = sorted(import_all(), key=lambda m: m.__name__)
    import logging
    import warnings
    out["global:recursionlimit"] = dig(sys.getrecursionlimit())
    out["global:warnings"] = dig([(f[0], str(f[1]), getattr(f[2], "__name__", str(f[2])), str(f[3]), f[4]) for f in warnings.filters])
    out["global:loggers"] = dig(sorted((n, getattr(l, "level", None), getattr(l, "disabled", None))
                                       for n, l in logging.root.manager.loggerDict.items() if n.startswith("sqlglot")))
    for mod in mods:
        name = mod.__name__
        for k, v in list(vars(mod).items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            key = f"{name}:{k}"
            if isinstance(v, type) and v.__module__ == name:
                do_class(name, v.__qualname__, v)
                out[key] = "class"
            else:
                out[key] = dig(v)
    return out


def diff(base: dict, cur: dict) -> list[str]:
    """base: {"fp": {...}, "loose": set, "ignore": set} from build_baseline."""
    fp, loose, ignore = base["fp"], base["loose"], base["ignore"]
    out = []
    for k in set(fp) | set(cur):
        if k in ignore:
            continue
        a, b = fp.get(k), cur.get(k)
        if a is None or b is None or not isinstance(a, tuple) or not isinstance(b, tuple):
            if a != b:
                out.append(k)
            continue
        if a[1] != b[1]:  # order-insensitive digest: set-built lists vary by process
            out.append(k)
    return sorted(out)


def build_baseline(repo: str, out: str, seeds=(1, 2, 3, 4, 5)) -> None:
    import os
    import subprocess
    import tempfile
    fps = []
    for sd in seeds:
        tmp = tempfile.mktemp(suffix=".pkl")
        env = dict(os.environ, PYTHONHASHSEED=str(sd), PYTHONPATH=repo)
        subprocess.run([sys.executable, __file__, "--one", repo, tmp], env=env, check=True)
        with open(tmp, "rb") as f:
            fps.append(pickle.load(f))
        os.unlink(tmp)
    base = fps[0]
    loose, ignore = set(), set()
    for k in base:
        stricts = {fp.get(k)[0] if isinstance(fp.get(k), tuple) else fp.get(k) for fp in fps}
        looses = {fp.get(k)[1] if isinstance(fp.get(k), tuple) else fp.get(k) for fp in fps}
        if len(stricts) > 1:
            (loose if len(looses) == 1 else ignore).add(k)
    with open(out, "wb") as f:
        pickle.dump({"fp": base, "loose": loose, "ignore": ignore}, f, protocol=5)
    print(f"fingerprint: {len(base)} entries, {len(loose)} order-free, ignored {sorted(ignore)}", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "--one":
        import unittest  # noqa: F401  (sqlglot reads "unittest" in sys.modules at import, as under pytest)
        repo, out = sys.argv[2], sys.argv[3]
        sys.path.insert(0, repo)
        with open(out, "wb") as f:
            pickle.dump(fingerprint(), f, protocol=5)
    else:
        build_baseline(sys.argv[1], sys.argv[2])
