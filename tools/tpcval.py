"""Check the TPC trace proxy against labels.

usage: tpcval.py OUT.json BASE.pkl DATA_DIR [DATA_DIR ...]
For each labeled commit (any data dir) that touches sqlglot/ but not the executor or
the TPC fixtures, trace the patched repo and compare with the pristine traces.
Records per commit: tpcds failing (label), query indices whose trace differs.
"""
import json, os, pickle, shutil, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "/home/claude/dev/br/tools")
sys.path.insert(0, "/home/claude/dev/br/selector")
import label  # noqa: E402

SRC = Path("/home/claude/dev/sqlglot")
PY = "/home/claude/dev/venv/bin/python"
TID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
out_path, base_path = sys.argv[1], sys.argv[2]
base = pickle.load(open(base_path, "rb"))[TID]
out = json.load(open(out_path)) if os.path.exists(out_path) else {}
jobs = []
KEYS = set(open(os.environ["KEYS"]).read().split()) if os.environ.get("KEYS") else None
for d in sys.argv[3:]:
    tests = json.load(open(f"{d}/tests.json"))["tests"]
    num = {t["id"]: t["number"] for t in tests}[TID]
    for cid, v in json.load(open(f"{d}/answers.json"))["commits"].items():
        key = f"{os.path.basename(d)}/{cid}"
        if key in out:
            continue
        if KEYS is not None and key not in KEYS and not (num in v["failing"]):
            continue
        txt = open(f"{d}/commits/{cid}.patch", errors="replace").read()
        jobs.append((key, txt, num in v["failing"]))

SCRIPT = r'''
import sys, pickle, hashlib
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, "/home/claude/dev/br/selector")
import os; os.chdir(sys.argv[1])
import tpctrace
t = tpctrace.trace_all(sys.argv[1], only=sys.argv[3])[sys.argv[3]]
pickle.dump(t, open(sys.argv[2], "wb"))
'''


def run(job):
    key, txt, fails = job
    tmp = Path(tempfile.mkdtemp(prefix="tpcval_"))
    try:
        work = tmp / "repo"
        shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "sqlglot-integration-tests"), symlinks=True)
        err = label.apply_patch(work, txt)
        if err:
            return key, {"fails": fails, "err": err}
        r = subprocess.run([PY, "-c", SCRIPT, str(work), str(tmp / "t.pkl"), TID], capture_output=True, text=True, timeout=600,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        if r.returncode != 0:
            return key, {"fails": fails, "crash": r.stderr[-300:]}
        t = pickle.load(open(tmp / "t.pkl", "rb"))
        diff = sorted(i for i in base if t.get(i) != base[i])
        return key, {"fails": fails, "diff": diff}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


with ThreadPoolExecutor(int(os.environ.get("JOBS", "2"))) as ex:
    for i, (key, res) in enumerate(ex.map(run, jobs)):
        out[key] = res
        if i % 10 == 0:
            json.dump(out, open(out_path, "w"))
        if res.get("fails") and not res.get("diff"):
            print("UNCAUGHT", key, res, flush=True)
json.dump(out, open(out_path, "w"))
print("done", len(out))
