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

ANIME_SHOW_REGEX="Naruto|Bleach|One Piece|Attack on Titan|Solo Leveling"
ANIME_MOVIE_REGEX="Ghibli|Spirited Away|Your Name|Suzume"

# ---------------- AUDIO HANDOFF ----------------
# Anything audio-only that shows up (loose files, folders, or audio-only ZIPs)
# gets moved here so it won't be deleted with handbrake_raw cleanup.
AUDIO_QUEUE="$INPUT_DIR/audiobook_queue"
AUDIO_INCOMING_FILES="$AUDIO_QUEUE/_incoming_files"
AUDIO_SOURCE_ZIPS="$AUDIO_QUEUE/_source_zips"
AUDIO_FAILED_ZIPS="$AUDIO_QUEUE/_failed_zips"

# If you want plex_pipeline.sh to invoke your audio pipeline automatically:
RUN_AUDIO_PIPELINE=0
AUDIO_PIPELINE="/Users/jhudson/code/plex/audio_pipeline.py"

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

mkdir -p "$AUDIO_QUEUE" "$AUDIO_INCOMING_FILES" "$AUDIO_SOURCE_ZIPS" "$AUDIO_FAILED_ZIPS"

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

# Shared "junk" stripper for titles (works for both MKV and MP4 naming)
strip_release_junk() {
  # NOTE: Keep this conservative; it only removes from the first matched token onward.
  # IMPORTANT FIX: require a non-alphanumeric boundary before the token so "NF" doesn't match inside "Infinity".
  echo "$1" | sed -E 's/(^|[^[:alnum:]])(2160p|1080p|720p|480p|WEB[- ]DL|WEBRip|BluRay|HDRip|HDTV|AMZN|NF|REPACK|x264|x265|H\.?264|H\.?265|HEVC|AV1|DDP[0-9. ]+|AAC[0-9. ]+|EAC3|AC3|TRUEHD|ATMOS|ESub|Eng).*$/\1/Ig'
}

move_dir_unique() {
  local src="$1"
  local dst="$2"
  if [[ -e "$dst" ]]; then
    local ts
    ts="$(date +%Y%m%d_%H%M%S)"
    dst="${dst}_${ts}_$$"
  fi
  mv "$src" "$dst"
  echo "$dst"
}

has_audio() {
  local dir="$1"
  find "$dir" -type f \( \
    -iname '*.mp3' -o -iname '*.m4b' -o -iname '*.m4a' -o -iname '*.epub' -o -iname '*.mobi' \
  \) -print -quit | grep -q .
}

has_video() {
  local dir="$1"
  find "$dir" -type f \( -iname '*.mkv' -o -iname '*.mp4' \) -print -quit | grep -q .
}

# ----------------------------------------------------------
# NEW: Featurettes/Extras routing helper
# If a file lives under a directory named Featurettes/Extras/Bonus,
# route it to: <TV or Anime>/<Series>/seasonXX/featurettes/<Title>.mp4
#
# Returns 0 and echoes full OUT path if it can parse series+season.
# Returns 1 if not a featurette path or parsing fails (fallback to old logic).
# ----------------------------------------------------------
compute_featurette_out() {
  local file="$1"
  local base_title="$2"

  local cur feat_dir dirbase low next
  cur="$(dirname "$file")"
  feat_dir=""

  while :; do
    dirbase="$(basename "$cur")"
    low="$(echo "$dirbase" | tr '[:upper:]' '[:lower:]')"
    if [[ "$low" == "featurettes" || "$low" == "featurette" || "$low" == "extras" || "$low" == "bonus" || "$low" == "bonusfeatures" || "$low" == "bonus features" ]]; then
      feat_dir="$cur"
      break
    fi
    next="$(dirname "$cur")"
    [[ "$next" == "$cur" ]] && break
    cur="$next"
  done

  [[ -n "$feat_dir" ]] || return 1

  local show_dir show_base season_raw season series_part series tv_root title_clean dest out
  show_dir="$(dirname "$feat_dir")"
  show_base="$(basename "$show_dir")"

  # Season: prefer "S02" in the show dir name; fallback to "Season 2"
  season_raw="$(echo "$show_base" | sed -nE 's/.*[Ss]([0-9]{1,2}).*/\1/p' | head -1)"
  if [[ -z "$season_raw" ]]; then
    season_raw="$(echo "$show_base" | sed -nE 's/.*[Ss]eason[[:space:]]*([0-9]{1,2}).*/\1/p' | head -1)"
  fi
  [[ -n "$season_raw" ]] || return 1
  season="$(printf "%02d" "$((10#$season_raw))")"

  # Series name: strip year + everything from Sxx onward
  series_part="$(echo "$show_base" | sed -E 's/\([[:space:]]*(19|20)[0-9]{2}[[:space:]]*\)//g; s/\[[[:space:]]*(19|20)[0-9]{2}[[:space:]]*\]//g')"
  series_part="$(echo "$series_part" | sed -E 's/[Ss][0-9]{1,2}.*$//')"
  series_part="$(strip_release_junk "$series_part")"
  series="$(maybe_title_case "$(trim_dashes "$(sanitize_name "$series_part")")")"
  [[ -n "$series" ]] || return 1

  # Decide TV root (anime vs normal TV)
  if [[ "$series" =~ $ANIME_SHOW_REGEX ]]; then
    tv_root="$ANIME_DIR"
  else
    tv_root="$TV_DIR"
  fi

  # Clean featurette title from filename stem
  title_clean="$(strip_release_junk "$base_title")"
  title_clean="$(echo "$title_clean" | sed -E 's/[()]+//g')"
  title_clean="$(maybe_title_case "$(trim_dashes "$(sanitize_name "$title_clean")")")"
  [[ -n "$title_clean" ]] || title_clean="Featurette"

  dest="$tv_root/$series/season${season}/featurettes"
  out="$dest/$title_clean.mp4"
  echo "$out"
  return 0
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

  title="$(strip_release_junk "$title")"
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
    | sed -E 's/[()]+//g')"

  cleaned="$(strip_release_junk "$cleaned")"
  cleaned="$(maybe_title_case "$(sanitize_name "$cleaned")").mkv"

  if [[ "$base" != "$cleaned" ]]; then
    echo "RENAME | $base -> $cleaned" >> "$INGEST_LOG"
    mv "$file" "$dir/$cleaned"
  fi
}

############################################################
# ============ STEP -0.5: STAGE AUDIO DIRS ================
############################################################
# If someone drops a folder full of chapter MP3s/M4Bs into PlexDrop,
# move that whole folder into AUDIO_QUEUE so cleanup won't touch it.

stage_audio_dirs_from_input() {
  # Only look at immediate subdirs of INPUT_DIR (not recursion)
  # Skip known internal dirs.
  find "$INPUT_DIR" -maxdepth 1 -type d -mindepth 1 -print0 \
    | while IFS= read -r -d '' d; do
        b="$(basename "$d")"
        [[ "$b" == "$RAW_DIR_NAME" ]] && continue
        [[ "$b" == "logs" ]] && continue
        [[ "$b" == "$(basename "$AUDIO_QUEUE")" ]] && continue

        # audio-only folder? move it
        if has_audio "$d" && ! has_video "$d"; then
          echo "AUDIO DIR DETECTED | moving to queue: $d"
          echo "AUDIO DIR DETECTED | moving to queue: $d" >> "$INGEST_LOG"
          move_dir_unique "$d" "$AUDIO_QUEUE/$b" >/dev/null
        fi
      done
}

stage_audio_dirs_from_input

############################################################
# ================= STEP 0: STAGING ========================
############################################################

echo "Staging files into $RAW_DIR_NAME/"
mkdir -p "$RAW_DIR"

# Move loose audio files straight into AUDIO_QUEUE so RAW cleanup won't remove them
shopt -s nullglob
for f in "$INPUT_DIR"/*.mp3 "$INPUT_DIR"/*.m4b "$INPUT_DIR"/*.m4a "$INPUT_DIR"/*.epub "$INPUT_DIR"/*.mobi; do
  [[ -f "$f" ]] || continue
  echo "AUDIO FILE DETECTED | moving to queue: $f"
  echo "AUDIO FILE DETECTED | moving to queue: $f" >> "$INGEST_LOG"
  mv "$f" "$AUDIO_INCOMING_FILES/" 2>/dev/null || true
done
shopt -u nullglob

# Stage video inputs / zips into RAW_DIR
shopt -s nullglob
for f in "$INPUT_DIR"/*.mkv "$INPUT_DIR"/*.mp4 "$INPUT_DIR"/*.zip; do
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

  # NEW: If this ZIP produced audio-only content, move it to AUDIO_QUEUE immediately
  if [[ -d "$DEST_DIR" ]] && has_audio "$DEST_DIR" && ! has_video "$DEST_DIR"; then
    echo "AUDIO ZIP DETECTED | preserving extracted audio: $(basename "$ZIP")"
    echo "AUDIO ZIP DETECTED | preserving extracted audio: $(basename "$ZIP")" >> "$INGEST_LOG"

    moved_to="$(move_dir_unique "$DEST_DIR" "$AUDIO_QUEUE/$(basename "$DEST_DIR")")"
    echo "AUDIO MOVED | $moved_to"
    echo "AUDIO MOVED | $moved_to" >> "$INGEST_LOG"

    if [[ "$rc" -le 1 ]]; then
      echo "AUDIO ZIP OK/WARN | moving zip to: $AUDIO_SOURCE_ZIPS"
      echo "AUDIO ZIP OK/WARN | moving zip to: $AUDIO_SOURCE_ZIPS" >> "$INGEST_LOG"
      mv "$ZIP" "$AUDIO_SOURCE_ZIPS/" 2>/dev/null || true
    else
      echo "AUDIO ZIP UNZIP ERROR (exit=$rc) | moving zip to: $AUDIO_FAILED_ZIPS"
      echo "AUDIO ZIP UNZIP ERROR (exit=$rc) | moving zip to: $AUDIO_FAILED_ZIPS" >> "$INGEST_LOG"
      mv "$ZIP" "$AUDIO_FAILED_ZIPS/" 2>/dev/null || true
    fi

    # Do NOT touch the normal video marker logic for audio zips (we moved the whole dir out)
    continue
  fi

  # Normal (video) behavior
  if [[ "$rc" -le 1 ]]; then
    touch "$MARKER"
  else
    echo "WARNING: unzip failed for $(basename "$ZIP") (exit=$rc). Will retry next run."
    echo "WARNING: unzip failed for $(basename "$ZIP") (exit=$rc)" >> "$INGEST_LOG"
    # leave NO marker so it retries later
  fi
done

############################################################
# ================= STEP 2: RENAME MKVS ====================
############################################################

# Rename every MKV anywhere under RAW_DIR (so reruns “start over” cleanly)
# TV detection upgraded to also catch: S01 E01, S01-E01, S01_E01, S01.E01 (etc.)
# AND case-insensitive s/e.
while IFS= read -r -d '' f; do
  base="$(basename "$f")"
  if [[ "$base" =~ [Ss][0-9]{1,2}[[:space:]_.-]*[Ee][0-9]{1,2} ]]; then
    clean_mkv_name_tv "$f"
  else
    clean_mkv_name_movie "$f"
  fi
done < <(find "$RAW_DIR" -type f -name '*.mkv' -print0)

############################################################
# =========== STEP 2.5: INGEST PRE-ENCODED MP4 ============
############################################################

ingest_mp4_files() {
  while IFS= read -r -d '' FILE; do
    [[ -f "$FILE" ]] || continue

    BASENAME="$(basename "$FILE" .mp4)"

    # NEW: Featurettes/Extras routing (pre-encoded mp4)
    if OUT_F="$(compute_featurette_out "$FILE" "$BASENAME" 2>/dev/null)"; then
      DEST_F="$(dirname "$OUT_F")"
      mkdir -p "$DEST_F"

      if [[ -f "$OUT_F" && -s "$OUT_F" ]]; then
        echo "SKIP (exists): $OUT_F"
        echo "SKIP (exists): $OUT_F" >> "$INGEST_LOG"
        continue
      fi

      echo "MOVE FEATURETTE MP4 | $FILE -> $OUT_F"
      echo "MOVE FEATURETTE MP4 | $FILE -> $OUT_F" >> "$INGEST_LOG"

      TMP="$OUT_F.partial"
      rm -f "$TMP" 2>/dev/null || true
      mv "$FILE" "$TMP"
      mv "$TMP" "$OUT_F"
      continue
    fi

    # Normalize common TV patterns like:
    #   "S01 E01", "S01-E01", "S01_E01", "S01.E01" -> "S01E01" (case-insensitive)
    BASENAME_TV="$(echo "$BASENAME" | sed -E 's/[Ss]([0-9]{1,2})[[:space:]_.-]*[Ee]([0-9]{1,2})/S\1E\2/g')"

    if [[ "$BASENAME_TV" =~ [Ss]([0-9]{1,2})[Ee]([0-9]{1,2}) ]]; then
      SEASON_RAW="${BASH_REMATCH[1]}"
      EPISODE_RAW="${BASH_REMATCH[2]}"

      SEASON="$(printf "%02d" "$((10#$SEASON_RAW))")"
      EPISODE="$(printf "%02d" "$((10#$EPISODE_RAW))")"

      SERIES="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV%%S${SEASON_RAW}E${EPISODE_RAW}*}")")")"
      TITLE="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV#*S${SEASON_RAW}E${EPISODE_RAW}}" | sed 's/[()]+//g')")")"

      # FIX: if the “episode title” is just a release tag like DUAL, drop it
      if [[ "$TITLE" =~ ^[Dd][Uu][Aa][Ll]$ ]]; then
        TITLE=""
      fi

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
      # Movie MP4
      norm="$(echo "$BASENAME" | sed 's/[._]/ /g')"
      YEAR="$(echo "$norm" | grep -oE '(19|20)[0-9]{2}' | head -1 || true)"

      if [[ -n "${YEAR:-}" ]]; then
        TITLE_RAW="$(echo "$norm" | sed -E "s/[[:space:]]*\(?$YEAR\)?.*//")"
      else
        TITLE_RAW="$norm"
      fi

      TITLE_RAW="$(strip_release_junk "$TITLE_RAW")"
      TITLE="$(maybe_title_case "$(sanitize_name "$TITLE_RAW")")"

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

    echo "MOVE MP4 | $FILE -> $OUT"
    echo "MOVE MP4 | $FILE -> $OUT" >> "$INGEST_LOG"

    TMP="$OUT.partial"
    rm -f "$TMP" 2>/dev/null || true
    mv "$FILE" "$TMP"
    mv "$TMP" "$OUT"
  done < <(find "$RAW_DIR" -type f -name '*.mp4' ! -name '*.mp4.partial' -print0)
}

ingest_mp4_files

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

    # NEW: Featurettes/Extras routing (HandBrake)
    if OUT_F="$(compute_featurette_out "$FILE" "$BASENAME" 2>/dev/null)"; then
      DEST_F="$(dirname "$OUT_F")"
      mkdir -p "$DEST_F"

      if [[ -f "$OUT_F" && -s "$OUT_F" ]]; then
        echo "SKIP (exists): $OUT_F"
        echo "SKIP (exists): $OUT_F" >> "$INGEST_LOG"
        continue
      fi

      TMP="$OUT_F.partial"
      rm -f "$TMP" 2>/dev/null || true

      if [[ "$TAG" == "4K" ]]; then
        CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="HQ 2160p60 4K HEVC Surround" -q 20 )
      else
        CMD=( HandBrakeCLI -i "$FILE" -o "$TMP" --preset="$PRESET_1080P" --format av_mp4 --optimize )
      fi

      echo "RUNNING | ${CMD[*]}"
      echo "RUNNING | ${CMD[*]}" >> "$INGEST_LOG"

      "${CMD[@]}" </dev/null
      mv "$TMP" "$OUT_F"
      continue
    fi

    # Normalize common TV patterns like:
    #   "S01 E01", "S01-E01", "S01_E01", "S01.E01" -> "S01E01" (case-insensitive)
    BASENAME_TV="$(echo "$BASENAME" | sed -E 's/[Ss]([0-9]{1,2})[[:space:]_.-]*[Ee]([0-9]{1,2})/S\1E\2/g')"

    if [[ "$BASENAME_TV" =~ [Ss]([0-9]{1,2})[Ee]([0-9]{1,2}) ]]; then
      SEASON_RAW="${BASH_REMATCH[1]}"
      EPISODE_RAW="${BASH_REMATCH[2]}"

      SEASON="$(printf "%02d" "$((10#$SEASON_RAW))")"
      EPISODE="$(printf "%02d" "$((10#$EPISODE_RAW))")"

      SERIES="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV%%S${SEASON_RAW}E${EPISODE_RAW}*}")")")"
      TITLE="$(maybe_title_case "$(trim_dashes "$(sanitize_name "${BASENAME_TV#*S${SEASON_RAW}E${EPISODE_RAW}}" | sed 's/[()]+//g')")")"

      # FIX: if the “episode title” is just a release tag like DUAL, drop it
      if [[ "$TITLE" =~ ^[Dd][Uu][Aa][Ll]$ ]]; then
        TITLE=""
      fi

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
# ============ STEP 3.5: OPTIONAL AUDIO PIPELINE ===========
############################################################

if [[ "$RUN_AUDIO_PIPELINE" -eq 1 ]]; then
  if [[ -f "$AUDIO_PIPELINE" ]]; then
    echo "AUDIO PIPELINE | running: $AUDIO_PIPELINE $AUDIO_QUEUE"
    echo "AUDIO PIPELINE | running: $AUDIO_PIPELINE $AUDIO_QUEUE" >> "$INGEST_LOG"
    set +e
    /usr/bin/python3 "$AUDIO_PIPELINE" "$AUDIO_QUEUE"
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      echo "AUDIO PIPELINE | WARNING: exited non-zero ($rc). Leaving audio queue in place."
      echo "AUDIO PIPELINE | WARNING: exited non-zero ($rc). Leaving audio queue in place." >> "$INGEST_LOG"
    fi
  else
    echo "AUDIO PIPELINE | not found at $AUDIO_PIPELINE (skipping)"
    echo "AUDIO PIPELINE | not found at $AUDIO_PIPELINE (skipping)" >> "$INGEST_LOG"
  fi
fi

############################################################
# ================= STEP 4: CLEANUP ========================
############################################################

if [[ "$KEEP_RAW" -eq 0 ]]; then
  rm -rf "$RAW_DIR"
fi
echo "Ingest complete."

