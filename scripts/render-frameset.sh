#!/usr/bin/env bash
##
## render-frameset.sh — produce one conformant base-frame set (D#358 slice 2, R6).
##
## Thin wrapper over shard-render.sh (which already works and is NOT modified by
## this slice): shard a frame-script render across physical cores, assemble the
## PNGs into scene.mp4, and emit a conforming manifest.json next to it.
##
## Usage:
##   render-frameset.sh <frame-script> <total_frames> <out_dir> [options]
##
## Options:
##   --fps N               clip frame rate (default 24 — must match the script's own rate)
##   --acc-dir DIR         asset dir, forwarded as ACC_DIR (default: the script's own default)
##   --shards N            force a shard count (default: auto from physical cores, see below)
##   --upload BUCKET/PREFIX
##                         also put manifest.json + scene.mp4 to R2 via wrangler
##   --print-shards        print the auto shard count for <total_frames> and exit (no render)
##
## Outputs in <out_dir>: frames/ (per-frame PNGs), scene.mp4, manifest.json.
##
## Producer bounds (R5, workload ii — measured on the 1024x1024 frame script):
##   MAX_BASE_FRAMES_PER_JOB=1800   refused up front (75 s @ 24 fps + headroom).
##   PER_SHARD_CEILING_SEC=180      a shard phase running past this is killed as a
##                                  group and the run fails loudly (3x measured shard time).
##   SHARD_TARGET_FRAMES=120        auto shards = ceil(total/120), capped by MAX_SHARDS.
##   MAX_SHARDS=min(physical,16)    PHYSICAL cores, never nproc (SMT bought ~28%).
##   MAX_JOB_FRAME_BYTES=3000000000 run aborts before assemble when the frames on
##                                  disk exceed this (silent ephemeral-disk fill).
##   PEAK_RSS_PER_SHARD_MB=256      sizing note only (measured 49 MB @1024px, ~120 MB
##                                  at 1080p) — pick instances accordingly; not enforced here.
##   BASE_RENDER_WALL_CLOCK_CEILING_SEC=600
##                                  single-core worst case at 0.40 s/frame; sharded runs
##                                  are bounded by the 180 s shard ceiling instead.
## PER_SHARD_CEILING_SEC and MAX_JOB_FRAME_BYTES honor same-named env overrides
## (spec defaults shown above) so tests can exercise the abort paths quickly.
##
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARD_RENDER="$SCRIPT_DIR/shard-render.sh"

MAX_BASE_FRAMES_PER_JOB=1800
PER_SHARD_CEILING_SEC="${PER_SHARD_CEILING_SEC:-180}"
SHARD_TARGET_FRAMES=120
MAX_SHARDS_HARD=16
MAX_JOB_FRAME_BYTES="${MAX_JOB_FRAME_BYTES:-3000000000}"
FPS=24
ACC_DIR=""
SHARDS_OVERRIDE=""
UPLOAD=""
PRINT_SHARDS=false

usage() { sed -n '2,30p' "$0"; }

# --- args ---
if [ "$#" -lt 3 ]; then usage; exit 2; fi
FRAME_SCRIPT="$1"; TOTAL="$2"; OUT_DIR="$3"; shift 3
while [ "$#" -gt 0 ]; do
  case "$1" in
    --fps) FPS="$2"; shift 2 ;;
    --acc-dir) ACC_DIR="$2"; shift 2 ;;
    --shards) SHARDS_OVERRIDE="$2"; shift 2 ;;
    --upload) UPLOAD="$2"; shift 2 ;;
    --print-shards) PRINT_SHARDS=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[frameset] unknown flag: $1" >&2; usage; exit 2 ;;
  esac
done

[ -f "$FRAME_SCRIPT" ] || { echo "[frameset] frame script not found: $FRAME_SCRIPT" >&2; exit 2; }
case "$TOTAL" in ''|*[!0-9]*) echo "[frameset] total_frames must be a positive integer" >&2; exit 2 ;; esac
[ "$TOTAL" -gt 0 ] || { echo "[frameset] total_frames must be a positive integer" >&2; exit 2; }
case "$FPS" in ''|*[!0-9]*) echo "[frameset] --fps must be a positive integer" >&2; exit 2 ;; esac

# --- producer bound: job size refused up front ---
if [ "$TOTAL" -gt "$MAX_BASE_FRAMES_PER_JOB" ]; then
  echo "[frameset] refused: $TOTAL frames exceeds MAX_BASE_FRAMES_PER_JOB=$MAX_BASE_FRAMES_PER_JOB" >&2
  exit 1
fi

# --- physical core count (never nproc: SMT buys ~28% on this workload) ---
physical_cores() {
  if command -v lscpu >/dev/null 2>&1; then
    local n
    n=$(lscpu --parse=core,socket 2>/dev/null | grep -v '^#' | sort -u | wc -l)
    [ "${n:-0}" -gt 0 ] 2>/dev/null && { echo "$n"; return; }
  fi
  if [ -r /proc/cpuinfo ] && grep -q '^physical id' /proc/cpuinfo 2>/dev/null; then
    awk '/^physical id/{p=$4} /^core id/{c=$4; seen[p":"c]=1} END{print length(seen)}' /proc/cpuinfo
    return
  fi
  if command -v sysctl >/dev/null 2>&1; then
    local n
    n=$(sysctl -n hw.physicalcpu 2>/dev/null || true)
    [ "${n:-0}" -gt 0 ] 2>/dev/null && { echo "$n"; return; }
  fi
  # Last resort only (non-Linux/macOS without topology): logical count.
  nproc
}

PHYSICAL=$(physical_cores)
[ "${PHYSICAL:-0}" -gt 0 ] 2>/dev/null || PHYSICAL=1
MAX_SHARDS=$(( PHYSICAL < MAX_SHARDS_HARD ? PHYSICAL : MAX_SHARDS_HARD ))

auto_shards() {
  local want=$(( (TOTAL + SHARD_TARGET_FRAMES - 1) / SHARD_TARGET_FRAMES ))
  [ "$want" -lt 1 ] && want=1
  [ "$want" -gt "$MAX_SHARDS" ] && want="$MAX_SHARDS"
  echo "$want"
}

if [ "$PRINT_SHARDS" = true ]; then
  auto_shards
  exit 0
fi

if [ -n "$SHARDS_OVERRIDE" ]; then
  case "$SHARDS_OVERRIDE" in ''|*[!0-9]*) echo "[frameset] --shards must be a positive integer" >&2; exit 2 ;; esac
  SHARDS="$SHARDS_OVERRIDE"
  [ "$SHARDS" -lt 1 ] && SHARDS=1
  [ "$SHARDS" -gt "$MAX_SHARDS_HARD" ] && SHARDS="$MAX_SHARDS_HARD"
else
  SHARDS=$(auto_shards)
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "[frameset] ffmpeg not found on PATH" >&2; exit 1; }
command -v ffprobe >/dev/null 2>&1 || { echo "[frameset] ffprobe not found on PATH" >&2; exit 1; }

FRAMES_DIR="$OUT_DIR/frames"
mkdir -p "$FRAMES_DIR"

echo "[frameset] $TOTAL frames / $SHARDS shards (physical=$PHYSICAL) -> $FRAMES_DIR"

# --- shard phase, bounded by PER_SHARD_CEILING_SEC (whole group killed on expiry) ---
# set -m puts the shard run in its own process group so the timeout kill takes
# the frame-script children with it instead of orphaning them.
set -m
if [ -n "$ACC_DIR" ]; then export ACC_DIR; fi
bash "$SHARD_RENDER" "$FRAME_SCRIPT" "$TOTAL" "$FRAMES_DIR" "$SHARDS" &
SHARD_PID=$!
SHARD_RC=0
for ((s=0; s<PER_SHARD_CEILING_SEC; s++)); do
  kill -0 "$SHARD_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$SHARD_PID" 2>/dev/null; then
  kill -- -"$SHARD_PID" 2>/dev/null || true
  wait "$SHARD_PID" 2>/dev/null || true
  echo "[frameset] FAILED: shard phase exceeded PER_SHARD_CEILING_SEC=${PER_SHARD_CEILING_SEC}s — killed, no clip assembled" >&2
  exit 1
fi
wait "$SHARD_PID" || SHARD_RC=$?
set +m
if [ "$SHARD_RC" -ne 0 ]; then
  echo "[frameset] FAILED: shard phase exited $SHARD_RC — no clip assembled" >&2
  exit 1
fi

# --- producer bound: abort before assemble when the frames on disk exceed the job budget ---
SIZE_KB=$(du -sk "$FRAMES_DIR" | cut -f1)
SIZE_BYTES=$(( SIZE_KB * 1024 ))
if [ "$SIZE_BYTES" -gt "$MAX_JOB_FRAME_BYTES" ]; then
  echo "[frameset] FAILED: frames on disk ($SIZE_BYTES bytes) exceed MAX_JOB_FRAME_BYTES=$MAX_JOB_FRAME_BYTES — refusing to assemble" >&2
  exit 1
fi

# --- assemble (libx264, yuv420p, crf 19, medium — plus an even-dimension pad
# shard-render.sh lacks, since libx264 rejects odd sizes like 1444x1089) ---
FIRST_PNG=$(ls "$FRAMES_DIR"/*.png | head -1)
PREFIX=$(basename "$FIRST_PNG" | sed -E 's/[0-9]+\.png$/%04d.png/')
ffmpeg -y -v error -framerate "$FPS" -i "$FRAMES_DIR/$PREFIX" \
  -vf "pad=ceil(iw/2)*2:ceil(ih/2)*2" -c:v libx264 -pix_fmt yuv420p -crf 19 -preset medium "$OUT_DIR/scene.mp4"

# --- manifest (dims + rate from ffprobe of the assembled clip, sha of its bytes) ---
PROBE_JSON=$(ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,avg_frame_rate,nb_frames -of json "$OUT_DIR/scene.mp4")
WIDTH=$(printf '%s' "$PROBE_JSON" | sed -n 's/.*"width": \([0-9]*\).*/\1/p')
HEIGHT=$(printf '%s' "$PROBE_JSON" | sed -n 's/.*"height": \([0-9]*\).*/\1/p')
RATE=$(printf '%s' "$PROBE_JSON" | sed -n 's/.*"avg_frame_rate": "\([^"]*\)".*/\1/p')
MANIFEST_FPS=$(awk -F/ '{ if ($2 == "" || $2 == 0) print $1; else print $1/$2 }' <<<"$RATE")
FRAME_COUNT=$(ls "$FRAMES_DIR"/*.png | wc -l)
CLIP_SHA=$(sha256sum "$OUT_DIR/scene.mp4" | cut -d' ' -f1)
printf '{"version":1,"frameCount":%d,"fps":%s,"width":%d,"height":%d,"clipSha256":"%s"}\n' \
  "$FRAME_COUNT" "$MANIFEST_FPS" "$WIDTH" "$HEIGHT" "$CLIP_SHA" > "$OUT_DIR/manifest.json"

echo "[frameset] done frames=$FRAME_COUNT ${WIDTH}x${HEIGHT}@${MANIFEST_FPS}fps shards=$SHARDS clip=$OUT_DIR/scene.mp4 sha=$CLIP_SHA"

# --- optional R2 upload ---
if [ -n "$UPLOAD" ]; then
  command -v wrangler >/dev/null 2>&1 || { echo "[frameset] --upload needs wrangler on PATH" >&2; exit 1; }
  BUCKET="${UPLOAD%%/*}"; DEST_PREFIX="${UPLOAD#*/}"
  [ "$BUCKET" != "$UPLOAD" ] || { echo "[frameset] --upload must be BUCKET/PREFIX" >&2; exit 2; }
  wrangler r2 object put "$BUCKET/$DEST_PREFIX/manifest.json" --file "$OUT_DIR/manifest.json"
  wrangler r2 object put "$BUCKET/$DEST_PREFIX/scene.mp4" --file "$OUT_DIR/scene.mp4"
  echo "[frameset] uploaded to $BUCKET/$DEST_PREFIX/"
fi
