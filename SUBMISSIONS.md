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
| 20 | `912006fc` | Sat 3:41 PM | ~182 | 145.6 | 36.7 | 0 | v14c: prewarmed executor tables, lazy TPC tasks + early subtest stop, skip teardown. CPU +8 vs v13d: prewarm suspected (memory), disabled next |
