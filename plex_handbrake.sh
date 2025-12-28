#!/usr/bin/env bash
set -euo pipefail

############################
# CONFIG
############################

INPUT_DIR="/Users/jhudson/Downloads/thunder3"
PLEX_ROOT="/Volumes/Media"

MOVIES_DIR="$PLEX_ROOT/Movies"
TV_DIR="$PLEX_ROOT/TV"
ANIME_DIR="$PLEX_ROOT/Anime"

PRESET_1080P="HQ 1080p30 Surround"
PRESET_4K="HQ 2160p60 4K HEVC Surround"
FOUR_K_MIN_WIDTH=3000

############################
# HELPERS
############################

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
    -e 's/  */ /g' \
    -e 's/^ *//;s/ *$//'
}

############################
# MAIN LOOP
############################

for FILE in "$INPUT_DIR"/*.mkv; do
  BASENAME=$(basename "$FILE" .mkv)

  WIDTH=$(detect_resolution "$FILE")
  if (( WIDTH >= FOUR_K_MIN_WIDTH )); then
    PRESET="$PRESET_4K"
    TAG="4K"
  else
    PRESET="$PRESET_1080P"
    TAG="1080p"
  fi

  ##################################################################
  # TV / ANIME EPISODES (SxxEyy)
  ##################################################################
  if [[ "$BASENAME" =~ S([0-9]{2})E([0-9]{2}) ]]; then
    SEASON="${BASH_REMATCH[1]}"
    EPISODE="${BASH_REMATCH[2]}"

    # Series name (everything before SxxEyy)
    SERIES_RAW="${BASENAME%%S${SEASON}E${EPISODE}*}"
    SERIES_NAME=$(sanitize_name "$SERIES_RAW")

    # Episode title (everything after SxxEyy)
    EP_TITLE_RAW="${BASENAME#*S${SEASON}E${EPISODE}}"
    EP_TITLE=$(sanitize_name "$EP_TITLE_RAW")
    EP_TITLE=$(echo "$EP_TITLE" | sed 's/^[[:space:]\._-]*//')

    # Anime heuristic (adjustable)
    if [[ "$SERIES_NAME" =~ (Naruto|Bleach|One\ Piece|Attack\ on\ Titan) ]]; then
      LIB_ROOT="$ANIME_DIR"
    else
      LIB_ROOT="$TV_DIR"
    fi

    DEST_DIR="$LIB_ROOT/$SERIES_NAME/season$(printf "%02d" "$SEASON")"
    mkdir -p "$DEST_DIR"

    if [[ -n "$EP_TITLE" ]]; then
      OUTPUT="$DEST_DIR/$SERIES_NAME S${SEASON}E${EPISODE} $EP_TITLE.mp4"
    else
      OUTPUT="$DEST_DIR/$SERIES_NAME S${SEASON}E${EPISODE}.mp4"
    fi

  ##################################################################
  # MOVIES (NORMAL + ANIME)
  ##################################################################
  else
    MOVIE_NAME=$(sanitize_name "$BASENAME")

    # Anime movie heuristic
    if [[ "$MOVIE_NAME" =~ (Ghibli|Spirited\ Away|Your\ Name|Suzume) ]]; then
      LIB_ROOT="$ANIME_DIR"
    else
      LIB_ROOT="$MOVIES_DIR"
    fi

    DEST_DIR="$LIB_ROOT/$MOVIE_NAME"
    mkdir -p "$DEST_DIR"

    OUTPUT="$DEST_DIR/$MOVIE_NAME.mp4"
  fi

  ##################################################################
  # SKIP IF ALREADY ENCODED
  ##################################################################
  if [[ -f "$OUTPUT" && -s "$OUTPUT" ]]; then
    echo "Skipping (exists): $OUTPUT"
    continue
  fi

  ##################################################################
  # ENCODE
  ##################################################################
  echo "Encoding [$TAG]: $FILE"
  echo " → $OUTPUT"

  TMP_OUTPUT="${OUTPUT}.partial"

  HandBrakeCLI \
    -i "$FILE" \
    -o "$TMP_OUTPUT" \
    --preset="$PRESET" \
    --format av_mp4 \
    --optimize

  mv "$TMP_OUTPUT" "$OUTPUT"

done

