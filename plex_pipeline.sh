#!/usr/bin/env bash
set -euo pipefail

############################################################
# ======================= CONFIG ===========================
############################################################

INPUT_DIR="${1:-/Users/jhudson/PlexDrop/}"
RAW_DIR_NAME="handbrake_raw"

PLEX_ROOT="/Volumes/Media"
MOVIES_DIR="$PLEX_ROOT/Movies"
TV_DIR="$PLEX_ROOT/TV"
ANIME_DIR="$PLEX_ROOT/Anime"

PRESET_1080P="HQ 1080p30 Surround"
FOUR_K_MIN_WIDTH=3000
KEEP_RAW=0

# If a zip dest dir exists but was not successfully marked as unzipped,
# should we delete it and retry clean?
UNZIP_RETRY_CLEAN=1

ANIME_SHOW_REGEX="Naruto|Bleach|One Piece|Attack on Titan"
ANIME_MOVIE_REGEX="Ghibli|Spirited Away|Your Name|Suzume"

############################################################
# ======================== LOGGING =========================
############################################################

LOG_DIR="/Users/$USER/PlexDrop/logs"
LOG_FILE="$LOG_DIR/plex_ingest.log"
INGEST_LOG="$LOG_DIR/ingest_manifest.log"

mkdir -p "$LOG_DIR"
exec > >(gawk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' | /usr/bin/tee -a "$LOG_FILE") 2>&1
echo "===== Ingest run $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$INGEST_LOG"

############################################################
# ==================== PRE-FLIGHT ==========================
############################################################

for cmd in unzip ffprobe HandBrakeCLI sed gawk find sort dirname python3; do
  command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd"; exit 1; }
done

RAW_DIR="$INPUT_DIR/$RAW_DIR_NAME"

############################################################
# ====================== HELPERS ===========================
############################################################

detect_resolution() {
  [[ -f "${1:-}" ]] || { echo "0 0"; return; }
  ffprobe -v error \
    -select_streams v:0 \
    -show_entries stream=width,height \
    -of csv=p=0 "$1" | tr ',' ' '
}

sanitize_name() {
  echo "$1" | sed -E 's/[._]/ /g; s/[[:space:]]+/ /g; s/^ //; s/ $//'
}

trim_dashes() {
  echo "$1" | sed -E 's/^[[:space:]-]+//; s/[[:space:]-]+$//'
}

# Title-case ONLY if the string has no uppercase letters already
# (so we don't mess up iCarly, Se7en, WALL·E, etc.)
maybe_title_case() {
  local s="$1"

  # If there's already any uppercase, assume it's intentional
  if [[ "$s" =~ [A-Z] ]]; then
    echo "$s"
    return
  fi

  /usr/bin/python3 - "$s" <<'PY'
import re, sys
s = sys.argv[1].strip()
if not s:
    print("")
    raise SystemExit

small = {"a","an","and","as","at","but","by","for","from","in","of","on","or","the","to","via","vs","with"}

def cap_token(tok: str) -> str:
    parts = tok.split('-')
    out = []
    for p in parts:
        if not p:
            out.append(p)
            continue
        if re.fullmatch(r'[ivxlcdm]+', p):
            out.append(p.upper())
        elif re.search(r'\d', p):
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:].lower())
    return "-".join(out)

words = s.split()
res = []
n = len(words)
for i, w in enumerate(words):
    lw = w.lower()
    if i not in (0, n-1) and lw in small:
        res.append(lw)
    else:
        res.append(cap_token(w))
print(" ".join(res))
PY
}

############################################################
# ============== CLEANUP FUNCTIONS (SPLIT) ================
############################################################

# MOVIES: keep year if present
clean_mkv_name_movie() {
  local file="$1" dir base name norm year title cleaned

  dir="$(dirname "$file")"
  base="$(basename "$file")"
  name="${base%.mkv}"

  norm="$(echo "$name" | sed 's/[._]/ /g')"
  year="$(echo "$norm" | grep -oE '(19|20)[0-9]{2}' | head -1 || true)"

  if [[ -n "$year" ]]; then
    title="$(echo "$norm" | sed -E "s/[[:space:]]*\(?$year\)?.*//")"
  else
    title="$norm"
  fi

  # remove common junk (before final formatting)
  title="$(echo "$title" | sed -E 's/(2160p|1080p|720p|480p|WEB[- ]DL|WEBRip|BluRay|HDRip|HDTV|AMZN|NF|REPACK|x264|x265|H\.?264|H\.?265|DDP[0-9.]+|AAC[0-9.]+).*//Ig')"
  title="$(maybe_title_case "$(sanitize_name "$title")")"

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

# TV: NEVER keep year, NEVER keep parentheses
clean_mkv_name_tv() {
  local file="$1" dir base name cleaned

  dir="$(dirname "$file")"
  base="$(basename "$file")"
  name="${base%.mkv}"

  cleaned="$(echo "$name" \
    | sed -E 's/\([[:space:]]*(19|20)[0-9]{2}[[:space:]]*\)//g' \
    | sed -E 's/(2160p|1080p|720p|480p|WEB[- ]DL|WEBRip|BluRay|HDRip|HDTV|AMZN|NF|REPACK|x264|x265|H\.?264|H\.?265|DDP[0-9.]+|AAC[0-9.]+).*//Ig' \
    | sed -E 's/[()]+//g')"

  cleaned="$(maybe_title_case "$(sanitize_name "$cleaned")").mkv"

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
  mv "$f" "$RAW_DIR/" 2>/dev/null || true
done
shopt -u nullglob

############################################################
# ================= STEP 1: UNZIP (RESTARTABLE) ============
############################################################

# v1-style extraction layout: RAW_DIR/<zipname>/...
# v2 fix: only mark success if unzip actually succeeds (exit 0 or 1).
for ZIP in "$RAW_DIR"/*.zip; do
  [[ -f "$ZIP" ]] || continue

  DEST_DIR="$RAW_DIR/$(basename "$ZIP" .zip)"
  MARKER="$DEST_DIR/.unzipped_ok"

  # If already successfully unzipped, never unzip again
  if [[ -f "$MARKER" ]]; then
    echo "SKIP UNZIP (already ok): $(basename "$ZIP")"
    continue
  fi

  # If this folder already has MKVs, treat it as extracted (helps migrate from older runs)
  if [[ -d "$DEST_DIR" ]] && find "$DEST_DIR" -maxdepth 1 -type f -name '*.mkv' -print -quit | grep -q .; then
    echo "SKIP UNZIP (mkv already present): $(basename "$ZIP")"
    touch "$MARKER"
    continue
  fi

  # If folder exists but no marker, assume previous unzip was incomplete and retry clean
  if [[ -d "$DEST_DIR" && "$UNZIP_RETRY_CLEAN" -eq 1 ]]; then
    echo "RETRY UNZIP (clean): removing incomplete $DEST_DIR"
    rm -rf "$DEST_DIR"
  fi

  mkdir -p "$DEST_DIR"
  echo "UNZIP | $(basename "$ZIP") -> $DEST_DIR"
  echo "UNZIP | $(basename "$ZIP") -> $DEST_DIR" >> "$INGEST_LOG"

  # Important: </dev/null prevents interactive prompts (disk full, etc.)
  # unzip exit codes: 0=ok, 1=warnings, >1=error
  set +e
  unzip -oq "$ZIP" -d "$DEST_DIR" </dev/null
  rc=$?
  set -e

  if [[ "$rc" -le 1 ]]; then
    touch "$MARKER"
  else
    echo "WARNING: unzip failed for $(basename "$ZIP") (exit=$rc). Will retry next run."
    echo "WARNING: unzip failed for $(basename "$ZIP") (exit=$rc)" >> "$INGEST_LOG"
    # leave NO marker so it retries later
  fi
done

############################################################
# ================= STEP 2: RENAME =========================
############################################################

# Rename every MKV anywhere under RAW_DIR (so reruns “start over” cleanly)
# TV detection upgraded to also catch: S01 E01, S01-E01, S01_E01, S01.E01 (etc.)
while IFS= read -r -d '' f; do
  base="$(basename "$f")"
  if [[ "$base" =~ S[0-9]{1,2}[[:space:]_.-]*E[0-9]{1,2} ]]; then
    clean_mkv_name_tv "$f"
  else
    clean_mkv_name_movie "$f"
  fi
done < <(find "$RAW_DIR" -type f -name '*.mkv' -print0)

############################################################
# ================= STEP 3: HANDBRAKE ======================
############################################################

encode_dir() {
  local SRC="$1"
  shopt -s nullglob
  for FILE in "$SRC"/*.mkv; do
    [[ -f "$FILE" ]] || continue

    BASENAME="$(basename "$FILE" .mkv)"

    read -r WIDTH HEIGHT < <(detect_resolution "$FILE")
    [[ "$WIDTH" =~ ^[0-9]+$ ]] || { echo "ERROR: invalid width '$WIDTH' for $FILE"; exit 1; }
    (( WIDTH >= FOUR_K_MIN_WIDTH )) && TAG="4K" || TAG="1080p"

    # Normalize common TV patterns like:
    #   "S01 E01", "S01-E01", "S01_E01", "S01.E01" -> "S01E01"
    BASENAME_TV="$(echo "$BASENAME" | sed -E 's/S([0-9]{1,2})[[:space:]_.-]*E([0-9]{1,2})/S\1E\2/g')"

    if [[ "$BASENAME_TV" =~ S([0-9]{1,2})E([0-9]{1,2}) ]]; then
      SEASON_RAW="${BASH_REMATCH[1]}"
      EPISODE_RAW="${BASH_REMATCH[2]}"

      # Always output padded SxxExx + seasonxx folder

      SEASON="$(printf "%02d" "$((10#$SEASON_RAW))")"
      EPISODE="$(printf "%02d" "$((10#$EPISODE_RAW))")"

      SERIES="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV%%S${SEASON_RAW}E${EPISODE_RAW}*}")")")"
      TITLE="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV#*S${SEASON_RAW}E${EPISODE_RAW}}" | sed 's/[()]+//g')")")"

      # Anime TV routing
      if [[ "$SERIES" =~ $ANIME_SHOW_REGEX ]]; then
        TV_ROOT="$ANIME_DIR"
      else
        TV_ROOT="$TV_DIR"
      fi

      DEST="$TV_ROOT/$SERIES/season${SEASON}"

      mkdir -p "$DEST"
      OUT="$DEST/$SERIES - S${SEASON}E${EPISODE}${TITLE:+ - $TITLE}.mp4"
    else
      YEAR="$(echo "$BASENAME" | grep -oE '(19|20)[0-9]{2}' | head -1 || true)"
      TITLE="$(maybe_title_case "$(sanitize_name "$(echo "$BASENAME" | sed -E 's/[[:space:]\(]*(19|20)[0-9]{2}.*$//')")")"

      # Anime movie routing
      if [[ "$TITLE" =~ $ANIME_MOVIE_REGEX ]]; then
        MOV_ROOT="$ANIME_DIR"
      else
        MOV_ROOT="$MOVIES_DIR"
      fi

      DEST="$MOV_ROOT/$TITLE"
      mkdir -p "$DEST"
      OUT="$DEST/$TITLE${YEAR:+ ($YEAR)}.mp4"
    fi

    if [[ -f "$OUT" && -s "$OUT" ]]; then
     echo "SKIP (exists): $OUT"
     echo "SKIP (exists): $OUT" >> "$INGEST_LOG"
     continue
    fi

    TMP="$OUT.partial"
    rm -f "$TMP" 2>/dev/null || true

    if [[ "$TAG" == "4K" ]]; then
      CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="HQ 2160p60 4K HEVC Surround" -q 20 )
    else
      CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="$PRESET_1080P" --format av_mp4 --optimize )
    fi

    echo "RUNNING | ${CMD[*]}"
    echo "RUNNING | ${CMD[*]}" >> "$INGEST_LOG"

    "${CMD[@]}" </dev/null
    mv "$TMP" "$OUT"
  done
  shopt -u nullglob
}

# Encode each directory containing MKVs exactly once (handles nested unzip layouts too)
find "$RAW_DIR" -type f -name '*.mkv' -print0 \
  | while IFS= read -r -d '' f; do dirname "$f"; done \
  | sort -u \
  | while IFS= read -r dir; do
      echo "ENCODE DIR: $dir" </dev/null
      echo "ENCODE DIR: $dir" >> "$INGEST_LOG"
      encode_dir "$dir"
    done

############################################################
# ================= STEP 4: CLEANUP ========================
############################################################

if [[ "$KEEP_RAW" -eq 0 ]]; then
  rm -rf "$RAW_DIR"
fi
echo "Ingest complete."

