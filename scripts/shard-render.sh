#!/usr/bin/env bash
# Parallel sharded frame render — fan independent frames across CPU cores.
# Every frame is independent, so wall-clock ≈ single-core-time / nshards.
# The render script must honor OUT_DIR, RENDER_LO, RENDER_HI env vars.
#
# Usage:  shard-render.sh <render.py> <total_frames> <out_dir> [nshards] [--assemble out.mp4 [audio.wav]]
#
# Examples:
#   scripts/shard-render.sh scripts/piano-c.py 1440 /tmp/c-frames
#   scripts/shard-render.sh scripts/piano-c.py 1440 /tmp/c-frames 12 --assemble out.mp4 audio.wav
set -euo pipefail

SCRIPT="$1"; TOTAL="$2"; OUTDIR="$3"; SHARDS="${4:-$(nproc)}"
ASSEMBLE=""; AUDIO=""
if [[ "${5:-}" == "--assemble" ]]; then ASSEMBLE="${6:-}"; AUDIO="${7:-}"; fi

mkdir -p "$OUTDIR"; rm -f "$OUTDIR"/*.png
CHUNK=$(( (TOTAL + SHARDS - 1) / SHARDS ))
echo "[shard] $TOTAL frames / $SHARDS shards (chunk=$CHUNK) -> $OUTDIR"
START=$(date +%s)

pids=()
for ((i=0; i<SHARDS; i++)); do
  LO=$(( i*CHUNK )); HI=$(( LO+CHUNK )); (( HI>TOTAL )) && HI=$TOTAL
  (( LO>=TOTAL )) && break
  OUT_DIR="$OUTDIR" RENDER_LO=$LO RENDER_HI=$HI python3 "$SCRIPT" >/dev/null 2>&1 &
  pids+=($!)
done
fail=0
for p in "${pids[@]}"; do wait "$p" || fail=1; done

N=$(ls "$OUTDIR"/*.png 2>/dev/null | wc -l)
ELAPSED=$(( $(date +%s) - START ))
echo "[shard] rendered $N/$TOTAL frames in ${ELAPSED}s (fail=$fail)"
[[ "$N" -ne "$TOTAL" || "$fail" -ne 0 ]] && { echo "[shard] INCOMPLETE"; exit 1; }

if [[ -n "$ASSEMBLE" ]]; then
  # frame filename prefix is whatever the script writes (e.g. c-%04d.png); detect it
  PREFIX=$(basename "$(ls "$OUTDIR"/*.png | head -1)" | sed -E 's/[0-9]+\.png$/%04d.png/')
  echo "[shard] assembling $PREFIX -> $ASSEMBLE"
  if [[ -n "$AUDIO" ]]; then
    ffmpeg -y -framerate 24 -i "$OUTDIR/$PREFIX" -i "$AUDIO" \
      -c:v libx264 -pix_fmt yuv420p -crf 19 -preset medium -c:a aac -b:a 192k -shortest "$ASSEMBLE" 2>/dev/null
  else
    ffmpeg -y -framerate 24 -i "$OUTDIR/$PREFIX" \
      -c:v libx264 -pix_fmt yuv420p -crf 19 -preset medium "$ASSEMBLE" 2>/dev/null
  fi
  echo "[shard] wrote $ASSEMBLE"
fi
