"""pytest plugin: which keys of class-level tables each test looks up.

After importing every sqlglot module, every UPPERCASE class attribute whose
value is a plain dict is replaced by a TrackingDict, and the generator's
dispatch builder returns a TrackingDict too. Each test runs in a forked child
(fresh caches) and records (owner, attr, key) for lookups, and (owner, attr,
"*") when the table is iterated or copied (every key counts then).
Writes KEY_OUT/<test>.json.
"""
import enum
import importlib
import json
import os
import pkgutil
import sys

import pytest

OUT = os.environ.get("KEY_OUT", "/tmp/keys")
_rec: set = set()
_SUPPRESS = [False]


def keyrepr(k) -> str:
    if isinstance(k, enum.Enum):
        return f"enum:{type(k).__name__}.{k.name}"
    if isinstance(k, type):
        return f"type:{k.__name__}"
    if isinstance(k, str):
        return "str:" + k
    return "other:" + type(k).__name__


class TrackingDict(dict):
    __slots__ = ("_tag",)

    def _hit(self, k):
        if not _SUPPRESS[0]:
            _rec.add((self._tag, keyrepr(k)))

    def _all(self):
        if not _SUPPRESS[0]:
            _rec.add((self._tag, "*"))

    def __getitem__(self, k):
        self._hit(k)
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        self._hit(k)
        return dict.get(self, k, default)

    def __contains__(self, k):
        self._hit(k)
        return dict.__contains__(self, k)

    def setdefault(self, k, default=None):
        self._hit(k)
        return dict.setdefault(self, k, default)

    def pop(self, k, *a):
        self._hit(k)
        return dict.pop(self, k, *a)

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def items(self):
        self._all()
        return dict.items(self)

    def values(self):
        self._all()
        return dict.values(self)

    def copy(self):
        self._all()
        return dict.copy(self)

    def __or__(self, other):
        self._all()
        return dict.__or__(self, other)

    def __ror__(self, other):
        self._all()
        return dict.__ror__(self, other)

    def __eq__(self, other):
        self._all()
        return dict.__eq__(self, other)

    __hash__ = None

    def __reduce__(self):
        return (dict, (dict(self),))


def _wrap(d: dict, tag: str) -> TrackingDict:
    t = TrackingDict(d)
    t._tag = tag
    return t


def _instrument():
    import sqlglot
    mods = [sqlglot]
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name == "sqlglot.__main__":
            continue
        try:
            mods.append(importlib.import_module(m.name))
        except Exception:
            pass
    n = 0
    seen = set()

    def do_class(cls):
        nonlocal n
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for attr, v in list(vars(cls).items()):
            if isinstance(v, type) and v.__module__ == cls.__module__:
                do_class(v)
            if attr.isupper() and type(v) is dict:
                try:
                    setattr(cls, attr, _wrap(v, f"{attr}"))
                    n += 1
                except (AttributeError, TypeError):
                    pass

    for mod in mods:
        for v in list(vars(mod).values()):
            if isinstance(v, type) and (v.__module__ or "").startswith("sqlglot"):
                do_class(v)
    # Generator dispatch cache: built lazily from TRANSFORMS (+ *_sql methods).
    try:
        import sqlglot.generator as g
        orig = g._build_dispatch

        def build(cls):
            # The copy of TRANSFORMS into the dispatch table is not a use of every
            # entry; lookups in the dispatch table are.
            _SUPPRESS[0] = True
            try:
                d = orig(cls)
            finally:
                _SUPPRESS[0] = False
            return _wrap(d, "TRANSFORMS")

        g._build_dispatch = build
        g._DISPATCH_CACHE.clear()
    except Exception as e:  # pragma: no cover
        print("dispatch hook failed", e, file=sys.stderr)
    print(f"instrumented {n} tables", file=sys.stderr)


def pytest_configure(config):
    os.makedirs(OUT, exist_ok=True)
    sys.path.insert(0, os.environ.get("MAP_SELECTOR", "/home/claude/dev/br/selector"))
    import syncpool
    syncpool.install()


def pytest_collection_finish(session):
    _instrument()


def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol
    pid = os.fork()
    if pid == 0:
        try:
            _rec.clear()
            reports = runtestprotocol(item, nextitem=None, log=False)
            safe = item.nodeid.replace("/", "_").replace("::", "__")
            with open(os.path.join(OUT, safe[:200] + ".json"), "w") as f:
                json.dump({"nodeid": item.nodeid, "failed": any(r.failed for r in reports),
                           "keys": sorted(f"{a}|{k}" for a, k in _rec)}, f)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    return True
