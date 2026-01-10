#!/usr/bin/env python3
import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIG
# ============================================================

DEFAULT_SOURCE = "/Users/jhudson/PlexDrop"
DEFAULT_DEST = "/Volumes/Media/Audiobooks"

AUDIO_EXTS = {".mp3", ".m4b", ".m4a"}

# Words/phrases commonly found in release/folder names that are NOT part of the title
JUNK_PHRASES = [
    "audiobook", "audiobooks", "full-cast", "full cast", "fullcast",
    "split chapters", "mp3 split chapters", "stereo", "mono",
    "unabridged", "abridged", "complete", "collection",
    "freepaidbooks", "freepaidbooks.online", "tgx", "rartv",
    "webrip", "web-dl", "web dl", "web", "bluray", "bdrip", "hdtv",
    "x264", "x265", "h264", "h265", "hevc", "av1",
    "ddp", "eac3", "aac", "atmos", "esub", "subs", "subtitles",
    "1080p", "720p", "2160p", "10bit", "8bit",
]

# If Open Library is down/offline, we fall back to metadata/Unknown Author
OPENLIB_TIMEOUT_SECS = 6

# ============================================================
# HELPERS
# ============================================================

def log(msg: str) -> None:
    print(msg, flush=True)

def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def safe_component(s: str) -> str:
    """
    Make a string safe for use as a single path component (folder name).
    """
    s = s.strip()
    # Replace path separators and weird chars
    s = s.replace("/", " ").replace("\\", " ")
    s = re.sub(r"[:*?\"<>|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "Unknown"

def sanitize_spaces(s: str) -> str:
    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def strip_brackets(s: str) -> str:
    # Remove [blah] blocks often used by sites/groups
    return re.sub(r"\[[^\]]+\]", " ", s)

def strip_parens_year(s: str) -> Tuple[str, Optional[str]]:
    """
    Remove (2025) or 2025, return (cleaned, year)
    """
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        year = m.group(0)
    # Remove parenthesized year blocks like "(2025)"
    s2 = re.sub(r"\(\s*(19|20)\d{2}\s*\)", " ", s)
    return s2, year

def strip_junk_phrases(s: str) -> str:
    s_low = s.lower()
    # Remove obvious junk phrases
    for phrase in JUNK_PHRASES:
        if phrase in s_low:
            # remove phrase case-insensitively
            s = re.sub(re.escape(phrase), " ", s, flags=re.IGNORECASE)
            s_low = s.lower()
    return s

def clean_title_text(s: str) -> Tuple[str, Optional[str], str]:
    """
    Returns (title_clean, year, hint_text)
    hint_text is extra context from original string that can help lookups (e.g., "Witcher")
    """
    original = s

    s = strip_brackets(s)
    s = sanitize_spaces(s)
    s, year = strip_parens_year(s)

    # Remove "The Witcher, Book 3" style suffixes (keep title itself)
    # ex: "Baptism of Fire The Witcher, Book 3 (Unabridged)"
    s = re.sub(r"\bthe witcher\s*,?\s*book\s*\d+\b", " ", s, flags=re.IGNORECASE)

    # Remove generic "Book 3" "Book 03" segments if present
    s = re.sub(r"\bbook\s*\d+\b", " ", s, flags=re.IGNORECASE)

    # Remove codec-ish stuff / junk phrases
    s = strip_junk_phrases(s)

    # Remove trailing dashes/extra separators
    s = re.sub(r"[-–—]+", " ", s)

    # Final collapse
    s = re.sub(r"\s+", " ", s).strip()

    # Hint for lookups: if original contains "witcher" or "harry potter", preserve as hint
    hint = ""
    if re.search(r"\bwitcher\b", original, re.IGNORECASE):
        hint = "witcher"
    elif re.search(r"\bharry potter\b", original, re.IGNORECASE):
        hint = "harry potter"

    return s, year, hint

def looks_like_author_name(s: str) -> bool:
    """
    Conservative: only return True if it really looks like a human name.
    Prevents "Cybersecurity All-in-One..." being treated as an author.
    """
    s = s.strip()
    if not s:
        return False
    if re.search(r"\d", s):
        return False

    tokens = s.split()
    if len(tokens) < 2 or len(tokens) > 4:
        return False

    if s.lower().startswith(("the ", "a ", "an ")):
        return False

    for t in tokens:
        # initials like "J."
        if re.fullmatch(r"[A-Z]\.", t):
            continue
        # names like O'Connor, Smith-Jones
        if re.fullmatch(r"[A-Z][a-z]+(?:[\'-][A-Z]?[a-z]+)*", t):
            continue
        return False

    return True

def parse_title_author_from_text(text: str) -> Tuple[str, Optional[str]]:
    """
    Extract a *book title* and (optional) author from a filename/folder string.

    Priority:
      1) "Title by Author"
      2) "Author - Title" ONLY if left side looks like a human name
      3) otherwise: treat whole as title, author None (force lookup)
    """
    t = text.strip()

    # Strong signal: "Title by Author"
    m = re.search(r"\s+by\s+", t, flags=re.IGNORECASE)
    if m:
        title, author = re.split(r"\s+by\s+", t, maxsplit=1, flags=re.IGNORECASE)
        return title.strip(), author.strip()

    # ONLY treat "Author - Title" if author looks like a name
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()]
    if len(parts) >= 2 and looks_like_author_name(parts[0]):
        author = parts[0]
        title = " - ".join(parts[1:])
        return title.strip(), author.strip()

    return t, None

def ffprobe_tags(path: Path) -> Dict[str, str]:
    """
    Try to get metadata tags from an audio file.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format_tags=title,artist,album,album_artist,composer",
        "-of", "json",
        str(path),
    ]
    rc, out, _ = run(cmd)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
        tags = (data.get("format", {}) or {}).get("tags", {}) or {}
        # Normalize keys to lowercase
        return {k.lower(): str(v).strip() for k, v in tags.items() if str(v).strip()}
    except Exception:
        return {}

def openlibrary_lookup_author(title: str, hint: str = "") -> Optional[str]:
    """
    Lookup author using Open Library (no key).
    Returns first author name from best match.
    """
    q = title.strip()
    if hint:
        q = f"{q} {hint}".strip()

    if not q:
        return None

    url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode({
        "q": q,
        "limit": "10",
    })

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "audiobook_ingest/1.0"})
        with urllib.request.urlopen(req, timeout=OPENLIB_TIMEOUT_SECS) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except Exception:
        return None

    docs = data.get("docs") or []
    if not docs:
        return None

    # pick best match by title similarity (if doc title exists)
    def score(doc) -> float:
        dt = (doc.get("title") or "").strip()
        if not dt:
            return 0.0
        return difflib.SequenceMatcher(None, title.lower(), dt.lower()).ratio()

    docs_sorted = sorted(docs, key=score, reverse=True)

    for d in docs_sorted[:5]:
        authors = d.get("author_name") or []
        if authors:
            return str(authors[0]).strip()

    return None

def ensure_unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = Path(str(path) + f" ({i})")
        if not cand.exists():
            return cand
    return Path(str(path) + f" ({int(time.time())})")

def move_file(src: Path, dst_dir: Path, dry_run: bool) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / src.name
    if dst_path.exists():
        # avoid overwrite
        dst_path = dst_dir / f"{src.stem} (2){src.suffix}"
    log(f"MOVE: {src} -> {dst_path}")
    if not dry_run:
        shutil.move(str(src), str(dst_path))

def maybe_rename_single_file(src: Path, dst_dir: Path, title: str, author: str, rename: bool, dry_run: bool) -> None:
    """
    If rename enabled, rename to: "<Title> by <Author><ext>"
    Otherwise keep original filename.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    if not rename:
        move_file(src, dst_dir, dry_run)
        return

    new_name = f"{title} by {author}{src.suffix}"
    new_name = safe_component(new_name)  # sanitize component-ish chars
    dst_path = dst_dir / new_name
    if dst_path.exists():
        dst_path = dst_dir / f"{title} by {author} (2){src.suffix}"
    log(f"MOVE+RENAME: {src} -> {dst_path}")
    if not dry_run:
        shutil.move(str(src), str(dst_path))

def gather_audio_files(d: Path) -> List[Path]:
    out = []
    for p in d.iterdir():
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            out.append(p)
    return sorted(out)

def is_chapter_folder(mp3s: List[Path]) -> bool:
    """
    Heuristic for folders like:
      01 - Opening Credits.mp3
      02 - The Worst Birthday.mp3
    """

