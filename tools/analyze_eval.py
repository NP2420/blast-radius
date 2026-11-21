"""Candidate-set quality on a labeled patch set: misses, precision by evidence type.

usage: analyze_eval.py PATCH_DIR LABELS_JSON [TESTS_JSON]
"""
import json, os, pickle, sys, collections
sys.path.insert(0, "/home/claude/dev/br/selector")
import analyze, patchparse
pdir, labels = sys.argv[1], json.load(open(sys.argv[2]))["commits"]
tests = json.load(open(sys.argv[3] if len(sys.argv) > 3 else "/home/claude/kits/br/sample/tests.json"))["tests"]
ms = {t["id"]: t["seconds"] for t in tests}; num = {t["number"]: t["id"] for t in tests}
m = pickle.load(open("/home/claude/dev/br/maps/maps.pkl", "rb")); ids = m["tests"]
an = analyze.Analyzer(m, "/home/claude/dev/sqlglot")
kinds = {}
kp = os.path.join(pdir, "kinds.json")
if os.path.exists(kp): kinds = json.load(open(kp))
st = collections.Counter(); misses = []
def bits(b): return {ids[i] for i in range(len(ids)) if b >> i & 1}
for cid, lab in sorted(labels.items()):
    if lab.get("failing") is None: continue
    fail = {num[n] for n in lab["failing"]}
    res = an.analyze(patchparse.parse(open(os.path.join(pdir, cid + ".patch")).read()))
    if res.broad:
        st["broad"] += 1; continue
    c = bits(res.tests); s = bits(res.strong); w = c - s
    miss = fail - c
    if miss: misses.append((cid, kinds.get(cid, "?"), sorted(miss)[:4], res.notes[:2]))
    st["commits"] += 1; st["fail_commits"] += bool(fail)
    st["strong"] += len(s); st["strong_fail"] += len(s & fail)
    st["weak"] += len(w); st["weak_fail"] += len(w & fail)
    st["strong_commits"] += bool(s); st["strong_commits_fail"] += bool(s & fail)
    st["weakonly_commits"] += bool(w and not s); st["weakonly_commits_fail"] += bool((w and not s) and (w & fail))
print(dict(st))
print("p(strong test fails)=%.3f p(weak test fails)=%.4f" % (st["strong_fail"] / max(1, st["strong"]), st["weak_fail"] / max(1, st["weak"])))
print("MISSES", len(misses))
for x in misses: print("  ", x)
