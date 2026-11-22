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
            kr = keyrepr(k)
            for tg in self._tag:
                _rec.add((tg, kr))

    def _all(self):
        if not _SUPPRESS[0]:
            for tg in self._tag:
                _rec.add((tg, "*"))

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


def _wrap(d: dict, tag) -> TrackingDict:
    t = TrackingDict(d)
    t._tag = tuple(tag)
    return t


def _owner(c) -> str:
    return f"{c.__module__}:{c.__qualname__}"


_TRIVIAL = (str, int, float, bool, type(None), enum.Enum, type)


def _owners(tables):
    """tables: attr -> {cls: dict}. Returns (cls, attr) -> set of owner names whose
    edit can reach this dict: classes in the MRO defining the attr, plus classes
    outside the MRO the dict looks copied from (identical values not explained by a
    shared ancestor), closed transitively."""
    own = {}
    for attr, by_cls in tables.items():
        for c in by_cls:
            own[(c, attr)] = {_owner(a) for a in c.__mro__ if attr in vars(a)}
        links = {c: set() for c in by_cls}
        cl = list(by_cls)
        for c in cl:
            dc = by_cls[c]
            mro_c = set(c.__mro__)
            for k in cl:
                if k is c or k in mro_c:
                    continue
                dk = by_cls[k]
                common = [a for a in k.__mro__ if a in mro_c and a in by_cls]
                # K's own entries: not inherited unchanged from an ancestor shared with C.
                own_k = [(key, v) for key, v in dk.items()
                         if not any(key in by_cls[a] and by_cls[a][key] is v for a in common)]
                if not own_k:
                    continue
                n = sum(1 for key, v in own_k if key in dc and dc[key] is v)
                if n >= 2 and n >= 0.5 * len(own_k) or n == len(own_k):
                    links[c].add(k)
        changed = True
        while changed:
            changed = False
            for c in cl:
                for k in links[c]:
                    add = own[(k, attr)] - own[(c, attr)]
                    if add:
                        own[(c, attr)] |= add
                        changed = True
    return own


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
    tables: dict = {}

    def do_class(cls):
        if id(cls) in seen:
            return
        seen.add(id(cls))
        for attr, v in list(vars(cls).items()):
            if isinstance(v, type) and v.__module__ == cls.__module__:
                do_class(v)
            if attr.isupper() and type(v) is dict:
                tables.setdefault(attr, {})[cls] = v

    for mod in mods:
        for v in list(vars(mod).values()):
            if isinstance(v, type) and (v.__module__ or "").startswith("sqlglot"):
                do_class(v)
    own = _owners(tables)
    tag_map = {}
    for attr, by_cls in tables.items():
        for cls, v in by_cls.items():
            tg = sorted(f"{o}|{attr}" for o in own[(cls, attr)])
            try:
                setattr(cls, attr, _wrap(v, tg))
                n += 1
                tag_map[f"{_owner(cls)}.{attr}"] = tg
            except (AttributeError, TypeError):
                pass
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "_tags.json.meta"), "w") as f:
        json.dump(tag_map, f)
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
            src = cls.TRANSFORMS
            tg = src._tag if isinstance(src, TrackingDict) else tuple(
                f"{_owner(a)}|TRANSFORMS" for a in cls.__mro__ if "TRANSFORMS" in vars(a))
            return _wrap(d, tg)

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
