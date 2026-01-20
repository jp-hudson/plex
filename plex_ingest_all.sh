#!/usr/bin/env bash
set -euo pipefail

DROP="${1:-/Users/jhudson/PlexDrop}"

# Video pipeline (unchanged)
/Users/jhudson/code/plex/plex_pipeline.sh "$DROP"

# NEW: siphon music first (prevents mp3 albums from being ingested as audiobooks)
/usr/bin/python3 /Users/jhudson/code/plex/music_pipeline.py "$DROP"

# Audiobooks next
/usr/bin/python3 /Users/jhudson/code/plex/audiobook_pipeline.py "$DROP"
