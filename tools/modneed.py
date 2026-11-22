"""Which sqlglot modules each test needs: what its file imports plus what it loads.

usage: modneed.py REPO MAPS OUT.json [JOBS]
A parent imports only `sqlglot` (what every test process has). Each test then runs in
a forked child that imports its test module and runs it; the child records every
sqlglot module that appears in sys.modules beyond the parent's. If one of those
modules cannot be imported, the test cannot pass (its file fails to collect, or the
test fails where it loads the module).
"""
import json, os, pickle, sys, time

repo, maps_p, out_p = sys.argv[1:4]
jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
ids = pickle.load(open(maps_p, "rb"))["tests"]
os.chdir(repo)
sys.path.insert(0, repo)
os.environ["SKIP_INTEGRATION"] = "0"
import importlib, unittest  # noqa: E402
import sqlglot  # noqa: E402,F401

BASE = set(sys.modules)


def run_one(tid):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            dn = os.open(os.devnull, os.O_RDWR)
            os.dup2(dn, 1); os.dup2(dn, 2)
            file, _, rest = tid.partition("::")
            cls, _, meth = rest.partition("::")
            need = set()
            try:
                mod = importlib.import_module(file[:-3].replace("/", "."))
                tc = getattr(mod, cls)(meth)
                unittest.TestSuite([tc]).run(unittest.TestResult())
            except BaseException:
                pass
            need = sorted(m for m in sys.modules if m not in BASE and m.split(".")[0] == "sqlglot")
            os.write(w, json.dumps(need).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    deadline = time.time() + 300
    import select
    while True:
        rl, _, _ = select.select([r], [], [], 5)
        if rl:
            chunk = os.read(r, 1 << 16)
            if not chunk:
                break
            data += chunk
        elif time.time() > deadline:
            os.kill(pid, 9)
            break
    os.close(r)
    os.waitpid(pid, 0)
    try:
        return json.loads(data)
    except ValueError:
        return None  # unknown: treat as needing everything


part = int(os.environ.get("PART", "0"))
mine = [t for i, t in enumerate(ids) if i % jobs == part]
out = {}
for i, t in enumerate(mine):
    out[t] = run_one(t)
    if i % 100 == 0:
        print(part, i, len(mine), file=sys.stderr, flush=True)
json.dump(out, open(f"{out_p}.{part}", "w"))
