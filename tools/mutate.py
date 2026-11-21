"""Generate synthetic patches against the pinned sqlglot, like the sample set.

usage: mutate.py REPO OUT_DIR N SEED
Writes OUT_DIR/<id>.patch and OUT_DIR/kinds.json ({id: kind}).
"""
import ast
import difflib
import json
import random
import re
import string
import sys
from pathlib import Path

COMMENTS = ["# Cheap check first", "# Intentionally not cached", "# TODO: revisit",
            "# keep in sync with the generator", "# noqa", "# type: ignore",
            "# fast path", "# see issue tracker", "# pragma: no cover"]
SQL_COMMENTS = ["-- note: keep", "# title: extra case", "-- TODO", "/* comment */"]


def unified(path: str, old: str, new: str) -> str:
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


class Mutator:
    def __init__(self, repo: Path, rng: random.Random):
        self.repo = repo
        self.rng = rng
        self.py = [p for p in sorted((repo / "sqlglot").rglob("*.py")) if p.stat().st_size > 2000]
        self.weights = [p.stat().st_size for p in self.py]
        self.fixtures = sorted((repo / "tests/fixtures").rglob("*.sql"))
        self.opt_fixtures = [p for p in self.fixtures if "optimizer" in str(p)]
        self.tests_py = sorted((repo / "tests").rglob("test_*.py"))

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.repo))

    # ---- python code mutations -------------------------------------------
    def code(self, path: Path | None = None, tries: int = 60):
        rng = self.rng
        for _ in range(tries):
            p = path or rng.choices(self.py, self.weights)[0]
            src = p.read_text()
            lines = src.split("\n")
            tree = ast.parse(src)
            cands = []
            for node in ast.walk(tree):
                ln = getattr(node, "lineno", None)
                if ln is None or node.lineno != getattr(node, "end_lineno", node.lineno):
                    continue
                if isinstance(node, ast.BoolOp):
                    cands.append(("boolop", node))
                elif isinstance(node, ast.Compare):
                    cands.append(("compare", node))
                elif isinstance(node, ast.Constant) and isinstance(node.value, (int, str, bool)) and node.value is not None:
                    cands.append(("const", node))
                elif isinstance(node, ast.If):
                    cands.append(("negate", node.test))
                elif isinstance(node, ast.Return) and node.value is not None:
                    cands.append(("return", node))
                elif isinstance(node, (ast.Expr, ast.Assign, ast.AugAssign)) and not isinstance(getattr(node, "value", None), ast.Constant):
                    cands.append(("delete", node))
                elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
                    cands.append(("binop", node))
            rng.shuffle(cands)
            for kind, node in cands[:10]:
                ln = node.lineno - 1
                line = lines[ln]
                new = self._edit(kind, node, line)
                if new is None or new == line:
                    continue
                out = lines[:]
                if new == "<DELETE>":
                    indent = re.match(r"\s*", line).group(0)
                    out[ln] = indent + "pass"
                else:
                    out[ln] = new
                nsrc = "\n".join(out)
                try:
                    ast.parse(nsrc)
                except SyntaxError:
                    continue
                return self.rel(p), src, nsrc, f"code:{kind}"
        return None

    def _edit(self, kind, node, line):
        rng = self.rng
        a, b = node.col_offset, node.end_col_offset
        seg = line[a:b]
        if kind == "boolop":
            if " and " in seg:
                return line[:a] + seg.replace(" and ", " or ", 1) + line[b:]
            if " or " in seg:
                return line[:a] + seg.replace(" or ", " and ", 1) + line[b:]
        if kind == "compare":
            for x, y in [(" is not ", " != "), (" == ", " != "), (" != ", " == "), (" is ", " == "),
                         (" not in ", " in "), (" in ", " not in "), (" <= ", " < "), (" >= ", " > "),
                         (" < ", " <= "), (" > ", " >= ")]:
                if x in seg:
                    return line[:a] + seg.replace(x, y, 1) + line[b:]
        if kind == "const":
            v = node.value
            if isinstance(v, bool):
                return line[:a] + str(not v) + line[b:]
            if isinstance(v, int):
                return line[:a] + str(v + rng.choice([-1, 1])) + line[b:]
            if isinstance(v, str) and len(v) >= 2 and seg[:1] in "\"'":
                q = seg[0]
                if seg.startswith(q * 3):
                    return None
                body = seg[1:-1]
                if not body or "\\" in body or "{" in body:
                    return None
                i = rng.randrange(len(body))
                if body.isupper():
                    nb = body[:i] + body[i + 1:] if len(body) > 1 else body + "X"
                else:
                    nb = body[:i] + rng.choice(string.ascii_lowercase) + body[i + 1:]
                return line[:a] + q + nb + q + line[b:]
        if kind == "negate":
            return line[:a] + "not (" + seg + ")" + line[b:]
        if kind == "return":
            return line[:a] + "return None" + line[b:]
        if kind == "delete":
            return "<DELETE>"
        if kind == "binop":
            if " + " in seg:
                return line[:a] + seg.replace(" + ", " - ", 1) + line[b:]
            if " - " in seg:
                return line[:a] + seg.replace(" - ", " + ", 1) + line[b:]
        return None

    # ---- comments / docstrings / no-op-ish ------------------------------------
    def comment(self, path: Path | None = None):
        rng = self.rng
        p = path or rng.choices(self.py, self.weights)[0]
        src = p.read_text()
        lines = src.split("\n")
        tree = ast.parse(src)
        stmts = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
        n = rng.choice(stmts)
        ln = n.lineno - 1
        indent = re.match(r"\s*", lines[ln]).group(0)
        out = lines[:ln] + [indent + rng.choice(COMMENTS)] + lines[ln:]
        return self.rel(p), src, "\n".join(out), "comment"

    def docstring(self):
        rng = self.rng
        for _ in range(30):
            p = rng.choices(self.py, self.weights)[0]
            src = p.read_text()
            tree = ast.parse(src)
            docs = [n.body[0] for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)
                    and isinstance(n.body[0].value.value, str)]
            if not docs:
                continue
            d = rng.choice(docs)
            lines = src.split("\n")
            ln = d.lineno - 1
            if '"""' not in lines[ln]:
                continue
            lines[ln] = lines[ln].replace('"""', '"""Note: ', 1)
            return self.rel(p), src, "\n".join(lines), "docstring"
        return None

    def unused_import(self):
        p = self.rng.choices(self.py, self.weights)[0]
        src = p.read_text()
        lines = src.split("\n")
        i = next((k for k, l in enumerate(lines) if l.startswith(("import ", "from "))), 0)
        out = lines[:i] + ["import typing as _typing_unused"] + lines[i:]
        return self.rel(p), src, "\n".join(out), "unused_import"

    def whitespace(self):
        p = self.rng.choices(self.py, self.weights)[0]
        src = p.read_text()
        lines = src.split("\n")
        idx = [k for k, l in enumerate(lines) if l.strip() == ""]
        if not idx:
            return None
        k = self.rng.choice(idx)
        out = lines[:k] + [""] + lines[k:]
        return self.rel(p), src, "\n".join(out), "whitespace"

    # ---- class-level tables (TRANSFORMS etc.) --------------------------------
    def table_entry(self):
        rng = self.rng
        for _ in range(40):
            p = rng.choices(self.py, self.weights)[0]
            src = p.read_text()
            tree = ast.parse(src)
            entries = []
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                for st in cls.body:
                    if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(st.value, ast.Dict):
                        for k, v in zip(st.value.keys, st.value.values):
                            if k is not None and k.lineno == v.end_lineno == k.end_lineno:
                                entries.append((k, v))
            if not entries:
                continue
            k, v = rng.choice(entries)
            lines = src.split("\n")
            ln = k.lineno - 1
            line = lines[ln]
            if not line.strip().endswith(","):
                continue
            if rng.random() < 0.5:
                indent = re.match(r"\s*", line).group(0)
                lines[ln] = indent + "# " + line.strip()  # drop the entry
                kind = "table:drop"
            else:
                seg = line[v.col_offset:v.end_col_offset]
                m = re.search(r'"([A-Z_]{3,})"', seg)
                if not m:
                    continue
                w = m.group(1)
                lines[ln] = line[:v.col_offset] + seg.replace(f'"{w}"', f'"{w[:-1]}"', 1) + line[v.end_col_offset:]
                kind = "table:rename"
            nsrc = "\n".join(lines)
            try:
                ast.parse(nsrc)
            except SyntaxError:
                continue
            return self.rel(p), src, nsrc, kind
        return None

    # ---- fixtures -------------------------------------------------------------
    def fixture(self, comment: bool = False):
        rng = self.rng
        pool = self.opt_fixtures if rng.random() < 0.8 else self.fixtures
        p = rng.choice(pool)
        src = p.read_text()
        lines = src.split("\n")
        idx = [k for k, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
        if not idx:
            return None
        k = rng.choice(idx)
        line = lines[k]
        if comment:
            out = lines[:k] + [rng.choice(SQL_COMMENTS)] + lines[k:]
            return self.rel(p), src, "\n".join(out), "fixture:comment"
        toks = list(re.finditer(r"[A-Za-z_][A-Za-z_0-9]*|\.|\(|\)|,|\d+", line))
        if not toks:
            return None
        t = rng.choice(toks)
        r = rng.random()
        if r < 0.4:
            new = line[:t.start()] + t.group(0)[:1] + rng.choice("axz") + t.group(0)[1:] + line[t.end():]
        elif r < 0.7:
            new = line[:t.end()] + " 1" + line[t.end():]
        else:
            new = line[:t.start()] + line[t.end():]
        lines[k] = new
        return self.rel(p), src, "\n".join(lines), "fixture"

    # ---- test files -------------------------------------------------------------
    def test_edit(self):
        rng = self.rng
        for _ in range(30):
            p = rng.choice(self.tests_py)
            src = p.read_text()
            lines = src.split("\n")
            idx = [k for k, l in enumerate(lines) if re.search(r'"[^"]{6,}"', l) and "def " not in l]
            if not idx:
                continue
            k = rng.choice(idx)
            m = list(re.finditer(r'"([^"\\{}]{6,})"', lines[k]))
            if not m:
                continue
            mm = rng.choice(m)
            s = mm.group(1)
            i = rng.randrange(len(s))
            ns = s[:i] + ("X" if s[i] != "X" else "Y") + s[i + 1:]
            lines[k] = lines[k][:mm.start(1)] + ns + lines[k][mm.end(1):]
            return self.rel(p), src, "\n".join(lines), "test_edit"
        return None


def main() -> None:
    repo, out, n, seed = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    m = Mutator(repo, rng)
    plan = (["code"] * 40 + ["fixture"] * 18 + ["comment"] * 8 + ["multi"] * 8 + ["table"] * 8
            + ["docstring"] * 3 + ["fixture_comment"] * 4 + ["test_edit"] * 5 + ["unused_import"] * 3
            + ["whitespace"] * 3)
    kinds = json.loads((out / "kinds.json").read_text()) if (out / "kinds.json").exists() else {}
    made = 0
    while made < n:
        k = rng.choice(plan)
        edits = []
        if k == "code":
            edits = [m.code()]
        elif k == "fixture":
            edits = [m.fixture()]
        elif k == "fixture_comment":
            edits = [m.fixture(comment=True)]
        elif k == "comment":
            edits = [m.comment()]
        elif k == "table":
            edits = [m.table_entry()]
        elif k == "docstring":
            edits = [m.docstring()]
        elif k == "test_edit":
            edits = [m.test_edit()]
        elif k == "unused_import":
            edits = [m.unused_import()]
        elif k == "whitespace":
            edits = [m.whitespace()]
        elif k == "multi":
            parts = rng.sample(["code", "comment", "fixture", "table"], rng.choice([2, 3]))
            fn = {"code": m.code, "comment": m.comment, "fixture": m.fixture, "table": m.table_entry}
            edits = [fn[x]() for x in parts]
        if not edits or any(e is None for e in edits):
            continue
        paths = [e[0] for e in edits]
        if len(set(paths)) != len(paths):
            continue
        cid = "m" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(5))
        text = "".join(unified(path, old, new) for path, old, new, _ in edits)
        (out / f"{cid}.patch").write_text(text)
        kinds[cid] = "+".join(e[3] for e in edits) if k == "multi" else edits[0][3]
        made += 1
    (out / "kinds.json").write_text(json.dumps(kinds, indent=0))
    print(f"made {made}", file=sys.stderr)


if __name__ == "__main__":
    main()
