"""Add the empty-registry line trace (regempty_plugin output) to maps.pkl.
usage: add_reg_maps.py MAPS_IN MAPS_OUT REG_DIR
Adds regempty: {path: {line: set id}} for lines whose empty-registry runs differ
from what the ordinary map records (all lines kept; the analyzer uses them as
'cannot verify in a full-registry child')."""
import glob, json, os, pickle, sys
mi, mo, d = sys.argv[1:4]
m = pickle.load(open(mi, "rb")); ids = m["tests"]; idx = {t: i for i, t in enumerate(ids)}
sets = m["sets"]; acc = {}
for f in glob.glob(os.path.join(d, "t", "*.json")):
    r = json.load(open(f)); i = idx.get(r["nodeid"])
    if i is None:
        continue
    for path, lines in r["lines"].items():
        for ln in lines:
            acc.setdefault(path, {}).setdefault(ln, 0)
            acc[path][ln] |= 1 << i
out = {}
for path, lm in acc.items():
    out[path] = {}
    for ln, b in lm.items():
        out[path][ln] = len(sets); sets.append(b)
m["regempty"] = out
print({p: len(v) for p, v in out.items()}, file=sys.stderr)
pickle.dump(m, open(mo, "wb"), protocol=5)
