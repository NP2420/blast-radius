"""Merge comparison-profile runs (cmp_plugin output dirs) into maps.pkl.

usage: add_cmp_maps.py MAPS_PKL OUT_PKL CMP_DIR [CMP_DIR ...]
Adds: cmp_sites (set of instrumented sites), cmp ({site: {alt: set id}}),
cmp_unknown ({site: set id}), cmp_untrusted (set id of tests that failed or
were missing under instrumentation in any run).
"""
import glob
import json
import os
import pickle
import sys

maps_in, maps_out, dirs = sys.argv[1], sys.argv[2], sys.argv[3:]
with open(maps_in, "rb") as f:
    m = pickle.load(f)
ids = m["tests"]
idx = {t: i for i, t in enumerate(ids)}
sets = m["sets"]
sid_of = {}


def sid(bits):
    if bits not in sid_of:
        sid_of[bits] = len(sets)
        sets.append(bits)
    return sid_of[bits]


sites = None
diff: dict = {}
unknown: dict = {}
seen_all = None
untrusted = 0
for d in dirs:
    s = set()
    for f in glob.glob(os.path.join(d, "sites.*.json")):
        s |= set(json.load(open(f)))
    sites = s if sites is None else (sites & s)
    seen = 0
    for f in glob.glob(os.path.join(d, "t", "*.json")):
        r = json.load(open(f))
        i = idx.get(r["nodeid"])
        if i is None:
            continue
        b = 1 << i
        seen |= b
        if r["failed"]:
            untrusted |= b
        for site, alt in r["diff"]:
            diff.setdefault(site, {}).setdefault(alt, 0)
            diff[site][alt] |= b
        for site in r["unknown"]:
            unknown[site] = unknown.get(site, 0) | b
    seen_all = seen if seen_all is None else (seen_all & seen)
    print(d, "tests", bin(seen).count("1"), "sites", len(s), file=sys.stderr)
# Tests missing from any run are untrusted (their profile is incomplete).
untrusted |= ((1 << len(ids)) - 1) & ~seen_all
m["cmp_sites"] = sites or set()
m["cmp"] = {s: {a: sid(b) for a, b in alts.items()} for s, alts in diff.items()}
m["cmp_unknown"] = {s: sid(b) for s, b in unknown.items()}
m["cmp_untrusted"] = sid(untrusted)
print("sites", len(m["cmp_sites"]), "with diffs", len(m["cmp"]), "unknown", len(m["cmp_unknown"]),
      "untrusted tests", bin(untrusted).count("1"), file=sys.stderr)
with open(maps_out, "wb") as f:
    pickle.dump(m, f, protocol=5)
