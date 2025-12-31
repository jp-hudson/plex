#!/usr/bin/env bash
set -euo pipefail

############################################################
# ======================= CONFIG ===========================
############################################################

INPUT_DIR="${1:-/Users/jhudson/Downloads/thunder3}"
RAW_DIR_NAME="handbrake_raw"

PLEX_ROOT="/Volumes/Media"
MOVIES_DIR="$PLEX_ROOT/Movies"
TV_DIR="$PLEX_ROOT/TV"
ANIME_DIR="$PLEX_ROOT/Anime"

PRESET_1080P="HQ 1080p30 Surround"
FOUR_K_MIN_WIDTH=3000

KEEP_RAW=0

ANIME_SHOW_REGEX="Naruto|Bleach|One Piece|Attack on Titan"
ANIME_MOVIE_REGEX="Ghibli|Spirited Away|Your Name|Suzume"

############################################################
# ======================== LOGGING =========================
############################################################

LOG_DIR="$INPUT_DIR/logs"
LOG_FILE="$LOG_DIR/plex_ingest.log"
INGEST_LOG="$LOG_DIR/ingest_manifest.log"

mkdir -p "$LOG_DIR"

exec > >(gawk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' | tee -a "$LOG_FILE") 2>&1
echo "===== Ingest run $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$INGEST_LOG"

############################################################
# ==================== PRE-FLIGHT ==========================
############################################################

for cmd in unzip ffprobe HandBrakeCLI sed gawk; do
  command -v "$cmd" >/dev/null || {
    echo "Missing dependency: $cmd"
    exit 1
  }
done

RAW_DIR="$INPUT_DIR/$RAW_DIR_NAME"

############################################################
# ====================== HELPERS ===========================
############################################################

# Outputs: WIDTH HEIGHT
detect_resolution() {
  ffprobe -v error \
    -select_streams v:0 \
    -show_entries stream=width,height \
    -of csv=p=0 "$1" | tr ',' ' '
}

sanitize_name() {
  echo "$1" | sed \
    -e 's/\./ /g' \
    -e 's/_/ /g' \
    -e 's/[[:space:]]\+/ /g' \
    -e 's/^ //;s/ $//'
}

############################################################
# ============== MKV CLEANUP (YEAR-SAFE) ==================
############################################################

clean_mkv_name() {
  local file="$1"
  local dir base name norm title year cleaned

  dir="$(dirname "$file")"
  base="$(basename "$file")"
  name="${base%.mkv}"

  # Normalize separators
  norm="$(echo "$name" | sed 's/[._]/ /g')"

  # Extract year FIRST
  year="$(echo "$norm" | grep -oE '(19|20)[0-9]{2}' | head -n1 || true)"

  # Remove everything from year onward
  if [[ -n "$year" ]]; then
    title="$(echo "$norm" | sed -E "s/[[:space:]]*\(?$year\)?.*//")"
  else
    title="$norm"
  fi

  # Remove common junk
  title="$(echo "$title" | sed -E 's/(2160p|1080p|720p|480p|WEB[- ]DL|BluRay|HDRip|HDTV|AMZN|NF|REPACK|x264|x265|H\.?264|H\.?265).*//Ig')"

  title="$(sanitize_name "$title")"

  if [[ -n "$year" ]]; then
    cleaned="$title ($year).mkv"
  else
    cleaned="$title.mkv"
  fi

  if [[ "$base" != "$cleaned" ]]; then
    echo "RENAME | $base -> $cleaned" >> "$INGEST_LOG"
    mv "$file" "$dir/$cleaned"
  fi
}



############################################################
# ================= STEP 0: STAGING ========================
############################################################

echo "Staging files into $RAW_DIR_NAME/"
mkdir -p "$RAW_DIR"

shopt -s nullglob
for f in "$INPUT_DIR"/*.mkv "$INPUT_DIR"/*.zip; do
  mv "$f" "$RAW_DIR/"
done
shopt -u nullglob

############################################################
# ================= STEP 1: UNZIP ==========================
############################################################

for ZIP in "$RAW_DIR"/*.zip; do
  [[ -e "$ZIP" ]] || continue
  DEST_DIR="$RAW_DIR/$(basename "$ZIP" .zip)"
  [[ -d "$DEST_DIR" ]] && continue
  mkdir -p "$DEST_DIR"
  unzip -oq "$ZIP" -d "$DEST_DIR" || true
done

############################################################
# ================= STEP 2: RENAME =========================
############################################################

rename_in_dir() {
  local dir="$1"
  for f in "$dir"/*.mkv; do
    [[ -e "$f" ]] || continue
    clean_mkv_name "$f"
  done
}

rename_in_dir "$RAW_DIR"
for d in "$RAW_DIR"/*; do
  [[ -d "$d" ]] && rename_in_dir "$d"
done

############################################################
# ================= STEP 3: HANDBRAKE ======================
############################################################

encode_dir() {
  local SRC_DIR="$1"

  for FILE in "$SRC_DIR"/*.mkv; do
    [[ -e "$FILE" ]] || continue

    BASENAME="$(basename "$FILE" .mkv)"

    read -r WIDTH HEIGHT < <(detect_resolution "$FILE")
    echo "Detected resolution: ${WIDTH}x${HEIGHT}"

    [[ "$WIDTH" =~ ^[0-9]+$ ]] || {
      echo "ERROR: Invalid width '$WIDTH'"
      exit 1
    }

    (( WIDTH >= FOUR_K_MIN_WIDTH )) && TAG="4K" || TAG="1080p"

    MOVIE_YEAR="$(echo "$BASENAME" | grep -oE '(19|20)[0-9]{2}' | head -1 || true)"
    MOVIE_TITLE="$(sanitize_name "$(echo "$BASENAME" | sed -E 's/[[:space:]\(]*(19|20)[0-9]{2}.*$//')")"

    [[ "$MOVIE_TITLE" =~ $ANIME_MOVIE_REGEX ]] && LIB_ROOT="$ANIME_DIR" || LIB_ROOT="$MOVIES_DIR"
    DEST_DIR="$LIB_ROOT/$MOVIE_TITLE"
    mkdir -p "$DEST_DIR"

    [[ -n "$MOVIE_YEAR" ]] \
      && OUTPUT="$DEST_DIR/$MOVIE_TITLE ($MOVIE_YEAR).mp4" \
      || OUTPUT="$DEST_DIR/$MOVIE_TITLE.mp4"

    [[ -f "$OUTPUT" && -s "$OUTPUT" ]] && continue
    TMP="${OUTPUT}.partial"

    if [[ "$TAG" == "4K" ]]; then
      CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="HQ 2160p60 4K HEVC Surround" -q 20 )
    else
      CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="$PRESET_1080P" --format av_mp4 --optimize )
    fi

    echo "RUNNING HANDBRAKE COMMAND:"
    echo " ${CMD[*]}"
    echo "RUNNING | ${CMD[*]}" >> "$INGEST_LOG"

    "${CMD[@]}"
    mv "$TMP" "$OUTPUT"
  done
}

encode_dir "$RAW_DIR"
for d in "$RAW_DIR"/*; do
  [[ -d "$d" ]] && encode_dir "$d"
done

############################################################
# ================= STEP 4: CLEANUP ========================
############################################################

[[ "$KEEP_RAW" -eq 0 ]] && rm -rf "$RAW_DIR"
echo "Ingest complete."

