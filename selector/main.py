"""Blast Radius selector: for each patch, pick the tests that can fail.

Pipeline per commit:
  1. analyze the patch against dependency maps -> candidate tests (or "broad")
  2. plan: cheap candidates are run locally, expensive ones included directly,
     and mid-cost ones ride free in worker slots a heavy test already pays for
  3. run local candidates (forked children, both cores)
  4. output = included + locally failing, packed to minimise busiest + 2 s/worker
"""
from __future__ import annotations

import heapq
import json
import os
import pickle
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import patchparse  # noqa: E402
from runner import LocalRunner, fp_then_tests, preload, warm_import, prewarm_executor  # noqa: E402
EXEC_CLS = "tests/test_executor.py::TestExecutor"
import hotswap  # noqa: E402
import runner as runner_mod  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
REPO = os.environ.get("BR_REPO", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", str(HERE.parent / "maps" / "maps.pkl"))
FP_BASE = os.environ.get("BR_FP_BASE", str(Path(MAPS).parent / "fp_base.pkl"))
FP_NARROW = os.environ.get("BR_FP_NARROW", "0") == "1"
EQUIV = os.environ.get("BR_EQUIV", "1") == "1"
# Heavy tests (>= HEAVY_MS) are listed only with strong evidence (diagnostic / policy).
DROP_WEAK_HEAVY = os.environ.get("BR_DROP_WEAK_HEAVY", "0") == "1"
HEAVY_MS = int(os.environ.get("BR_HEAVY_MS", "1000"))
EQUIV_PROBE_MS = int(os.environ.get("BR_EQUIV_PROBE_MS", "500"))
# Per-query runs of heavy executor tests (tpcds); off by default.
TASK_PARTIAL = os.environ.get("BR_TASK_PARTIAL", "1") == "1"
TMP = os.environ.get("BR_TMP", "/tmp")
WORKER_MS = 2000
LOCAL_OVERHEAD_MS = int(os.environ.get("BR_LOCAL_OVERHEAD_MS", "150"))
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))
HOT = os.environ.get("BR_HOT", "1") == "1"
FP_MAXKEYS = int(os.environ.get("BR_FP_MAXKEYS", "50"))
PREWARM = os.environ.get("BR_PREWARM", "0") == "1"
LAZY_CHECK = os.environ.get("BR_LAZY_CHECK", "0") == "1"
FILL_SLACK = os.environ.get("BR_FILL_SLACK", "1") == "1"
PREWARM_PLAN = os.environ.get("BR_PREWARM_PLAN", "0") == "1"
PREWARM_SETUP_MS = int(os.environ.get("BR_PREWARM_SETUP_MS", "150"))
LOCAL_BUDGET_S = float(os.environ.get("BR_LOCAL_BUDGET_S", "110"))
NO_LOCAL = os.environ.get("BR_NO_LOCAL") == "1"
T0 = time.monotonic()


def log(*a) -> None:
    print(f"[{time.monotonic() - T0:6.2f}]", *a, file=sys.stderr, flush=True)


def pack(nums: list[int], ms: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Best LPT packing over 1..8 workers: (cost_ms, workers)."""
    if not nums:
        return 0, []
    order = sorted(nums, key=lambda n: -ms[n])
    best = None
    for k in range(1, min(8, len(order)) + 1):
        heap = [(0, i) for i in range(k)]
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            load, i = heapq.heappop(heap)
            bins[i].append(n)
            heapq.heappush(heap, (load + ms[n], i))
        cost = max(l for l, _ in heap) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best


def pack_cost(nums, ms) -> int:
    return pack(list(nums), ms)[0]


CLASS_SETUP_MS = {"tests/test_executor.py::TestExecutor": 1200}
TPC_PROXY = os.environ.get("BR_TPC_PROXY", "1") == "1"
BROKEN_FP = os.environ.get("BR_BROKEN_FP", "1") == "1"
# Lines the TPC-DS test executes changed: it fails about half the time there, so a
# trace that usually ends in listing it anyway is not worth its CPU.
TPC_STRONG = os.environ.get("BR_TPC_STRONG", "1") == "1"
TPC_P_CAP = float(os.environ.get("BR_TPC_P_CAP", "0.5"))
TPC_EXEC = os.environ.get("BR_TPC_EXEC", "1") == "1"  # observed: strong TPC-DS fails about half the time
TPC_TRACE_BASE_MS = int(os.environ.get("BR_TPC_TRACE_BASE_MS", "400"))
TPC_TRACE_Q_MS = int(os.environ.get("BR_TPC_TRACE_Q_MS", "62"))  # replaced from maps when present
LOCAL_MS: dict[int, int] = {}
PROXIED: set = set()  # heavy tests verified locally by a cheap proxy  # measured local CPU per test number (falls back to recorded)
_class_of: dict[int, str] = {}


def local_cost(nums, ms) -> float:
    if not nums:
        return 0.0
    setup = sum(CLASS_SETUP_MS.get(c, 0) for c in {_class_of.get(n, "") for n in nums})
    return LOCAL_OVERHEAD_MS + setup + sum(
        (HEAVY_CPU_FACTOR if ms[n] >= HEAVY_LOCAL_MS else CPU_FACTOR) * LOCAL_MS.get(n, ms[n]) for n in nums)


P_STRONG = float(os.environ.get("BR_P_STRONG", "0.30"))
P_WEAK = float(os.environ.get("BR_P_WEAK", "0.01"))
P_MEDIUM = float(os.environ.get("BR_P_MEDIUM", "0.15"))
# Failure rates by evidence kind and how many tests share it (calibrated on labeled commits):
# a lone test reading a changed fixture or executing a changed line usually fails.
P_BY_SIZE = os.environ.get("BR_P_BY_SIZE", "1") == "1"


P_SCALE = float(os.environ.get("BR_P_SCALE", "0.6"))


P_SCALE_FILE = os.environ.get("BR_P_SCALE_FILE", "1.0")


GEN_MEM_SUBSET = os.environ.get("BR_GEN_MEM_SUBSET", "1") == "1"
_DIALECT_DIRS = ("sqlglot/dialects/", "sqlglot/generators/", "sqlglot/parsers/", "sqlglot/typing/")


def _dialect_specific_modules(patched: dict) -> list | None:
    """Module names when every patched file is one dialect's own module (not a base
    module or package __init__) or a test/fixture file; else None."""
    mods = []
    for p in patched:
        if p.startswith("tests/"):
            continue
        if not p.startswith(_DIALECT_DIRS) or not p.endswith(".py"):
            return None
        base = p.rsplit("/", 1)[-1]
        if base in ("__init__.py", "dialect.py"):
            return None
        mods.append(p[:-3].replace("/", "."))
    return mods or None


def p_of(kind: str, n: int) -> float:
    sc = float(P_SCALE_FILE) if (kind == "file" and P_SCALE_FILE) else P_SCALE
    return min(0.97, sc * _p_of(kind, n))


P_ANY = os.environ.get("BR_P_ANY", "0") == "1"
# Separate scale for cheap tests (heavy ones keep P_SCALE): cheap local runs rarely
# changed the board's simulated time but cost CPU.
P_SCALE_CHEAP = os.environ.get("BR_P_SCALE_CHEAP", "0.8")
HEAVY_LOCAL_MS = int(os.environ.get("BR_HEAVY_LOCAL_MS", "3000"))
HEAVY_CPU_FACTOR = float(os.environ.get("BR_HEAVY_CPU_FACTOR", os.environ.get("BR_CPU_FACTOR", "1.5")))


def a_of(kind: str, n: int) -> float:
    """Odds that at least one test of an evidence group fails (labeled commits)."""
    sc = float(P_SCALE_FILE) if (kind == "file" and P_SCALE_FILE) else P_SCALE
    if kind == "file":
        base = 0.85 if n <= 3 else 0.6
    elif kind == "strong":
        base = 0.8 if n == 1 else 0.65 if n <= 50 else 0.35
    else:
        base = 0.45 if n <= 3 else 0.6 if n <= 10 else 0.35
    return min(0.97, sc * base)


def _p_of(kind: str, n: int) -> float:
    if kind == "file":
        return 0.85 if n <= 3 else 0.3 if n <= 10 else 0.05
    if kind == "strong":
        return 0.8 if n == 1 else 0.5 if n <= 10 else 0.38 if n <= 50 else 0.08
    if kind == "medium":
        return 0.35 if n <= 3 else 0.27 if n <= 10 else 0.05
    return P_WEAK


def plan(cands: list[int], ms: dict[int, int], strong: set | None = None, medium: set | None = None,
         pmap: dict | None = None, groups: list | None = None) -> tuple[list[int], list[int]]:
    """Split candidates into (include directly, run locally), minimising expected cost.

    Expected cost = packed cost of the included set
                  + local CPU (overhead, class setup, test time)
                  + expected cost of tests that fail locally and get listed anyway.
    Strong candidates (directly executed the change) fail often; weak ones rarely.
    """
    if not cands:
        return [], []
    if NO_LOCAL:
        return list(cands), []
    strong = strong or set()
    medium = medium or set()
    if pmap is not None:
        p = {n: pmap.get(n, P_WEAK) for n in cands}
    else:
        p = {n: (P_STRONG if n in strong else P_MEDIUM if n in medium else P_WEAK) for n in cands}
    # Heaviest first; a heavy test with a cheap proxy check goes last (verify it).
    def order(n):
        return 0 if n in PROXIED else -ms[n]
    st = sorted((n for n in cands if n in strong), key=order)
    wk = sorted((n for n in cands if n not in strong), key=order)

    def expected(inc, loc):
        c = pack_cost(inc, ms) + local_cost(loc, ms)
        if loc:
            ef = sum(p[n] * ms[n] for n in loc)
            if not inc:
                if groups is not None:
                    # Failures within one commit are correlated: use per-group odds that
                    # any test of the group fails, scaled by the share of it run locally.
                    none_fail = 1.0
                    ls = set(loc)
                    rest = set(loc)
                    for members, a_any in groups:
                        k = len(members & ls)
                        if k:
                            none_fail *= 1.0 - min(1.0, a_any * k / len(members))
                            rest -= members
                    for n in rest:
                        none_fail *= 1.0 - p[n]
                else:
                    none_fail = 1.0
                    for n in loc:
                        none_fail *= 1.0 - p[n]
                ef += (1.0 - none_fail) * WORKER_MS
            c += ef
        return c

    best = None
    cut_points = range(len(wk) + 1) if len(wk) <= 60 else sorted(
        set([0, len(wk)] + [int(len(wk) * q) for q in (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5)]
            + list(range(0, min(len(wk), 12)))))
    for s_split in sorted(set([0, len(st)] + list(range(0, min(len(st), 6) + 1)))):
        for j in cut_points:
            inc = st[:s_split] + wk[:j]
            loc = st[s_split:] + wk[j:]
            c = expected(inc, loc)
            if best is None or c < best[0]:
                best = (c, inc, loc)
    _, inc, loc = best
    # Free riders: a local test that does not raise the packed cost goes in directly.
    if inc:
        base = pack_cost(inc, ms)
        moved = []
        for n in loc:
            if pack_cost(inc + moved + [n], ms) <= base:
                moved.append(n)
        rest = [n for n in loc if n not in set(moved)]
        for c, setup in CLASS_SETUP_MS.items():
            cls_rest = [n for n in rest if _class_of.get(n) == c]
            if cls_rest and pack_cost(inc + moved + cls_rest, ms) - base <= setup + CPU_FACTOR * sum(LOCAL_MS.get(n, ms[n]) for n in cls_rest):
                moved.extend(cls_rest)
        if moved:
            mset = set(moved)
            inc = inc + moved
            loc = [n for n in loc if n not in mset]
    return inc, loc


def bits_to_nums(bits: int, map_ids, num_by_id) -> set:
    out = set()
    n = 0
    while bits:
        if bits & 1:
            tid = map_ids[n]
            if tid in num_by_id:
                out.add(num_by_id[tid])
        bits >>= 1
        n += 1
    return out


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    num_by_id = {t["id"]: t["number"] for t in tests}
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    sec_by_id = {t["id"]: t["seconds"] for t in tests}
    id_by_num = {t["number"]: t["id"] for t in tests}
    all_nums = [t["number"] for t in tests]
    for t in tests:
        _class_of[t["number"]] = "::".join(t["id"].split("::")[:2])

    with open(MAPS, "rb") as f:
        maps = pickle.load(f)
    import gc
    gc.freeze()  # the maps are permanent: keep the cycle collector from rescanning them
    # Analysis allocates millions of short-lived AST nodes: collect cycles rarely here.
    # (Test children restore the default thresholds.)
    if os.environ.get("BR_GC_THRESH"):
        gc.set_threshold(int(os.environ["BR_GC_THRESH"]), 50, 50)
    map_ids = maps["tests"]
    if "local_ms" in maps:
        for tid, v in maps["local_ms"].items():
            if tid in num_by_id:
                LOCAL_MS[num_by_id[tid]] = v
        CLASS_SETUP_MS.clear()
        CLASS_SETUP_MS.update(maps["class_setup"])
    if runner_mod.PROXY and runner_mod.MAX_NODES_ID in num_by_id:
        LOCAL_MS[num_by_id[runner_mod.MAX_NODES_ID]] = 400  # checked by a scaled proxy
        PROXIED.add(num_by_id[runner_mod.MAX_NODES_ID])
    # TPC-DS execution: compare data-free traces with pristine ones (tpctrace).
    tpc_n = num_by_id.get(runner_mod.TPC_ID)
    tpc_dp: dict = {}
    tpc_base_p = Path(MAPS).parent / "tpc_base.pkl"
    tpc_dp_p = Path(MAPS).parent / "tpc_dataphase.json"
    if TPC_PROXY and tpc_n is not None and tpc_base_p.exists() and tpc_dp_p.exists():
        try:
            runner_mod.TPC_BASE = pickle.loads(tpc_base_p.read_bytes()).get(runner_mod.TPC_ID, {})
            runner_mod.TPC_REPO = REPO
            for f, q, _ in json.loads(tpc_dp_p.read_text()):
                tpc_dp.setdefault(f, set()).add(q)
        except Exception:  # noqa: BLE001
            runner_mod.TPC_BASE = {}
    tpc_exec_p = Path(MAPS).parent / "tpc_exec_base.pkl"
    if TPC_EXEC and tpc_exec_p.exists():
        try:
            _eb = pickle.loads(tpc_exec_p.read_bytes())
            runner_mod.TPC_EXEC_BASE = _eb.get(runner_mod.TPC_ID, {})
            runner_mod.TPC_TIMES = _eb.get("times", {}).get(runner_mod.TPC_ID, {})
        except Exception:  # noqa: BLE001
            runner_mod.TPC_EXEC_BASE = {}
    if not runner_mod.TPC_BASE:
        tpc_n = None
    mapped = set(map_ids)
    unmapped = [num_by_id[i] for i in num_by_id if i not in mapped]
    if unmapped:
        log(f"warning: {len(unmapped)} tests without maps; always candidates")
    an = analyze.Analyzer(maps, REPO)

    patches = sorted((DATA / "commits").glob("*.patch"))
    decisions: dict[str, dict] = {}
    jobs = []
    analyses = {}
    for p in patches:
        cid = p.stem
        try:
            fds = patchparse.parse(p.read_text(encoding="utf-8", errors="replace"))
            if not fds:
                raise ValueError("empty patch")
            analyses[cid] = an.analyze(fds)
        except Exception:
            log(f"{cid}: analysis error\n{traceback.format_exc()}")
            analyses[cid] = analyze.Result(broad="analysis error")
    for c, r in analyses.items():
        if not FP_NARROW:  # class/module edits: heuristic sets, fingerprint as a ripple guard
            r.tests |= r.import_heur
            r.import_fp = r.fp_func or r.state_guard
    fp_jobs = [(c, r.patched) for c, r in analyses.items() if not r.broad and r.import_fp]

    def apply_fp(c, changed):
        r = analyses[c]
        failed = {k.split(":", 1)[1] for k in changed if k.startswith("<import-failed>:")}
        bm = maps.get("broken_mod", {})
        pmods = set()
        for p in r.patched:
            if p.startswith("sqlglot/") and p.endswith(".py"):
                mm = p[:-3].replace("/", ".")
                pmods.add(mm[: -len(".__init__")] if mm.endswith(".__init__") else mm)
        if BROKEN_FP and failed and pmods and pmods <= failed and failed <= set(bm) and all(
                p.startswith("sqlglot/") and p.endswith(".py") for p in r.patched):
            # Every patched module now fails to import: the tests that load one fail;
            # no other test can see the patch.
            b = 0
            for f in failed:
                b |= an.sets[bm[f]]
            r.tests |= b
            r.strong |= b
            r.unverified |= b
            r.broken = True
            r.notes.append(f"import fails for {sorted(failed)[:3]}: {bin(b).count('1')} tests load them")
            return
        if r.key_attrs and not any(k.startswith("<") or k.startswith("global:") for k in changed):
            # The edited table (and same-name copies in subclasses) must differ;
            # anything else that changed is a ripple to account for.
            tags = maps.get("okey_tags") or {}

            def expected(k):
                a = k.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
                if a not in r.key_attrs:
                    return False
                owners = r.key_owners.get(a)
                if a in r.plain_key_attrs or not owners or None in owners:
                    return True
                # Owner-level narrowing: only tables an edit of the owner's table reaches.
                tg = tags.get(k)
                return tg is not None and any(f"{o}|{a}" in tg for o in owners)

            changed = [k for k in changed if not expected(k)]
        if not changed:
            r.notes.append("import state unchanged")
            return
        users = 0
        for key in changed:
            u = an.state_key_users(key)
            if u is None or len(changed) >= FP_MAXKEYS:
                users = an.all_bits
                break
            users |= u
        r.tests |= users
        r.notes.append(f"import state changed ({len(changed)}): {changed[:3]}")

    def tpc_outside_ok(p) -> bool:
        tbit = an.bit_of(runner_mod.TPC_ID)
        return not (p in ("tests/test_executor.py", "tests/helpers.py") or p.startswith(
            ("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/")) or
            not (p.startswith(("tests/", "docs/", "posts/", ".github/")) or p.endswith((".md", ".rst", ".txt"))) or
            (an.opens(p) & tbit) or os.path.basename(p) in ("conftest.py", "__init__.py"))

    def tpc_exec_ok(res) -> bool:
        """Only the executor changed (plus files the TPC-DS run cannot see)."""
        if res.broad or res.broken or not res.patched or not runner_mod.TPC_EXEC_BASE:
            return False
        ex = False
        for p in res.patched:
            if p.startswith("sqlglot/executor/") and p.endswith(".py"):
                ex = True
            elif p.startswith("sqlglot/") or not tpc_outside_ok(p):
                return False
        return ex

    def tpc_proxy_ok(res) -> bool:
        """The patch can change TPC-DS results only through what the trace sees: it
        stays in sqlglot/ outside the executor, and leaves the functions the executor
        runs per row (and their modules' import-time code) alone."""
        if res.broad or not res.patched:
            return False
        tbit = an.bit_of(runner_mod.TPC_ID)
        for p in res.patched:
            if p.startswith("sqlglot/executor/"):
                return False
            if p.startswith("sqlglot/"):
                continue
            # Files outside the package: harmless to the TPC-DS run unless it reads
            # them or they are its own test module / helpers / fixtures.
            if p in ("tests/test_executor.py", "tests/helpers.py") or p.startswith(
                    ("tests/fixtures/optimizer/tpc-ds/", "tests/fixtures/optimizer/tpc-h/")) or \
                    not (p.startswith(("tests/", "docs/", "posts/", ".github/")) or p.endswith((".md", ".rst", ".txt"))) or \
                    (an.opens(p) & tbit) or os.path.basename(p) in ("conftest.py", "__init__.py"):
                return False
        hit = [p for p in res.patched if p in tpc_dp]
        if hit:
            code = {p: t for p, t in res.patched.items() if p.startswith("sqlglot/")}
            swaps = hotswap.plan(REPO, code, res.trees)
            if swaps is None:
                return False
            for _mod, path, _src, quals in swaps:
                if set(quals) & tpc_dp.get(path, set()):
                    return False
        return True

    def decide(cid):
        """Decide include/local/cond for one commit; returns (local test ids, allowed)."""
        res = analyses[cid]
        if res.broad:
            decisions[cid] = {"include": all_nums, "local": [], "broad": res.broad, "cond": []}
            log(f"{cid}: BROAD ({res.broad})")
            return [], {}
        cands = set(unmapped) | bits_to_nums(res.tests, map_ids, num_by_id)
        if res.broken:
            # Unimportable module: every test that loads it fails; nothing to narrow or verify.
            inc = sorted(cands | bits_to_nums(res.unverified, map_ids, num_by_id))
            decisions[cid] = {"include": inc, "local": [], "patched": res.patched, "cond": []}
            log(f"{cid}: cands={len(cands)} include={len(inc)} ({sum(ms[x] for x in inc)} ms) local=0 (0 ms) "
                f"{'; '.join(res.notes)[:200]}")
            return [], {}
        strong = bits_to_nums(res.strong, map_ids, num_by_id)
        medium = bits_to_nums(res.medium, map_ids, num_by_id)
        if DROP_WEAK_HEAVY:
            dropped = [n for n in cands if ms[n] >= HEAVY_MS and n not in strong and n not in medium]
            for n in dropped:
                cands.discard(n)
            if dropped:
                res.notes.append(f"dropped {len(dropped)} non-strong heavy tests")
        # Heavy executor tests with per-query maps: run only the queries that can
        # observe the change (or drop the test when none can).
        allowed: dict = {}
        saved_local = {}
        for tid, qs in res.tpc_queries.items():
            # A TPC fixture edit: the test parses every query, executes the changed
            # executed ones. Run it locally on just those (possibly none: parse only).
            n_ = num_by_id.get(tid)
            if n_ is None or n_ not in cands or len(res.patched) != 1:
                continue
            allowed[tid] = set(qs)
            saved_local[n_] = LOCAL_MS.get(n_, ms[n_])
            n_exec = max(1, (maps.get("task_cov", {}).get(tid) or {}).get("n", 99))
            LOCAL_MS[n_] = int(700 + saved_local[n_] * len(qs) / n_exec)
            res.notes.append(f"{tid.split('::')[-1]}: fixture queries {sorted(qs)[:5]}")
        for tid in (maps.get("task_cov", {}) if TASK_PARTIAL else {}):
            n_ = num_by_id.get(tid)
            if n_ is None or n_ not in cands or tid in allowed:
                continue
            qbits = an.partial_queries(tid, res, n_ in strong, n_ in medium)
            if qbits is None:
                continue
            if qbits == 0 and not (res.tests & ~(res.strong | res.medium) & (1 << map_ids.index(tid))):
                cands.discard(n_)
                res.notes.append(f"{tid.split('::')[-1]}: no query reaches the change")
                continue
            if qbits == 0:
                continue
            qs = {i for i in range(maps["task_cov"][tid]["n"]) if qbits >> i & 1}
            allowed[tid] = qs
            saved_local[n_] = LOCAL_MS.get(n_, ms[n_])
            LOCAL_MS[n_] = int(300 + saved_local[n_] * len(qs) / max(1, maps["task_cov"][tid]["n"]))
            res.notes.append(f"{tid.split('::')[-1]}: {len(qs)} queries")
        tpc_saved = None
        if tpc_n is not None and tpc_n in cands and tpc_exec_ok(res):
            qs = allowed.get(runner_mod.TPC_ID)
            idx = sorted(q for q in qs if q in runner_mod.TPC_EXEC_BASE) if qs is not None else None
            allowed[runner_mod.TPC_ID] = {"exec": idx}
            tpc_saved = (LOCAL_MS.get(tpc_n, ms[tpc_n]), _class_of.get(tpc_n))
            nq = len(idx) if idx is not None else len(runner_mod.TPC_EXEC_BASE)
            LOCAL_MS[tpc_n] = 1800 + 120 * nq  # stops at the first differing query
            _class_of[tpc_n] = "<tpc exec>"
            PROXIED.add(tpc_n)
            res.notes.append(f"tpc-ds executor check on {nq} queries")
        elif tpc_n is not None and tpc_n in cands and (TPC_STRONG or tpc_n not in strong) and tpc_proxy_ok(res):
            qs = allowed.get(runner_mod.TPC_ID)
            idx = sorted(q for q in qs if q in runner_mod.TPC_BASE) if qs is not None else None
            allowed[runner_mod.TPC_ID] = {"trace": idx}
            tpc_saved = (LOCAL_MS.get(tpc_n, ms[tpc_n]), _class_of.get(tpc_n))
            nq = len(idx) if idx is not None else len(runner_mod.TPC_BASE) - 1
            LOCAL_MS[tpc_n] = TPC_TRACE_BASE_MS + TPC_TRACE_Q_MS * nq
            _class_of[tpc_n] = "<tpc trace>"
            PROXIED.add(tpc_n)
            res.notes.append(f"tpc-ds trace check on {nq} queries")
        forced = bits_to_nums(res.unverified, map_ids, num_by_id) & cands if res.unverified else set()
        if forced:
            cands -= forced
            res.notes.append(f"{len(forced)} tests listed unverified (empty-registry path)")
        gm_n = num_by_id.get(runner_mod.GEN_MEM_ID)
        gm_saved = None
        if GEN_MEM_SUBSET and gm_n is not None and gm_n in cands:
            mods = _dialect_specific_modules(res.patched)
            if mods:
                allowed[runner_mod.GEN_MEM_ID] = {"mods": mods}
                gm_saved = LOCAL_MS.get(gm_n, ms[gm_n])
                LOCAL_MS[gm_n] = 600
                PROXIED.add(gm_n)
                res.notes.append(f"generator memory check on dialects of {len(mods)} module(s)")
        cond: list[int] = []
        # Hot-swappable commits that leave the executor and tests alone reuse the
        # parent's TPC tables: TestExecutor's class setup is nearly free there.
        saved_setup = CLASS_SETUP_MS.get(EXEC_CLS)
        if PREWARM_PLAN and saved_setup is not None and HOT and not res.import_fp and not any(
                p.startswith(("tests/", "sqlglot/executor/")) for p in res.patched):
            code = {p: t for p, t in res.patched.items()}
            if code and hotswap.plan(REPO, code) is not None:
                CLASS_SETUP_MS[EXEC_CLS] = PREWARM_SETUP_MS
        if EQUIV and res.equiv and cands:
            # Swap with no semantic effect: probe with cheap candidates (strong first);
            # list the rest only if a probe fails.
            order = sorted((n for n in cands if n in strong), key=lambda n: LOCAL_MS.get(n, ms[n]))
            probe, budget, classes = [], EQUIV_PROBE_MS, set()
            for n in order:
                c = LOCAL_MS.get(n, ms[n])
                k = _class_of.get(n, "")
                if k not in classes:
                    c += CLASS_SETUP_MS.get(k, 0)
                if c <= budget:
                    probe.append(n)
                    budget -= c
                    classes.add(k)
            cond = [n for n in cands if n not in set(probe)]
            inc, loc = [], probe
            res.notes.append(f"equiv: probe {len(probe)}, conditional {len(cond)}")
        else:
            pmap = None
            if P_BY_SIZE:
                files = bits_to_nums(res.file_readers, map_ids, num_by_id) & cands
                st_ = (strong - files) & cands
                md_ = (medium - strong - files) & cands
                pmap = {}
                groups = [] if P_ANY else None
                for kind, S in (("medium", md_), ("strong", st_), ("file", files)):
                    for n in S:
                        pv = p_of(kind, len(S))
                        if P_SCALE_CHEAP and kind != "file" and ms[n] < HEAVY_LOCAL_MS:
                            pv = min(0.97, pv * float(P_SCALE_CHEAP) / P_SCALE)
                        pmap[n] = max(pmap.get(n, 0.0), pv)
                    if S and groups is not None:
                        groups.append((set(S), a_of(kind, len(S))))
            else:
                groups = None
            if pmap is not None and tpc_saved is not None and tpc_n in pmap:
                pmap[tpc_n] = min(pmap[tpc_n], TPC_P_CAP)
            inc, loc = plan(sorted(cands), ms, strong, medium, pmap, groups)
        if forced:
            inc = sorted(set(inc) | forced)
        if gm_saved is not None:
            LOCAL_MS[gm_n] = gm_saved
            PROXIED.discard(gm_n)
        LOCAL_MS.update(saved_local)
        if tpc_saved is not None:
            if tpc_n not in saved_local:
                LOCAL_MS[tpc_n] = tpc_saved[0]
            _class_of[tpc_n] = tpc_saved[1]
            PROXIED.discard(tpc_n)
        if saved_setup is not None:
            CLASS_SETUP_MS[EXEC_CLS] = saved_setup
        allowed = {t: (q if isinstance(q, dict) else sorted(q)) for t, q in allowed.items() if num_by_id[t] in set(loc)}
        decisions[cid] = {"include": inc, "local": loc, "patched": res.patched, "cond": cond}
        log(f"{cid}: cands={len(cands)} include={len(inc)} ({sum(ms[x] for x in inc)} ms) "
            f"local={len(loc)} ({sum(ms[x] for x in loc)} ms) {'; '.join(res.notes)[:200]}")
        return [id_by_num[x] for x in loc], allowed

    map_idx = {t: i for i, t in enumerate(map_ids)}

    def hot_plan(cid, tids):
        """Code swaps for a function-body-only patch (tests/ files come from the farm), else None."""
        r = analyses[cid]
        if not HOT or r.broad or r.import_fp:
            return None
        code = {p: t for p, t in r.patched.items() if not p.startswith("tests/")}
        if not code:
            return []
        swaps = hotswap.plan(REPO, code, r.trees)
        r.trees = {}
        if swaps is None:
            return None
        for p in code:
            readers = an.opens(p)
            if readers and any(t in map_idx and readers >> map_idx[t] & 1 for t in tids):
                return None  # a local test reads this source text
        return swaps

    # Children fork from here: move everything the parent holds out of the collector's
    # reach so they neither rescan nor copy it.
    import gc
    gc.collect()
    gc.freeze()
    t_start = time.monotonic()
    results: dict[str, dict[str, bool]] = {}
    fp: dict = {}
    retry: list = []
    # Offline tuning: BR_FP_CACHE replays fingerprint verdicts; BR_ORACLE answers
    # local runs from labels and estimates their CPU (no tests run).
    fp_cache = os.environ.get("BR_FP_CACHE")
    oracle = os.environ.get("BR_ORACLE")
    if fp_cache and os.path.exists(fp_cache) and oracle:
        fp = json.loads(Path(fp_cache).read_text())
        fp_jobs_run = []
    else:
        fp_jobs_run = fp_jobs
    if fp_jobs_run and os.path.exists(FP_BASE):
        preload()

        def on_fp(c, changed):
            apply_fp(c, changed)
            if LAZY_CHECK and runner_mod.LAZY_RES.get(c) == 1:
                # The fresh-interpreter test sees exactly what the fp child just saw.
                r = analyses[c]
                keep = ~an.m["proc_tests"]
                r.tests &= keep
                r.import_heur &= keep
                r.strong &= keep
                r.medium &= keep
                r.notes.append("lazy-load check passed in fingerprint child")
            return decide(c)

        fp, results, retry = fp_then_tests(REPO, TMP, FP_BASE, fp_jobs_run, on_fp, sec_by_id,
                                           parallel=2, budget_s=LOCAL_BUDGET_S, log=log)
        if fp_cache:
            Path(fp_cache).write_text(json.dumps(fp))
    fp_set = {c for c, _ in fp_jobs}
    for c in fp_set:
        if c in decisions:
            continue  # decided inside the combined fingerprint/test run
        apply_fp(c, fp.get(c, ["<no verdict>"]))
    for p in patches:
        cid = p.stem
        if cid in decisions:
            continue
        tids, allowed = decide(cid)
        if tids:
            jobs.append((cid, analyses[cid].patched, tids, allowed, hot_plan(cid, tids)))
    for c, patched, tids, allowed in retry:
        jobs.append((c, patched, tids, allowed, None))
    est_cpu = 0.0
    if jobs and oracle:
        lab = json.loads(Path(oracle).read_text())
        lab = lab.get("commits", lab)
        early = float(os.environ.get("BR_EARLY_STOP_S", "0"))
        for c, _, tids, allowed, *_ in jobs:
            f = lab[c]["failing"] if isinstance(lab[c], dict) else lab[c]
            fs = {id_by_num[n] for n in f}
            results[c] = {}
            ran = []
            failed = False
            for t in tids:
                if failed and early and sec_by_id[t] < early:
                    results[c][t] = True  # listed without running
                    continue
                results[c][t] = t in fs
                ran.append(t)
                failed = failed or t in fs
            est_cpu += local_cost([num_by_id[t] for t in ran], ms) / 1000.0
        log(f"oracle local cpu estimate {est_cpu:.1f} s over {len(jobs)} jobs")
        jobs = []
    if jobs:
        preload()
        if any(j[4] is not None for j in jobs):
            if not warm_import(REPO):
                jobs = [j[:4] + (None,) for j in jobs]
            # Worth it (~1.6 s once) when two or more children would each rebuild the tables.
            if PREWARM and sum(1 for j in jobs if j[4] is not None and any(t.startswith(EXEC_CLS + "::") for t in j[2])
                               and not any(p.startswith(("tests/", "sqlglot/executor/")) for p in j[1])) >= 2:
                prewarm_executor(REPO)
            gc.collect()
            gc.freeze()
        runner = LocalRunner(REPO, TMP, sec_by_id, log=log)
        left = max(5.0, LOCAL_BUDGET_S - (time.monotonic() - t_start))
        more = runner.run_many(jobs, parallel=2, budget_s=left)
        for c, r in more.items():
            results.setdefault(c, {}).update(r)

    sec_by_num = {t["number"]: t["seconds"] for t in tests}

    def insurance(cid, chosen, d, r):
        """Tests worth adding for free: candidates verified locally (in case a local
        verdict was wrong), then tests that ran any line of a changed file."""
        out = [x for x in d.get("local", []) if x not in chosen and not r.get(id_by_num[x], True)]
        res = analyses.get(cid)
        bits = 0
        if res is not None:
            for path in res.patched:
                if path.startswith("sqlglot/"):
                    bits |= an.cov_file(path)
                elif path.startswith("tests/"):
                    bits |= an.m["file_tests"].get(path, 0) | an.opens(path)
        seen = set(out) | set(chosen)
        extra = sorted((n for n in bits_to_nums(bits, map_ids, num_by_id) if n not in seen),
                       key=lambda n: sec_by_num[n])
        return out + extra

    def fill_slack(workers, extra):
        """Add tests to workers below the busiest one without raising its time."""
        loads = [sum(sec_by_num[n] for n in w) for w in workers]
        top = max(loads) - 1e-6
        workers = [list(w) for w in workers]
        for n in extra:
            t = sec_by_num[n]
            i = min(range(len(workers)), key=lambda j: loads[j])
            if loads[i] + t <= top:
                workers[i].append(n)
                loads[i] += t
        return workers

    total = 0
    for p in patches:
        cid = p.stem
        d = decisions[cid]
        chosen = set(d["include"])
        r = results.get(cid, {})
        if os.environ.get("BR_DEBUG"):
            log(f"{cid}: local results {json.dumps({id_by_num[x]: r.get(id_by_num[x]) for x in d['local']})}")
        probe_failed = False
        for x in d["local"]:
            tid = id_by_num[x]
            if r.get(tid, True):  # failed, or never finished: include
                chosen.add(x)
                probe_failed = True
        if probe_failed and d.get("cond"):
            chosen.update(d["cond"])
            log(f"{cid}: equiv probe failed; listing {len(d['cond'])} conditional tests")
        cost, workers = pack(sorted(chosen), ms)
        if FILL_SLACK and workers and not d.get("broad"):
            workers = fill_slack(workers, insurance(cid, chosen, d, r))
        total += cost
        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
    sys.stdout.flush()
    import resource
    ru_s, ru_c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = ru_s.ru_utime + ru_s.ru_stime + ru_c.ru_utime + ru_c.ru_stime
    log(f"done: {len(patches)} commits, packed cost {total / 1000:.1f} s, cpu {cpu:.2f} s")


if __name__ == "__main__":
    main()
    # Skip interpreter teardown (freeing the maps and every module costs CPU and
    # changes nothing): flush and exit.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
