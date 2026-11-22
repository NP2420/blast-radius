# Blast Radius selector

For each patch to sqlglot, pick exactly the tests that can fail.

## How it works

1. **Dependency maps** (`maps/maps.pkl`, built from the pinned commit by `tools/`):
   each test runs in its own forked child after all sqlglot modules are imported,
   so every test's coverage is complete (no cache from an earlier test hides a
   dependency). Recorded per test: line coverage, files opened, subprocesses, and
   receiver classes (the sqlglot MRO of every `self`/`cls` in a call). Process
   pools are run in-process so their work is covered. Import-time lines and
   per-test-file collection lines are recorded separately.
2. **Analysis** (`selector/analyze.py`): the patch is applied in memory and each
   changed line is resolved to its innermost statement and scope:
   - function body → tests that executed that statement
   - function code that also runs during sqlglot's import → every test
   - class body → tests using the class (receivers) or `Owner.ATTR` references
   - module level → tests that executed the module or use its names
   - comment/whitespace only (AST unchanged) → only tests that read the file as text
   - data files (fixtures) → tests that opened them
   - anything unanalyzable (e.g. renamed package files) → all tests
3. **Plan** (`selector/main.py`): expensive candidates are listed directly; cheap
   ones are run locally and only those that fail are listed; mid-cost ones ride
   free in worker slots a heavy test already pays for.
4. **Local runs** (`selector/runner.py`): a child per commit on a symlink farm in
   `/tmp` with the patched files, plain `unittest`, per-test time limits (a
   timeout counts as a failure), and a global time budget (unfinished tests are
   listed).
5. **Packing**: LPT over 1–8 workers, minimising busiest worker + 2 s per worker.

Later additions:

- **Import-state fingerprint** (`selector/fingerprint.py`): a child imports the
  patched tree and digests every class attribute and module global; changed keys
  map to the tests that read them. Modules that fail to import are reported too.
- **Hot swap** (`selector/hotswap.py`): body-only patches swap `__code__` in a warm
  parent instead of re-importing sqlglot.
- **Operator-swap value profiles** (`tools/cmp_plugin.py`): a swapped comparison or
  and/or is charged only to tests that saw a different result at that site.
- **Owner-level table keys** (`tools/key_plugin2.py`): an edited table entry is
  charged to tests that looked that key up in the class's own table, its
  subclasses' or copies, not in every same-name table.
- **Unimportable modules** (`tools/modneed.py`, `tools/add_broken_maps.py`): a
  syntax error, import-time error or deleted module lists the tests that load it.
- **TPC-DS** (21 s): queries are pruned per changed line or per fixture query;
  sqlglot-side changes are checked by data-free traces (`selector/tpctrace.py`),
  executor-only changes by rerunning the executor and comparing result tables with
  the pinned ones (`selector/tpcexec.py`); per-query CPU caps stop a crawling query.
- **Run-everything guards**: pytest/interpreter config (`conftest.py`, `pytest.ini`,
  pytest sections of `pyproject.toml`/`setup.cfg`), test-module import-time code,
  memoizing decorators (local verdicts untrusted).

## Develop

```sh
tar -xzf blast-radius-sample.tar.gz sample score.py Makefile   # kit files, not committed
make sample                                                     # build + score on the 30 samples
```

Maps: run `tools/map_plugin.py` under pytest at the pinned commit, then
`tools/compile_maps.py`. Eval sets: `tools/mutate.py` + `tools/label.py`.
