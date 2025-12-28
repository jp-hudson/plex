#!/usr/bin/env bash
set -euo pipefail

############################
# CONFIG
############################

INPUT_DIR="${1:-/Users/jhudson/Downloads/}"
TMP_PREFIX=".unzip_"

PY_RENAMER="/Users/jhudson/bin/moviev1.py"   # <-- adjust path
PLEX_SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

############################
#  Logging
############################

LOG_DIR="$INPUT_DIR/logs"
LOG_FILE="$LOG_DIR/plex_ingest.log"

mkdir -p "$LOG_DIR"

# Timestamp every line, log to file + console
exec > >(awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0; fflush(); }' | tee -a "$LOG_FILE") 2>&1

############################
# PRE-FLIGHT CHECKS
############################

command -v unzip >/dev/null || { echo "unzip not found"; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found"; exit 1; }
command -v HandBrakeCLI >/dev/null || { echo "HandBrakeCLI not found"; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }

############################
# STEP 1: UNZIP ANY ZIP FILES
############################

for ZIP in "$INPUT_DIR"/*.zip; do
  [[ -e "$ZIP" ]] || continue

  BASENAME=$(basename "$ZIP" .zip)
  TMP_DIR="$INPUT_DIR/${TMP_PREFIX}${BASENAME}"

  if [[ -d "$TMP_DIR" ]]; then
    echo "Already unzipped: $ZIP"
    continue
  fi

  echo "Unzipping: $ZIP → $TMP_DIR"
  mkdir -p "$TMP_DIR"
  unzip -q "$ZIP" -d "$TMP_DIR"
done

############################
# STEP 2: RUN PYTHON RENAMER
############################

echo "Normalizing MKV filenames..."

# Original input dir
python3 "$PY_RENAMER" "$INPUT_DIR"

# Any unzip temp dirs
for DIR in "$INPUT_DIR"/${TMP_PREFIX}*; do
  [[ -d "$DIR" ]] || continue
  python3 "$PY_RENAMER" "$DIR"
done

############################
# STEP 3: RUN PLEX HANDBRAKE LOGIC
############################

echo "Starting Plex encode pipeline..."

run_handbrake_on_dir() {
  local SRC_DIR="$1"

  for FILE in "$SRC_DIR"/*.mkv; do
    [[ -e "$FILE" ]] || continue

    # Export INPUT_DIR so your existing script logic works unchanged
    INPUT_DIR="$SRC_DIR" \
      bash "$PLEX_SCRIPT_DIR/plex_handbrake.sh"
    break
  done
}

# Original input dir
run_handbrake_on_dir "$INPUT_DIR"

# Any unzip temp dirs
for DIR in "$INPUT_DIR"/${TMP_PREFIX}*; do
  [[ -d "$DIR" ]] || continue
  run_handbrake_on_dir "$DIR"
done

############################
# STEP 4: CLEANUP
############################

echo "Cleaning up temp unzip directories..."

for DIR in "$INPUT_DIR"/${TMP_PREFIX}*; do
  [[ -d "$DIR" ]] || continue
  rm -rf "$DIR"
done

echo "Pipeline complete."

