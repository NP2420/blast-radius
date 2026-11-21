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
from runner import LocalRunner, fp_then_tests, preload  # noqa: E402

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
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.5"))
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


CLASS_SETUP_MS = {"tests/test_executor.py::TestExecutor": 1200}  # replaced from maps when present
LOCAL_MS: dict[int, int] = {}  # measured local CPU per test number (falls back to recorded)
_class_of: dict[int, str] = {}


def local_cost(nums, ms) -> float:
    if not nums:
        return 0.0
    setup = sum(CLASS_SETUP_MS.get(c, 0) for c in {_class_of.get(n, "") for n in nums})
    return LOCAL_OVERHEAD_MS + setup + CPU_FACTOR * sum(LOCAL_MS.get(n, ms[n]) for n in nums)


P_STRONG = float(os.environ.get("BR_P_STRONG", "0.30"))
P_WEAK = float(os.environ.get("BR_P_WEAK", "0.01"))
P_MEDIUM = float(os.environ.get("BR_P_MEDIUM", "0.15"))
# Failure rates by evidence kind and how many tests share it (calibrated on labeled commits):
# a lone test reading a changed fixture or executing a changed line usually fails.
P_BY_SIZE = os.environ.get("BR_P_BY_SIZE", "1") == "1"


P_SCALE = float(os.environ.get("BR_P_SCALE", "0.6"))


P_SCALE_FILE = os.environ.get("BR_P_SCALE_FILE", "1.0")


def p_of(kind: str, n: int) -> float:
    sc = float(P_SCALE_FILE) if (kind == "file" and P_SCALE_FILE) else P_SCALE
    return min(0.97, sc * _p_of(kind, n))


def _p_of(kind: str, n: int) -> float:
    if kind == "file":
        return 0.85 if n <= 3 else 0.3 if n <= 10 else 0.05
    if kind == "strong":
        return 0.8 if n == 1 else 0.5 if n <= 10 else 0.38 if n <= 50 else 0.08
    if kind == "medium":
        return 0.35 if n <= 3 else 0.27 if n <= 10 else 0.05
    return P_WEAK


def plan(cands: list[int], ms: dict[int, int], strong: set | None = None, medium: set | None = None,
         pmap: dict | None = None) -> tuple[list[int], list[int]]:
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
    st = sorted((n for n in cands if n in strong), key=lambda n: -ms[n])
    wk = sorted((n for n in cands if n not in strong), key=lambda n: -ms[n])

    def expected(inc, loc):
        c = pack_cost(inc, ms) + local_cost(loc, ms)
        if loc:
            ef = sum(p[n] * ms[n] for n in loc)
            if not inc:
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
    map_ids = maps["tests"]
    if "local_ms" in maps:
        for tid, v in maps["local_ms"].items():
            if tid in num_by_id:
                LOCAL_MS[num_by_id[tid]] = v
        CLASS_SETUP_MS.clear()
        CLASS_SETUP_MS.update(maps["class_setup"])
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
        if r.key_attrs and not any(k.startswith("<") or k.startswith("global:") for k in changed):
            # The edited table (and same-name copies in subclasses) must differ;
            # anything else that changed is a ripple to account for.
            changed = [k for k in changed if k.rsplit(".", 1)[-1].rsplit(":", 1)[-1] not in r.key_attrs]
        if not changed:
            r.notes.append("import state unchanged")
            return
        users = 0
        for key in changed:
            u = an.state_key_users(key)
            if u is None or len(changed) >= 50:
                users = an.all_bits
                break
            users |= u
        r.tests |= users
        r.notes.append(f"import state changed ({len(changed)}): {changed[:3]}")

    def decide(cid):
        """Decide include/local/cond for one commit; returns (local test ids, allowed)."""
        res = analyses[cid]
        if res.broad:
            decisions[cid] = {"include": all_nums, "local": [], "broad": res.broad, "cond": []}
            log(f"{cid}: BROAD ({res.broad})")
            return [], {}
        cands = set(unmapped) | bits_to_nums(res.tests, map_ids, num_by_id)
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
        for tid in (maps.get("task_cov", {}) if TASK_PARTIAL else {}):
            n_ = num_by_id.get(tid)
            if n_ is None or n_ not in cands:
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
        cond: list[int] = []
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
                for kind, S in (("medium", md_), ("strong", st_), ("file", files)):
                    for n in S:
                        pmap[n] = max(pmap.get(n, 0.0), p_of(kind, len(S)))
            inc, loc = plan(sorted(cands), ms, strong, medium, pmap)
        LOCAL_MS.update(saved_local)
        allowed = {t: sorted(q) for t, q in allowed.items() if num_by_id[t] in set(loc)}
        decisions[cid] = {"include": inc, "local": loc, "patched": res.patched, "cond": cond}
        log(f"{cid}: cands={len(cands)} include={len(inc)} ({sum(ms[x] for x in inc)} ms) "
            f"local={len(loc)} ({sum(ms[x] for x in loc)} ms) {'; '.join(res.notes)[:200]}")
        return [id_by_num[x] for x in loc], allowed

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
            jobs.append((cid, analyses[cid].patched, tids, allowed))
    for c, patched, tids, allowed in retry:
        jobs.append((c, patched, tids, allowed))
    est_cpu = 0.0
    if jobs and oracle:
        lab = json.loads(Path(oracle).read_text())
        lab = lab.get("commits", lab)
        early = float(os.environ.get("BR_EARLY_STOP_S", "0"))
        for c, _, tids, allowed in jobs:
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
        runner = LocalRunner(REPO, TMP, sec_by_id, log=log)
        left = max(5.0, LOCAL_BUDGET_S - (time.monotonic() - t_start))
        more = runner.run_many(jobs, parallel=2, budget_s=left)
        for c, r in more.items():
            results.setdefault(c, {}).update(r)

    total = 0
    for p in patches:
        cid = p.stem
        d = decisions[cid]
        chosen = set(d["include"])
        r = results.get(cid, {})
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
        total += cost
        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
    sys.stdout.flush()
    import resource
    ru_s, ru_c = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = ru_s.ru_utime + ru_s.ru_stime + ru_c.ru_utime + ru_c.ru_stime
    log(f"done: {len(patches)} commits, packed cost {total / 1000:.1f} s, cpu {cpu:.2f} s")


if __name__ == "__main__":
    main()
