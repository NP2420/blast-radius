"""Tests that can fail when a sqlglot module cannot be imported (syntax error).

usage: add_broken_maps.py MAPS_IN REPO CORE_JSON MAPS_OUT [MODNEED_JSON]
maps["broken_mod"]: module -> set id. A module that a bare `import sqlglot` loads, or
that such a module imports, is left out (every test fails). Otherwise the tests are
those that ran code of, received a class from, or imported a name from any module that
imports it (any import statement, also inside functions; transitively), plus tests
that load every dialect when one of those modules is a dialect.
"""
import ast, glob, json, os, pickle, sys

mi, repo, core_p, mo = sys.argv[1:5]
need = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {}  # modneed.py output
m = pickle.load(open(mi, "rb")); ids = m["tests"]; sets = m["sets"]
core = set(json.load(open(core_p)))
mods = {}
for p in glob.glob(os.path.join(repo, "sqlglot", "**", "*.py"), recursive=True):
    rel = os.path.relpath(p, repo)
    mod = rel[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    mods[mod] = rel
rev = {}      # module -> modules importing it at module level (when they load)
lazy = {}     # module -> [(importing file, line)] imports inside functions


def _targets(n, mod, rel):
    s = set()
    if isinstance(n, ast.Import):
        for a in n.names:
            s.add(a.name)
    else:
        base = n.module or ""
        if n.level:
            pk = mod.split(".")
            if not rel.endswith("__init__.py"):
                pk = pk[:-1]
            pk = pk[: len(pk) - (n.level - 1)]
            base = ".".join(pk + ([base] if base else []))
        s.add(base)
        for a in n.names:
            s.add(f"{base}.{a.name}")
    for x in list(s):
        parts = x.split(".")
        for i in range(1, len(parts)):
            s.add(".".join(parts[:i]))
    return {x for x in s if x in mods and x != mod}


def _is_tc(test):
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


for mod, rel in mods.items():
    try:
        t = ast.parse(open(os.path.join(repo, rel), encoding="utf-8").read())
    except SyntaxError:
        continue

    def walk(node, in_func):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.If) and _is_tc(c.test):
                for x in c.orelse:
                    walk_stmt(x, in_func)
                continue
            walk_stmt(c, in_func)

    def walk_stmt(c, in_func):
        if isinstance(c, (ast.Import, ast.ImportFrom)):
            for x in _targets(c, mod, rel):
                if in_func:
                    lazy.setdefault(x, []).append((rel, c.lineno))
                else:
                    rev.setdefault(x, set()).add(mod)
            return
        walk(c, in_func or isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    walk(t, False)


def cov_file(rel):
    out = 0
    for ln, sid in m["cov"].get(rel, {}).items():
        out |= sets[sid]
    return out


recv_by_mod, names_by_mod = {}, {}
for k, sid in m["recv"].items():
    recv_by_mod.setdefault(k.split(":")[0], []).append(sid)
for k, sid in m["test_name_imports"].items():
    names_by_mod.setdefault(k.split(":")[0], []).append(sid)
# Tests that load (nearly) every dialect.
cnt = [0] * len(ids)
dmods = [x for x in mods if x.startswith("sqlglot.dialects.") and x not in core]
for dm in dmods:
    b = 0
    for sid in recv_by_mod.get(dm, []):
        b |= sets[sid]
    for i in range(len(ids)):
        if b >> i & 1:
            cnt[i] += 1
all_d = 0
for i, c in enumerate(cnt):
    if c >= len(dmods) * 0.8:
        all_d |= 1 << i
dedup = {b: i for i, b in enumerate(sets)}
out = {}
for x in mods:
    if x in core:
        continue
    cl, todo = {x}, [x]
    while todo:
        y = todo.pop()
        for z in rev.get(y, ()):
            if z not in cl:
                cl.add(z)
                todo.append(z)
    if cl & core:
        continue
    b = 0
    for y in cl:
        for f, ln in lazy.get(y, ()):
            sid = m["cov"].get(f, {}).get(ln)
            if sid is not None:
                b |= sets[sid]
        b |= cov_file(mods[y])
        for sid in recv_by_mod.get(y, []) + names_by_mod.get(y, []):
            b |= sets[sid]
    for i, t in enumerate(ids):
        nm = need.get(t, ())
        if nm is None or x in nm:
            b |= 1 << i
    if any(y.startswith(("sqlglot.dialects.", "sqlglot.parsers.", "sqlglot.generators.", "sqlglot.tokenizers.", "sqlglot.typing.")) for y in cl):
        b |= all_d
    s = dedup.get(b)
    if s is None:
        s = dedup[b] = len(sets)
        sets.append(b)
    out[x] = s
m["broken_mod"] = out
pickle.dump(m, open(mo, "wb"), protocol=5)
print(f"broken_mod: {len(out)} modules; all-dialect tests {bin(all_d).count('1')}; core-reaching {len(mods) - len(out)}")
