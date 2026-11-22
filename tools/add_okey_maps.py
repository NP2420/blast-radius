"""Add owner-level key users (from key_plugin2 output) to maps.pkl.

usage: add_okey_maps.py MAPS_IN KEYS_DIR MAPS_OUT
maps["okey_users"]: "module:Qual|ATTR" -> {key: set id, or -1 for the attribute-level set} (tests that looked up key in a
dict reachable by an edit of that class's table); maps["okey_tags"]:
"module:Qual.ATTR" -> owners ("module:Qual|ATTR") whose edit can reach that table.
"""
import glob, json, os, pickle, sys

mi, kd, mo = sys.argv[1:4]
m = pickle.load(open(mi, "rb")); ids = m["tests"]; idx = {t: i for i, t in enumerate(ids)}
sets = m["sets"]; bits = {}; n = 0
for f in glob.glob(os.path.join(kd, "*.json")):
    d = json.load(open(f)); i = idx.get(d["nodeid"])
    if i is None:
        continue
    n += 1
    for k in d["keys"]:
        bits[k] = bits.get(k, 0) | (1 << i)
if n != len(ids):
    sys.exit(f"key data for {n} of {len(ids)} tests")
dedup = {b: i for i, b in enumerate(sets)}
n0 = len(sets)
users = {}
ku = m.get("key_users", {})
for k, b in bits.items():
    o, a, key = k.split("|", 2)
    base = ku.get(f"{a}|{key}")
    if base is not None and sets[base] == b:
        s = -1  # same as the attribute-level set
    else:
        s = dedup.get(b)
        if s is None:
            s = dedup[b] = len(sets); sets.append(b)
    users.setdefault(f"{o}|{a}", {})[key] = s
m["okey_users"] = users
m["okey_tags"] = json.load(open(os.path.join(kd, "_tags.json.meta")))
pickle.dump(m, open(mo, "wb"), protocol=5)
print(f"okey: {n} tests, {sum(map(len, users.values()))} keys, {len(sets) - n0} new sets, tags {len(m['okey_tags'])}")
