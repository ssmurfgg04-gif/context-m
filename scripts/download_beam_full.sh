#!/usr/bin/env bash
# Download all 10 rows of Mohammadta/BEAM-10M via the datasets-server
# /rows endpoint (the only path that bypasses the CloudFront 429 that
# blocks huggingface.co direct from the sandbox).
#
# Each row is ~50-110 MB so we fetch one at a time and cache locally.
# Idempotent: skips rows that already exist.

set -uo pipefail

CACHE="${BEAM_CACHE_DIR:-/tmp/beam_cache}"
mkdir -p "$CACHE"

DS="https://datasets-server.huggingface.co/rows?dataset=Mohammadta/BEAM-10M&config=default&split=10M"

echo "[beam-dl] cache: $CACHE"
echo "[beam-dl] target: 10 rows (offsets 0..9)"

for i in $(seq 0 9); do
    OUT="$CACHE/beam_row_${i}.json"
    if [ -s "$OUT" ] && grep -q '"conversation_id"' "$OUT" 2>/dev/null; then
        SIZE=$(wc -c < "$OUT")
        echo "[beam-dl] row $i already cached ($SIZE bytes) — skip"
        continue
    fi
    echo "[beam-dl] fetching row $i ..."
    for ATTEMPT in 1 2 3 4 5; do
        rm -f "${OUT}.partial"
        curl -sS \
            --connect-timeout 30 --max-time 600 \
            -H "User-Agent: context-m-beam/1.0" \
            "${DS}&offset=${i}&length=1" \
            -o "${OUT}.partial"
        CURL_RC=$?
        if [ $CURL_RC -eq 0 ] && [ -s "${OUT}.partial" ] && \
                grep -q '"conversation_id"' "${OUT}.partial" 2>/dev/null; then
            mv "${OUT}.partial" "$OUT"
            SIZE=$(wc -c < "$OUT")
            echo "[beam-dl] row $i OK ($SIZE bytes, attempt $ATTEMPT)"
            break
        fi
        echo "[beam-dl] row $i attempt $ATTEMPT failed (rc=$CURL_RC) — backoff $((ATTEMPT*10))s"
        sleep $((ATTEMPT*10))
    done
    if [ ! -s "$OUT" ]; then
        echo "[beam-dl] row $i FAILED after 5 attempts — continuing anyway"
    fi
done

echo "[beam-dl] === done ==="
ls -la "$CACHE" | head -15
du -sh "$CACHE"
