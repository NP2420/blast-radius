# Submissions

| # | Commit | Time (ET) | Score | Sim s | CPU s | Misses | Notes |
|---|--------|-----------|-------|-------|-------|--------|-------|
| 1 | `2892ccec` | Fri 8:18 PM | 210.7 | 169.7 | 41.0 | 0 | v1 baseline; zero missed commits |
| 2 | `34c89256` | Fri 9:21 PM | 186.0 | 176.0 | 10.0 | 0 | v4: line-level attribute readers; CPU 41.0 → 10.0 s |
| 3 | `8671309d` | Fri 10:22 PM | 464.0 | 429.5 | 34.6 | 1 | v5: per-query tpcds undercount (fixed later) |
| 4 | `3b3e59d8` | Fri 11:23 PM | 192.2 | 168.1 | 24.0 | 0 | v7: key-level tables, compile check; CPU factor 1.0 too low |
| 5 | `14316fe3` | Sat 12:24 AM | 178.2 | 164.2 | 14.0 | 0 | v8: + skip weak-evidence heavy tests, CPU factor 1.5. Best so far |
| 6 | `a92f7557` | Sat 1:26 AM | 186.4 | 156.3 | 29.9 | 0 | v10: fingerprint ripple guard, fixed per-query tpcds, X off (safe). CPU from guards |
| 7 | `329c01f6` | Sat 2:27 AM | 199.2 | 171.1 | 28.1 | 0 | v11: module-level narrowing, loose fingerprint; simulated +14.8 vs v10, under investigation |
| 8 | `17313af7` | Sat 3:27 AM | 186.2 | 171.0 | 15.2 | 0 | v12c: fingerprint + tests in one process, failure odds by evidence size, cheaper module table sets. CPU halved; simulated regression from v11 remains |
| 9 | `35653616` | Sat 4:28 AM | 191.7 | 183.7 | 8.0 | 0 | v12e: failure odds x1.5, import edits in-file readers, faster analysis. CPU -7 but simulated +12.7: on the board, local runs pay off more than on our labels |
| 10 | `6558c4d0` | Sat 5:30 AM | 191.5 | 171.0 | 20.5 | 0 | v12f (A/B): odds x1.0, module narrowing for class headers only. Simulated identical to v12c, so module narrowing is not the regression |
| 11 | `28cdf6be` | Sat 6:30 AM | 185.2 | 145.6 | 39.5 | 0 | v12g: class-header edits narrowed to class users + referencing classes, odds x0.6. Simulated -25 vs v12f, CPU +19 |
| 12 | `20104064` | Sat 7:32 AM | 190.3 | 171.0 | 19.2 | 0 | v12h (A/B): v12g at odds x1.0. Header fix had no board effect; v12g's -25 s simulated came from more local runs |
| 13 | `1d041936` | Sat 8:33 AM | 190.8 | 171.0 | 19.7 | 0 | v12i: odds x0.75 (fixtures x1.0). The -25 s from x0.6 is one heavy test run locally; x0.75 lists it again |
| 14 | `a4a48072` | Sat 9:34 AM | 182.1 | 147.7 | 35.0 | 0 | v12j: odds x0.6, fixture readers x1.0. Best safe score |
| 15 | `ff41f0f9` | Sat 10:35 AM | 182.4 | 145.6 | 36.8 | 0 | v12k: local overhead 150 ms |
| 16 | `44495c74` | Sat 11:36 AM | 174.5 | 145.6 | 28.6 | 0 | v13: hot-swap local runs (no re-import for function-body patches), new-function insertions, faster parent. CPU -8.3. Best so far, safe |
| 17 | `798da171` | Sat 12:37 PM | 184.0 | 145.6 | 37.5 | 0 | v13b (A/B): correlated failure odds: more local runs, same simulated, CPU +8.9. Reverted |
| 18 | `9d4a2aed` | Sat 1:38 PM | 173.3 | 145.6 | 27.6 | 0 | v13d: cheap tests odds x0.8, structural patches handled, metadata files inert. Best so far |
| 19 | `18cf28a9` | Sat 2:40 PM | 174.1 | 145.6 | 28.5 | 0 | v13e: no blank-line pins for inserted code (labels only; board same) |
| 20 | `912006fc` | Sat 3:41 PM | 182.4 | 145.6 | 36.7 | 0 | v14c: prewarmed executor tables, lazy TPC tasks + early subtest stop, skip teardown. CPU +8 vs v13d: prewarm suspected (memory), disabled next |
| 21 | `4f2d665b` | Sat 4:11 PM | 168.4 | 141.7 | 26.7 | 0 | v15: operator-swap value profile, insurance tests in free slack, prewarm off (CPU back down). Best so far; first simulated drop in 9 versions |
| 22 | `82947f2b` | Sat 4:42 PM | 168.1 | 141.7 | 26.4 | 0 | v15b (A/B): heavy-test odds x0.4: no decision changes on the board. Reverted to x0.6 |
| 23 | `6050f229` | Sat 5:12 PM | 168.2 | 141.7 | 26.5 | 0 | v16: and/or swap profile, empty-registry guard, local-rename no-ops (safety for unseen kinds; board same) |
| 24 | `f0f3252c` | Sat 5:45 PM | 168.3 | 141.7 | 27.2 | 0 | v17: scaled proxy for test_max_nodes (big gains on labels; board has no such commit) |
| 25 | `3e431443` | Sat 6:21 PM | 165.1 | 138.4 | 26.3 | 0 | v18: lazy-load test only for core-module edits, generator-memory check on reachable dialects, CPU factor 1.0. Best so far |
| 26 | `a8ea9ddf` | Sat 7:07 PM | 162.5 | 138.4 | 24.1 | 0 | v19: owner-level table keys (edit reaches its class, subclasses and copies only). CPU -2.2. Best so far |
| 27 | `6e0a2751` | Sat 8:04 PM | 166.1 | 138.4 | 27.6 | 0 | v20: TPC-DS checked by data-free execution traces instead of a 23 s listing; memoizing-decorator patches listed unverified. Board simulated unchanged (no passing-TPC commit there), CPU +3.6 |
| 28 | `c41bfe8b` | Sat 9:18 PM | 160.9 | 138.4 | 22.5 | 0 | v21: syntax-error patches list only tests that load the broken module (module-need map), not every test. Best so far (board has no syntax-error commit; CPU lower) |
| 29 | `aca028d3` | Sat 10:24 PM | 164.5 | 138.4 | 26.1 | 0 | v22: modules failing at import or deleted list only their loaders; conftest/pytest.ini/pytest settings edits run everything; TPC trace only without line evidence. Board CPU varies ±3 s between identical-path versions |
| 30 | `aea778c3` | Sat 10:34 PM | 166.8 | 138.4 | 28.4 | 0 | v23: test package __init__ edits list every test under it; new or changed test-module import-time code (monkeypatching) runs everything |
| 31 | `c41bfe8b` | Sat 10:39 PM | 164.1 | 138.4 | 25.6 | 0 | v21 resubmitted unchanged: measures board CPU noise between runs: +3.1 s CPU vs its first run, same simulated |
| 32 | `dab69d31` (branch diag-nolocal) | Sun 1:01 AM | 188.3 | 183.3 | 5.1 | 0 | Diagnostic: no local runs. Local verification saves 45 s simulated for ~19 s CPU on the board |
| 33 | `b591520a` | Sun 1:25 AM | rejected | | | | Mistyped commit hash (not in repo); no effect on scores |
| 34 | `6c004fff` | Sun 1:26 AM | 441.6 | 417.2 | 24.4 | 1 | v24: TPC-DS/TPC-H fixture edits charged per query; expected-output-only edits skip the executor tests (25.5 s -> 4.2 s). Board has a TPC fixture commit: -21.2 s simulated, but 1 miss (test parses every query; a broken non-executed query fails it) |
| 35 | `dccc347c` | Sun 1:36 AM | 169.7 | 138.4 | 31.3 | 0 | v25: fix - any TPC fixture SQL change keeps the execute test, verified locally (parse all + run changed queries). Board's TPC fixture commit really fails TPC-DS: no simulated gain, local check cost ~6 s CPU |
| 36 | `ad128586` | Sun 2:01 AM | 161.4 | 138.4 | 22.9 | 0 | v26: changed TPC fixture queries parsed in the parent: parse failure -> list the execute test directly, non-executed and parses -> drop it |
| 37 | `98791d98` (branch diag-heavy) | Sun 2:26 AM | 419.9 | 395.8 | 24.2 | 1 | Diagnostic: tests over 5 s dropped from output: -42.7 s simulated, 1 miss (the TPC fixture commit) |
| 38 | `e454a45d` (branch diag-tpcds) | Sun 2:35 AM | 419.7 | 395.8 | 24.0 | 1 | Diagnostic: only TPC-DS dropped: identical. TPC-DS is listed in 2 board commits; one fails (fixture), one passes (~21 s wasted) |
| 39 | `7ff99398` | Sun 2:48 AM | 161.2 | 138.4 | 22.8 | 0 | v27: TPC-DS trace also with strong evidence, real run of few mismatching queries. Board's passing TPC-DS commit not reached by it (no CPU change) |
| 40 | `2c2d758e` (branch diag-tpcA) | Sun 3:01 AM | 140.3 | 117.0 | 23.3 | 0 | Diagnostic (UNSAFE rule, never the final pick): TPC-DS dropped where the trace check cannot apply. The board's wasted TPC-DS is in that group (-21.5 s, no miss) |
| 41 | `01745c5c` | Sun 3:26 AM | 153.6 | 141.8 | 11.9 | 0 | v28: TPC-DS trace allowed when other patched files cannot reach the run. CPU -11 s, simulated +3.3. Best safe score |
