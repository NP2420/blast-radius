"""Label patches by running the full sqlglot suite on each.

usage: label.py TESTS_JSON OUT_JSON PATCH [PATCH ...]
Output: {"commits": {id: {"failing": [numbers], "apply_ok": bool}}}
Resumable: commits already in OUT_JSON are skipped.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEV = Path(os.environ.get("LABEL_DEV", "/home/claude/dev"))
SRC = Path(os.environ.get("LABEL_SRC", str(DEV / "sqlglot")))
PY = Path(os.environ.get("LABEL_PY", str(DEV / "venv/bin/python")))
TOOLS = Path(os.environ.get("LABEL_TOOLS", str(DEV / "tools")))
JOBS = os.environ.get("LABEL_JOBS", "2")


def run_suite(work: Path, outcome: Path, jobs: str = JOBS) -> dict:
    env = dict(os.environ, SKIP_INTEGRATION="0", OUTCOME_FILE=str(outcome),
               PYTHONPATH=f"{work}:{TOOLS}", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([str(PY), "-m", "pytest", "tests", "-p", "outcome_plugin",
                    "-p", "no:cacheprovider", "-q", "--no-header", "-n", jobs],
                   cwd=work, env=env, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=1800)
    return json.loads(outcome.read_text())


def main() -> None:
    tests = json.loads(Path(sys.argv[1]).read_text())["tests"]
    by_id = {t["id"]: t["number"] for t in tests}
    by_file: dict[str, list[int]] = {}
    for t in tests:
        by_file.setdefault(t["file"], []).append(t["number"])
    out_path = Path(sys.argv[2])
    out = json.loads(out_path.read_text()) if out_path.exists() else {"commits": {}}
    tmp = Path(tempfile.mkdtemp(prefix="label_"))
    work = tmp / "repo"
    shutil.copytree(SRC, work, ignore=shutil.ignore_patterns(".git", "__pycache__",
                                                              "sqlglot-integration-tests"),
                    symlinks=True)
    pristine = tmp / "pristine"
    shutil.copytree(work, pristine)
    for p in sys.argv[3:]:
        cid = Path(p).stem
        if cid in out["commits"]:
            continue
        shutil.rmtree(work)
        shutil.copytree(pristine, work)
        r = subprocess.run(["patch", "-p1", "-s", "-f", "-i", str(Path(p).resolve())],
                           cwd=work, capture_output=True, text=True)
        if r.returncode != 0:
            out["commits"][cid] = {"failing": None, "apply_ok": False, "err": r.stdout + r.stderr}
            print(cid, "APPLY FAILED", file=sys.stderr)
        else:
            d = run_suite(work, tmp / "outcome.json")
            fail = {by_id[n] for n in d["failed"] if n in by_id}
            for f in d["bad_files"]:
                fail.update(by_file.get(f, []))
            seen = {by_id[n] for n in d["seen"] if n in by_id}
            # A test that never reported (crash/abort) counts as failing.
            fail.update(set(by_id.values()) - seen - fail)
            out["commits"][cid] = {"failing": sorted(fail), "apply_ok": True}
            print(cid, len(fail), file=sys.stderr, flush=True)
        out_path.write_text(json.dumps(out))
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
