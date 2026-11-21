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
   - anything unanalyzable (new/deleted/renamed files, syntax errors) → all tests
3. **Plan** (`selector/main.py`): expensive candidates are listed directly; cheap
   ones are run locally and only those that fail are listed; mid-cost ones ride
   free in worker slots a heavy test already pays for.
4. **Local runs** (`selector/runner.py`): a child per commit on a symlink farm in
   `/tmp` with the patched files, plain `unittest`, per-test time limits (a
   timeout counts as a failure), and a global time budget (unfinished tests are
   listed).
5. **Packing**: LPT over 1–8 workers, minimising busiest worker + 2 s per worker.

## Develop

```sh
tar -xzf blast-radius-sample.tar.gz sample score.py Makefile   # kit files, not committed
make sample                                                     # build + score on the 30 samples
```

Maps: run `tools/map_plugin.py` under pytest at the pinned commit, then
`tools/compile_maps.py`. Eval sets: `tools/mutate.py` + `tools/label.py`.
