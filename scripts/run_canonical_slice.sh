#!/bin/bash
# Launcher for canonical LongMemEval slice — keeps the python process
# alive even when the parent shell exits.

# Ignore SIGHUP so we survive shell exit
trap '' HUP

cd /home/z/my-project

START="${1:-0}"
END="${2:-100}"
OUT="benchmarks/results/canonical_slice_${START}_${END}.json"
CKPT="benchmarks/results/canonical_slice_${START}_${END}.ckpt.jsonl"
LOG="/tmp/canonical_${START}_${END}.log"

exec python -u scripts/longmemeval_canonical_full.py \
    --start "$START" --end "$END" \
    --out "$OUT" --checkpoint "$CKPT" \
    --max-messages-per-q 2000 \
    --max-seconds-per-q 180 \
    --db-dir /tmp/cortexm_canonical \
    > "$LOG" 2>&1
