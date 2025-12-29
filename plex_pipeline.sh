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
PRESET_4K="HQ 2160p60 4K HEVC Surround"
FOUR_K_MIN_WIDTH=3000

KEEP_RAW=0

ANIME_SHOW_REGEX="Naruto|Bleach|One Piece|Attack on Titan"
ANIME_MOVIE_REGEX="Ghibli|Spirited Away|Your Name|Suzume"

############################################################
# ======================== LOGGING =========================
############################################################

LOG_DIR="$INPUT_DIR/logs"
LOG_FILE="$LOG_DIR/plex_ingest.log"
mkdir -p "$LOG_DIR"

exec > >(gawk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' | tee -a "$LOG_FILE") 2>&1

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

detect_resolution() {
  ffprobe -v error \
    -select_streams v:0 \
    -show_entries stream=width \
    -of csv=p=0 "$1"
}

sanitize_name() {
  echo "$1" | sed \
    -e 's/\./ /g' \
    -e 's/_/ /g' \
    -e 's/[[:space:]]\+/ /g' \
    -e 's/^ //;s/ $//'
}

############################################################
# ================= MKV CLEANUP (SAFE) ====================
############################################################

clean_mkv_name() {
  local file="$1"
  local dir base name cleaned

  dir="$(dirname "$file")"
  base="$(basename "$file")"
  name="${base%.mkv}"

  # Cut at first technical marker (original behavior)
  cleaned="$(echo "$name" | sed -E 's/[[:space:]\(]*(1080p|720p|480p|2160p|WEB|WEB[- ]DL|BluRay|HDRip|HDTV).*//I')"

  cleaned="$(sanitize_name "$cleaned")"
  cleaned="${cleaned}.mkv"

  if [[ "$base" != "$cleaned" ]]; then
    echo "Renaming:"
    echo "  $base"
    echo "  → $cleaned"
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
  echo "  Moving $(basename "$f") → $RAW_DIR_NAME/"
  mv "$f" "$RAW_DIR/"
done
shopt -u nullglob

############################################################
# ================= STEP 1: UNZIP ==========================
############################################################

for ZIP in "$RAW_DIR"/*.zip; do
  [[ -e "$ZIP" ]] || continue

  BASENAME="$(basename "$ZIP" .zip)"
  DEST_DIR="$RAW_DIR/$BASENAME"

  [[ -d "$DEST_DIR" ]] && continue

  echo "Unzipping: $(basename "$ZIP") → $DEST_DIR"
  mkdir -p "$DEST_DIR"

  unzip -oq "$ZIP" -d "$DEST_DIR" || \
    echo "Warning: CRC issues in $ZIP — continuing"
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
  [[ -d "$d" ]] || continue
  rename_in_dir "$d"
done

############################################################
# ================= STEP 3: HANDBRAKE ======================
############################################################

encode_dir() {
  local SRC_DIR="$1"

  for FILE in "$SRC_DIR"/*.mkv; do
    [[ -e "$FILE" ]] || continue

    BASENAME="$(basename "$FILE" .mkv)"

    WIDTH="$(detect_resolution "$FILE")"
    if (( WIDTH >= FOUR_K_MIN_WIDTH )); then
      PRESET="$PRESET_4K"
      TAG="4K"
    else
      PRESET="$PRESET_1080P"
      TAG="1080p"
    fi

    ########################################################
    # TV / ANIME
    ########################################################
    if [[ "$BASENAME" =~ S([0-9]{2})E([0-9]{2}) ]]; then
      SEASON="${BASH_REMATCH[1]}"
      EPISODE="${BASH_REMATCH[2]}"

      SERIES_RAW="${BASENAME%%S${SEASON}E${EPISODE}*}"
      SERIES_NAME="$(sanitize_name "$SERIES_RAW")"

      EP_TITLE_RAW="${BASENAME#*S${SEASON}E${EPISODE}}"
      EP_TITLE="$(sanitize_name "$EP_TITLE_RAW")"
      EP_TITLE="$(echo "$EP_TITLE" | sed 's/^[[:space:]\._-]*//')"

      [[ "$SERIES_NAME" =~ $ANIME_SHOW_REGEX ]] \
        && LIB_ROOT="$ANIME_DIR" \
        || LIB_ROOT="$TV_DIR"

      DEST_DIR="$LIB_ROOT/$SERIES_NAME/season$(printf "%02d" "$SEASON")"
      mkdir -p "$DEST_DIR"

      OUTPUT="$DEST_DIR/$SERIES_NAME S${SEASON}E${EPISODE}${EP_TITLE:+ $EP_TITLE}.mp4"

    ########################################################
    # MOVIES (FIXED FOLDER LOGIC)
    ########################################################
    else
      MOVIE_FULL="$(sanitize_name "$BASENAME")"

      # Extract title + year (if present)
      if [[ "$MOVIE_FULL" =~ ^(.+)[[:space:]]([0-9]{4})(.*)$ ]]; then
        MOVIE_TITLE="${BASH_REMATCH[1]}"
        MOVIE_YEAR="${BASH_REMATCH[2]}"
      else
        MOVIE_TITLE="$MOVIE_FULL"
        MOVIE_YEAR=""
      fi

      # Final safety: strip scene tags from FOLDER NAME ONLY
      MOVIE_TITLE="$(echo "$MOVIE_TITLE" | sed -E 's/\b(REPACK|PROPER|REAL|EXTENDED|UNRATED|LIMITED)\b//Ig')"
      MOVIE_TITLE="$(sanitize_name "$MOVIE_TITLE")"

      [[ "$MOVIE_TITLE" =~ $ANIME_MOVIE_REGEX ]] \
        && LIB_ROOT="$ANIME_DIR" \
        || LIB_ROOT="$MOVIES_DIR"

      DEST_DIR="$LIB_ROOT/$MOVIE_TITLE"
      mkdir -p "$DEST_DIR"

      OUTPUT="$DEST_DIR/$MOVIE_TITLE${MOVIE_YEAR:+ $MOVIE_YEAR}.mp4"
    fi

    [[ -f "$OUTPUT" && -s "$OUTPUT" ]] && continue

    echo "Encoding [$TAG]: $FILE"
    TMP="${OUTPUT}.partial"

    HandBrakeCLI \
      -i "$FILE" \
      -o "$TMP" \
      --preset="$PRESET" \
      --format av_mp4 \
      --optimize

    mv "$TMP" "$OUTPUT"
  done
}

encode_dir "$RAW_DIR"
for d in "$RAW_DIR"/*; do
  [[ -d "$d" ]] || continue
  encode_dir "$d"
done

############################################################
# ================= STEP 4: CLEANUP ========================
############################################################

if [[ "$KEEP_RAW" -eq 0 ]]; then
  rm -rf "$RAW_DIR"
fi

echo "Ingest complete."

