"""Compile raw per-test map files into one compact pickle for the selector.

usage: compile_maps.py RAW_DIR REPO TESTS_JSON OUT_PICKLE

Test sets are Python ints used as bitsets over the map's test list.
"""
import ast
import glob
import json
import os
import pickle
import sys
from pathlib import Path


def main() -> None:
    raw, repo, tests_json, out = sys.argv[1:5]
    tests = json.loads(Path(tests_json).read_text())["tests"]
    ids = [t["id"] for t in tests]
    idx = {i: n for n, i in enumerate(ids)}

    sets: list[int] = []
    set_id: dict[int, int] = {}

    def sid(bits: int) -> int:
        if bits not in set_id:
            set_id[bits] = len(sets)
            sets.append(bits)
        return set_id[bits]

    cov: dict[str, dict[int, int]] = {}
    opens: dict[str, int] = {}
    recv: dict[str, int] = {}
    tcls: dict[str, int] = {}
    procs: dict[int, list] = {}
    failed_at_baseline = []
    seen = 0
    for f in glob.glob(os.path.join(raw, "t", "*.json")):
        d = json.loads(Path(f).read_text())
        n = idx.get(d["nodeid"])
        if n is None:
            continue
        seen += 1
        bit = 1 << n
        if d["failed"]:
            failed_at_baseline.append(d["nodeid"])
        for path, lines in d["lines"].items():
            fm = cov.setdefault(path, {})
            for ln in lines:
                fm[ln] = fm.get(ln, 0) | bit
        for p in d["opens"]:
            opens[p] = opens.get(p, 0) | bit
        for k in d["recv"]:
            recv[k] = recv.get(k, 0) | bit
        for k in d.get("tcls", []):
            tcls[k] = tcls.get(k, 0) | bit
        if d["procs"]:
            procs[n] = d["procs"]
    missing = [i for i in ids if i not in {json.loads(Path(f).read_text())["nodeid"] for f in []}]
    print(f"tests with maps: {seen}/{len(ids)}; failed at baseline: {failed_at_baseline}", file=sys.stderr)

    parent_opens: set = set()
    for f in glob.glob(os.path.join(raw, "parent_opens.*.json")):
        parent_opens.update(json.loads(Path(f).read_text()))
    import_lines = json.loads(Path(raw, "import.json").read_text())

    # Functions per python file, and which functions reference which names.
    funcs: dict[str, list] = {}
    name_bits: dict[str, int] = {}
    for root in ("sqlglot", "tests"):
        for py in Path(repo, root).rglob("*.py"):
            rel = str(py.relative_to(repo))
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            fl = []

            def walk(body, qual):
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        q = f"{qual}.{node.name}" if qual else node.name
                        if not isinstance(node, ast.ClassDef):
                            fl.append((q, node.lineno, node.end_lineno or node.lineno, node))
                        walk(node.body, q)
                    else:
                        for attr in ("body", "orelse", "finalbody", "handlers"):
                            sub = getattr(node, attr, None)
                            if isinstance(sub, list):
                                walk([s for s in sub if isinstance(s, ast.AST)], qual)

            walk(tree.body, "")
            funcs[rel] = [(q, a, b) for (q, a, b, _) in fl]
            fm = cov.get(rel, {})
            for (q, a, b, node) in fl:
                bits = 0
                for ln in range(a, b + 1):
                    bits |= fm.get(ln, 0)
                if not bits:
                    continue
                names = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
                    elif isinstance(sub, ast.Attribute):
                        names.add(sub.attr)
                for nm in names:
                    name_bits[nm] = name_bits.get(nm, 0) | bits

    # Owner-qualified attribute references (e.g. Postgres.Tokenizer.KEYWORDS):
    # key "<owner suffix>.<attr>" -> tests that can observe the reference.
    attr_bits: dict[str, int] = {}
    file_cov_bits = {p: 0 for p in cov}
    for p, fm in cov.items():
        b_ = 0
        for v in fm.values():
            b_ |= v
        file_cov_bits[p] = b_
    for py in Path(repo, "sqlglot").rglob("*.py"):
        rel = str(py.relative_to(repo))
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        fm = cov.get(rel, {})

        def chain(node):
            parts = []
            while isinstance(node, ast.Attribute):
                parts.append(node.attr)
                node = node.value
            if isinstance(node, ast.Name):
                parts.append(node.id)
                return parts[::-1]
            return None

        def visit(node, ctx_bits_fn):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    a0, b0 = child.lineno, child.end_lineno or child.lineno
                    fb = 0
                    for ln in range(a0, b0 + 1):
                        fb |= fm.get(ln, 0)
                    visit(child, lambda fb=fb: fb)
                    continue
                if isinstance(child, ast.ClassDef):
                    # qualname tracking for class bodies
                    visit_class(child, ctx_bits_fn)
                    continue
                if isinstance(child, ast.Attribute):
                    ch = chain(child)
                    if ch and len(ch) >= 3:
                        bits = ctx_bits_fn()
                        owner, attr = ch[:-1], ch[-1]
                        for k in range(1, len(owner) + 1):
                            key = ".".join(owner[-k:]) + "." + attr
                            attr_bits[key] = attr_bits.get(key, 0) | bits
                visit(child, ctx_bits_fn)

        qual_stack = []

        def visit_class(cls, outer_fn):
            qual_stack.append(cls.name)
            q = ".".join(qual_stack)
            key = f"{mod}:{q}"
            visit(cls, lambda key=key: recv.get(key, 0))
            qual_stack.pop()

        visit(tree, lambda rel=rel: file_cov_bits.get(rel, 0))

    # Names read by code that runs at import (functions with import-time lines,
    # including string constants passed to getattr), and aliases created by
    # class-body / module-level assignments (FOO = Parent.BAR makes BAR -> FOO).
    import_ref_names: set = set()
    import_func_refs: set = set()
    import_const_keys: set = set()   # "ATTR|str:KEY" subscripted/get with a constant at module/class level
    aliases: dict[str, set] = {}
    for py in Path(repo, "sqlglot").rglob("*.py"):
        rel = str(py.relative_to(repo))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        il = set(import_lines.get(rel, []))

        def names_in(node):
            out = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    out.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    out.add(sub.attr)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier():
                    out.add(sub.value)
            return out

        def scan(body, in_func):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lines = range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1)
                    if any(l in il for l in lines):
                        import_ref_names.update(names_in(node))
                        import_func_refs.update(names_in(node))
                    continue
                if not in_func:
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Attribute) \
                                and isinstance(sub.slice, ast.Constant) and isinstance(sub.slice.value, str):
                            import_const_keys.add(f"{sub.value.attr}|str:{sub.slice.value}")
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) and sub.func.attr == "get" \
                                and isinstance(sub.func.value, ast.Attribute) and sub.args \
                                and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
                            import_const_keys.add(f"{sub.func.value.attr}|str:{sub.args[0].value}")
                if isinstance(node, ast.ClassDef):
                    scan(node.body, False)
                    continue
                if not in_func and isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    tnames = set()
                    for t in targets:
                        for sub in ast.walk(t):
                            if isinstance(sub, ast.Name):
                                tnames.add(sub.id)
                    if node.value is not None:
                        for n in names_in(node.value):
                            aliases.setdefault(n, set()).update(tnames - {n})
                elif not in_func and isinstance(node, (ast.Expr, ast.For, ast.If, ast.With)):
                    import_ref_names.update(names_in(node))

        scan(tree.body, False)

    # Line-level readers of each name: tests that executed a statement in which
    # the name appears (attribute, bare name, or identifier-like string constant).
    name_line_bits: dict[str, int] = {}
    for py in Path(repo, "sqlglot").rglob("*.py"):
        rel = str(py.relative_to(repo))
        fm = cov.get(rel)
        if not fm:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            body = fn.body if isinstance(fn.body, list) else [fn.body]
            for st in (n for b in body for n in ast.walk(b) if isinstance(n, (ast.stmt, ast.expr)) and hasattr(n, "lineno")):
                if not isinstance(st, ast.stmt) and not isinstance(fn, ast.Lambda):
                    continue
                own = range(st.lineno, (getattr(st, "end_lineno", None) or st.lineno) + 1)
                if isinstance(st, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef, ast.Match)):
                    first_child = min((c.lineno for c in getattr(st, "body", []) if hasattr(c, "lineno")), default=st.lineno + 1)
                    own = range(st.lineno, max(st.lineno, first_child - 1) + 1)
                    nodes = [st.test] if hasattr(st, "test") else ([st.iter, st.target] if hasattr(st, "iter") else (st.items if hasattr(st, "items") else []))
                    if isinstance(st, ast.Match):
                        nodes = [st.subject]
                else:
                    nodes = [st]
                names = set()
                for nd in nodes:
                    for sub in ast.walk(nd):
                        if isinstance(sub, ast.Attribute):
                            names.add(sub.attr)
                        elif isinstance(sub, ast.Name):
                            names.add(sub.id)
                        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier():
                            names.add(sub.value)
                if not names:
                    continue
                bits = 0
                for ln in own:
                    bits |= fm.get(ln, 0)
                if bits:
                    for nm in names:
                        name_line_bits[nm] = name_line_bits.get(nm, 0) | bits

    # Which test files import which names from which sqlglot modules
    # ("sqlglot.helper:seq_get" -> tests of files that do `from sqlglot.helper import seq_get`).
    test_name_imports: dict[str, int] = {}
    ftb = {}
    for t in tests:
        ftb[t["file"]] = ftb.get(t["file"], 0) | (1 << idx[t["id"]])
    for py in Path(repo, "tests").rglob("*.py"):
        rel = str(py.relative_to(repo))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        bits_f = ftb.get(rel, 0)
        if not bits_f:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("sqlglot"):
                for a_ in node.names:
                    k_ = f"{node.module}:{a_.name}"
                    test_name_imports[k_] = test_name_imports.get(k_, 0) | bits_f

    # Tests per test file, and test-module import graph (static, transitive).
    file_tests: dict[str, int] = {}
    for t in tests:
        file_tests[t["file"]] = file_tests.get(t["file"], 0) | (1 << idx[t["id"]])
    imports: dict[str, set] = {}
    for py in Path(repo, "tests").rglob("*.py"):
        rel = str(py.relative_to(repo))
        mod = rel[:-3].replace("/", ".")
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        deps = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests"):
                deps.add(node.module)
                for a in node.names:
                    deps.add(f"{node.module}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("tests"):
                        deps.add(a.name)
        imports[mod] = deps
    test_importers: dict[str, int] = {}
    for t in tests:
        mod = t["file"][:-3].replace("/", ".")
        stack, seen_m = [mod], set()
        while stack:
            m = stack.pop()
            if m in seen_m:
                continue
            seen_m.add(m)
            stack.extend(imports.get(m, ()))
        for m in seen_m:
            test_importers[m] = test_importers.get(m, 0) | (1 << idx[t["id"]])

    # Lines run while collecting test file A affect A's tests and the tests of
    # every test module A imports (their module code ran under A's context).
    collect_raw = json.loads(Path(raw, "collect.json").read_text()) if Path(raw, "collect.json").exists() else {}
    mod_file = {t["file"][:-3].replace("/", "."): t["file"] for t in tests}
    collect: dict[str, dict[int, int]] = {}
    for afile, files in collect_raw.items():
        amod = afile[:-3].replace("/", ".")
        stack, seen_m = [amod], set()
        while stack:
            m = stack.pop()
            if m in seen_m:
                continue
            seen_m.add(m)
            stack.extend(imports.get(m, ()))
        bits = 0
        for m in seen_m:
            if m in mod_file:
                bits |= file_tests.get(mod_file[m], 0)
        if afile == "?":
            bits = (1 << len(ids)) - 1  # unattributed collection work: everyone
        for path, lines in files.items():
            fm = collect.setdefault(path, {})
            for ln in lines:
                fm[ln] = fm.get(ln, 0) | bits

    proc_bits = 0
    for n in procs:
        proc_bits |= 1 << n

    maps = {
        "tests": ids,
        "sets": None,
        "cov": {p: {ln: sid(b) for ln, b in fm.items()} for p, fm in cov.items()},
        "opens": {p: sid(b) for p, b in opens.items()},
        "recv": {k: sid(b) for k, b in recv.items()},
        "tcls": {k: sid(b) for k, b in tcls.items()},
        "name_users": {k: sid(b) for k, b in name_bits.items()},
        "attr_refs": {k: sid(b) for k, b in attr_bits.items() if b},
        "import_ref_names": import_ref_names,
        "test_name_imports": {k: sid(b_) for k, b_ in test_name_imports.items()},
        "import_func_refs": import_func_refs,
        "import_const_keys": import_const_keys,
        "name_line_users": {k: sid(b) for k, b in name_line_bits.items()},
        "aliases": {k: sorted(v) for k, v in aliases.items() if v},
        "funcs": funcs,
        "file_tests": file_tests,
        "test_importers": test_importers,
        "parent_opens": parent_opens,
        "import_lines": {p: set(v) for p, v in import_lines.items()},
        "collect": {p: {ln: sid(b) for ln, b in fm.items()} for p, fm in collect.items()},
        "proc_tests": proc_bits,
        "procs": procs,
    }
    if len(sys.argv) > 5 and os.path.exists(sys.argv[5]):
        lm = json.loads(Path(sys.argv[5]).read_text())
        maps["local_ms"] = lm["tests"]
        maps["class_setup"] = {k: v for k, v in lm["class_setup"].items() if v >= 20}
    if len(sys.argv) > 6 and os.path.exists(sys.argv[6]):
        tc = json.loads(Path(sys.argv[6]).read_text())
        task_cov = {}
        for tid, v in tc.items():
            qm = {}
            for path, d in v["q"].items():
                for ln, qs in d.items():
                    bits_q = 0
                    for qi in qs:
                        bits_q |= 1 << qi
                    qm[(path, int(ln))] = bits_q
            task_cov[tid] = {"n": v["n"], "rest": {p_: set(l_) for p_, l_ in v["rest"].items()}, "q": qm}
        maps["task_cov"] = task_cov
        # positions of names on lines these tests execute (for attribute-level narrowing)
        lines_needed = set()
        for tid in task_cov:
            ti = idx.get(tid)
            if ti is None:
                continue
            for path_, fm_ in cov.items():
                for ln_, b_ in fm_.items():
                    if b_ >> ti & 1:
                        lines_needed.add((path_, ln_))
        pos: dict = {}
        for path in {p_ for p_, _ in lines_needed}:
            try:
                tree = ast.parse(Path(repo, path).read_text(encoding="utf-8"))
            except (SyntaxError, OSError):
                continue
            for sub in ast.walk(tree):
                nm = sub.attr if isinstance(sub, ast.Attribute) else sub.id if isinstance(sub, ast.Name) else (
                    sub.value if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.isidentifier() else None)
                if nm is None or not hasattr(sub, "lineno"):
                    continue
                for ln in range(sub.lineno, (getattr(sub, "end_lineno", None) or sub.lineno) + 1):
                    if (path, ln) in lines_needed:
                        pos.setdefault(nm, set()).add((path, ln))
        maps["task_name_pos"] = pos
    if len(sys.argv) > 7 and os.path.isdir(sys.argv[7]):
        key_bits: dict[str, int] = {}
        nkeys = 0
        for f in glob.glob(os.path.join(sys.argv[7], "*.json")):
            d = json.loads(Path(f).read_text())
            n_ = idx.get(d["nodeid"])
            if n_ is None:
                continue
            nkeys += 1
            for k in d["keys"]:
                key_bits[k] = key_bits.get(k, 0) | (1 << n_)
        maps["key_users"] = {k: sid(b_) for k, b_ in key_bits.items()}
        maps["key_tests"] = nkeys
        print(f"key map: {nkeys} tests, {len(key_bits)} keys", file=sys.stderr)
    maps["sets"] = sets
    with open(out, "wb") as f:
        pickle.dump(maps, f, protocol=5)
    print(f"sets={len(sets)} cov_files={len(cov)} opens={len(opens)} recv={len(recv)} "
          f"names={len(name_bits)} procs={ {ids[n]: v for n, v in procs.items()} } "
          f"size={os.path.getsize(out)}", file=sys.stderr)


if __name__ == "__main__":
    main()
