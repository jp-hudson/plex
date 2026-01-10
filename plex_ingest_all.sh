#!/usr/bin/env bash
set -euo pipefail

DROP="${1:-/Users/jhudson/PlexDrop}"

# Run your existing (working) video pipeline unchanged
/Users/jhudson/code/plex/plex_pipeline.sh "$DROP"

# Then process audiobooks left in the DROP folder
/usr/bin/python3 /Users/jhudson/code/plex/audiobook_pipeline.py "$DROP"

