#!/usr/bin/env bash
# usage: eval.sh DATA_DIR ANSWERS_JSON [extra env...]
# Runs the selector natively (dev venv) and scores it with the kit's score.py.
# Prints simulated seconds, misses, and the selector's own CPU seconds.
set -euo pipefail
DATA=$1; ANS=$2
DEV=/home/claude/dev
OUT=$(mktemp)
TMPD=$(mktemp -d)
export BR_DATA=$DATA BR_REPO=$DEV/sqlglot BR_MAPS=$DEV/br/maps/maps.pkl BR_TMP=$TMPD SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
START=$(date +%s.%N)
python3 -c '
import resource,subprocess,sys,time
t=time.time(); r=subprocess.run(sys.argv[1:]); u=resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"CPU user={u.ru_utime:.2f} sys={u.ru_stime:.2f} total={u.ru_utime+u.ru_stime:.2f} wall={time.time()-t:.2f}", file=sys.stderr); sys.exit(r.returncode)
' $DEV/venv/bin/python $DEV/br/selector/main.py > "$OUT" 2> "$OUT.err" || { cat "$OUT.err"; exit 1; }
grep -E "^CPU|done:|BROAD|timed out|crashed|budget" "$OUT.err" || true
python3 /home/claude/kits/br/score.py "$DATA" "$ANS" "$OUT"
cp "$OUT" $DEV/data/last_output.jsonl; cp "$OUT.err" $DEV/data/last_output.err
rm -rf "$TMPD"
