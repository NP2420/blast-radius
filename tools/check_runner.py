"""Does the local runner agree with pytest labels?

usage: check_runner.py PATCH_DIR LABELS_JSON [MAX_COMMITS]
For each labeled commit, runs (failing tests + up to 5 cheap passing candidates)
through LocalRunner and reports any disagreement. A "pass" where the label says
"fail" would be a miss in production.
"""
import json
import os
import pickle
import random
import sys

sys.path.insert(0, "/home/claude/dev/br/selector")
import analyze  # noqa: E402
import patchparse  # noqa: E402
from runner import LocalRunner, preload  # noqa: E402

pdir, labels_path = sys.argv[1], sys.argv[2]
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 10**9
labels = json.load(open(labels_path))["commits"]
tests = json.load(open("/home/claude/kits/br/sample/tests.json"))["tests"]
num = {t["number"]: t["id"] for t in tests}
sec = {t["id"]: t["seconds"] for t in tests}
m = pickle.load(open("/home/claude/dev/br/maps/maps.pkl", "rb"))
an = analyze.Analyzer(m, "/home/claude/dev/sqlglot")
rng = random.Random(1)
jobs, expect = [], {}
for cid, lab in sorted(labels.items())[:limit]:
    if lab.get("failing") is None:
        continue
    fds = patchparse.parse(open(os.path.join(pdir, cid + ".patch")).read())
    res = an.analyze(fds)
    if res.broad:
        continue
    fail = {num[n] for n in lab["failing"]}
    cands = {m["tests"][i] for i in range(len(m["tests"])) if (res.tests | res.import_heur) >> i & 1}
    passing = sorted(t for t in cands - fail if sec.get(t, 9) < 0.5)
    pick = sorted(t for t in fail if sec.get(t, 9) < 5) + rng.sample(passing, min(5, len(passing)))
    if pick:
        jobs.append((cid, res.patched, pick))
        expect[cid] = fail
preload()
r = LocalRunner("/home/claude/dev/sqlglot", "/tmp", sec).run_many(jobs, parallel=2)
bad_miss, bad_extra, n = [], [], 0
for cid, _, pick in jobs:
    for t in pick:
        n += 1
        got = r[cid].get(t, True)
        want = t in expect[cid]
        if want and not got:
            bad_miss.append((cid, t))
        elif got and not want:
            bad_extra.append((cid, t))
print(f"checked {n} test runs over {len(jobs)} commits")
print("LOCAL PASS but LABEL FAIL (would miss):", bad_miss)
print("local fail but label pass (costs a little):", bad_extra)
