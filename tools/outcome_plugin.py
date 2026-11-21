"""pytest plugin: write per-test outcome (pass/fail) to $OUTCOME_FILE as JSON.

A test fails on any failed/errored phase, any failed subtest, or when its
file fails to collect (import error). Collection errors are recorded by file.
"""
import json
import os

_fail = set()
_seen = set()
_bad_files = set()


def pytest_runtest_logreport(report):
    nodeid = report.nodeid
    # Subtest reports carry the parent nodeid; any failure fails the parent.
    _seen.add(nodeid)
    if report.failed:
        _fail.add(nodeid)


def pytest_collectreport(report):
    if report.failed:
        _bad_files.add(report.nodeid.split("::")[0])


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("OUTCOME_FILE")
    if not path or hasattr(session.config, "workerinput"):
        return
    with open(path, "w") as f:
        json.dump({"failed": sorted(_fail), "seen": sorted(_seen),
                   "bad_files": sorted(_bad_files)}, f)
