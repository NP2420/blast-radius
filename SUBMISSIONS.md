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
