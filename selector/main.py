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
from runner import LocalRunner, preload  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
REPO = os.environ.get("BR_REPO", "/opt/sqlglot")
MAPS = os.environ.get("BR_MAPS", str(HERE.parent / "maps" / "maps.pkl"))
TMP = os.environ.get("BR_TMP", "/tmp")
WORKER_MS = 2000
LOCAL_OVERHEAD_MS = int(os.environ.get("BR_LOCAL_OVERHEAD_MS", "400"))
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.15"))
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


CLASS_SETUP_MS = {"tests/test_executor.py::TestExecutor": 1100}
_class_of: dict[int, str] = {}


def local_cost(nums, ms) -> float:
    if not nums:
        return 0.0
    setup = sum(CLASS_SETUP_MS.get(c, 0) for c in {_class_of.get(n, "") for n in nums})
    return LOCAL_OVERHEAD_MS + setup + CPU_FACTOR * sum(ms[n] for n in nums)


def plan(cands: list[int], ms: dict[int, int]) -> tuple[list[int], list[int]]:
    """Split candidates into (include directly, run locally)."""
    if not cands:
        return [], []
    if NO_LOCAL:
        return list(cands), []
    order = sorted(cands, key=lambda n: -ms[n])
    best = None
    suffix = [0] * (len(order) + 1)
    for i in range(len(order) - 1, -1, -1):
        suffix[i] = suffix[i + 1] + ms[order[i]]
    for j in range(len(order) + 1):
        inc = order[:j]
        cost = pack_cost(inc, ms) + local_cost(order[j:], ms)
        if best is None or cost < best[0]:
            best = (cost, j)
        if j > 12 and best[1] < j - 12:
            break  # costs only rise once the heavy head is decided
    j = best[1]
    inc, loc = order[:j], order[j:]
    # Free riders: a local test that does not raise the packed cost goes in directly.
    if inc:
        base = pack_cost(inc, ms)
        moved = []
        for n in loc:
            if pack_cost(inc + moved + [n], ms) <= base:
                moved.append(n)
        # Moving a class's last local tests also saves its setup cost.
        rest = [n for n in loc if n not in set(moved)]
        for c, setup in CLASS_SETUP_MS.items():
            cls_rest = [n for n in rest if _class_of.get(n) == c]
            if cls_rest and pack_cost(inc + moved + cls_rest, ms) - base <= setup + CPU_FACTOR * sum(ms[n] for n in cls_rest):
                moved.extend(cls_rest)
        if moved:
            ms_set = set(moved)
            inc = inc + moved
            loc = [n for n in loc if n not in ms_set]
    return inc, loc


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
    mapped = set(map_ids)
    unmapped = [num_by_id[i] for i in num_by_id if i not in mapped]
    if unmapped:
        log(f"warning: {len(unmapped)} tests without maps; always candidates")
    an = analyze.Analyzer(maps, REPO)

    patches = sorted((DATA / "commits").glob("*.patch"))
    decisions: dict[str, dict] = {}
    jobs = []
    for p in patches:
        cid = p.stem
        try:
            fds = patchparse.parse(p.read_text(encoding="utf-8", errors="replace"))
            if not fds:
                raise ValueError("empty patch")
            res = an.analyze(fds)
        except Exception:
            log(f"{cid}: analysis error\n{traceback.format_exc()}")
            res = analyze.Result(broad="analysis error")
        if res.broad:
            decisions[cid] = {"include": all_nums, "local": [], "broad": res.broad}
            log(f"{cid}: BROAD ({res.broad})")
            continue
        bits = res.tests
        cands = set(unmapped)
        n = 0
        while bits:
            if bits & 1:
                tid = map_ids[n]
                if tid in num_by_id:
                    cands.add(num_by_id[tid])
            bits >>= 1
            n += 1
        inc, loc = plan(sorted(cands), ms)
        decisions[cid] = {"include": inc, "local": loc, "patched": res.patched}
        log(f"{cid}: cands={len(cands)} include={len(inc)} ({sum(ms[x] for x in inc)} ms) "
            f"local={len(loc)} ({sum(ms[x] for x in loc)} ms) {'; '.join(res.notes)[:200]}")
        if loc:
            jobs.append((cid, res.patched, [id_by_num[x] for x in loc]))

    results: dict[str, dict[str, bool]] = {}
    if jobs:
        preload()
        runner = LocalRunner(REPO, TMP, sec_by_id, log=log)
        results = runner.run_many(jobs, parallel=2, budget_s=LOCAL_BUDGET_S)

    total = 0
    for p in patches:
        cid = p.stem
        d = decisions[cid]
        chosen = set(d["include"])
        r = results.get(cid, {})
        for x in d["local"]:
            tid = id_by_num[x]
            if r.get(tid, True):  # failed, or never finished: include
                chosen.add(x)
        cost, workers = pack(sorted(chosen), ms)
        total += cost
        sys.stdout.write(json.dumps({"commit": cid, "workers": workers}) + "\n")
    sys.stdout.flush()
    log(f"done: {len(patches)} commits, packed cost {total / 1000:.1f} s")


if __name__ == "__main__":
    main()
