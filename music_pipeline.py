#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================
# CONFIG
# ============================================================

DEFAULT_SOURCE = "/Users/jhudson/PlexDrop"
DEFAULT_DEST = "/Volumes/Media/Music"

# Music formats (intentionally excludes .m4b which is strongly “audiobook”)
MUSIC_EXTS = {
    ".mp3", ".flac", ".m4a", ".aac", ".alac", ".wav", ".aiff", ".ogg", ".opus"
}

SIDE_EXTS = {
    ".jpg", ".jpeg", ".png",
    ".nfo", ".cue", ".m3u", ".m3u8", ".txt",
    ".log", ".sfv"
}

DUPLICATES_DIRNAME = "_duplicates"

# Music-ish tokens that often appear in release folder names
MUSIC_JUNK_TOKENS = [
    "eac-flac", "eac flac", "flac", "mp3", "320kbps", "kbps",
    "hi res", "hi-res", "24-192", "24 192", "24bit", "16bit",
    "remastered", "deluxe", "edition",
    "web", "webrip", "web-dl", "web dl",
]

# If you drop a folder like "Artist - Album ..." this helps split it
ARTIST_ALBUM_SPLIT_RE = re.compile(r"\s+-\s+")

TRACKNUM_RE = re.compile(r"^\s*\d{1,3}([ ._-].*)?$")  # "01", "01.", "01 -", etc.
DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|part|pt|vol|volume)\s*0*\d{1,3}\b", re.IGNORECASE)

# duration thresholds (seconds) used as a “music vs audiobook” hint
MUSICISH_MEDIAN_MAX = 12 * 60     # <= 12 minutes median -> likely music
AUDIOBOOKISH_MEDIAN_MIN = 20 * 60 # >= 20 minutes median -> likely audiobook

# ============================================================
# HELPERS
# ============================================================

def log(msg: str) -> None:
    print(msg, flush=True)

def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def safe_component(s: str) -> str:
    s = (s or "").strip()
    s = s.replace("/", " ").replace("\\", " ")
    s = re.sub(r"[:*?\"<>|]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "Unknown"

def sanitize_spaces(s: str) -> str:
    s = s.replace(".", " ").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def strip_brackets(s: str) -> str:
    return re.sub(r"\[[^\]]+\]", " ", s)

def strip_parens_blocks(s: str) -> str:
    # remove parenthetical blocks that are usually metadata: (Remastered 2023 - Hi Res), (2023 Rock), etc.
    return re.sub(r"\([^)]*\)", " ", s)

def strip_music_junk(s: str) -> str:
    out = s
    for tok in sorted(MUSIC_JUNK_TOKENS, key=len, reverse=True):
        out = re.sub(re.escape(tok), " ", out, flags=re.IGNORECASE)
    # remove bitrate patterns like "24-192" or "24 192"
    out = re.sub(r"\b\d{1,2}\s*[- ]\s*\d{2,3}\b", " ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out

def looks_like_artist(s: str) -> bool:
    # more permissive than “author” rules; allows 1-token artists like “Korn”, “Killers”
    s = (s or "").strip()
    if not s:
        return False
    if re.search(r"\d{4}", s):
        return False
    if len(s) > 80:
        return False
    # avoid obviously-non-artist folder labels
    if s.lower() in {"cd1", "cd2", "disc1", "disc2", "various"}:
        return False
    return True

def ffprobe_tags(path: Path) -> Dict[str, str]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format_tags=title,artist,album,album_artist,composer,genre,track,disc",
        "-of", "json",
        str(path),
    ]
    rc, out, _ = run(cmd)
    if rc != 0 or not out.strip():
        return {}
    try:
        data = json.loads(out)
        tags = (data.get("format", {}) or {}).get("tags", {}) or {}
        return {k.lower(): str(v).strip() for k, v in tags.items() if str(v).strip()}
    except Exception:
        return {}

def ffprobe_duration_seconds(path: Path) -> Optional[float]:
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]
    rc, out, _ = run(cmd)
    if rc != 0:
        return None
    try:
        v = float(out.strip())
        return v if v > 0 else None
    except Exception:
        return None

def gather_media_recursive(d: Path) -> List[Path]:
    out: List[Path] = []
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in MUSIC_EXTS:
            out.append(p)
    return sorted(out)

def gather_sidecars_recursive(d: Path) -> List[Path]:
    out: List[Path] = []
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in SIDE_EXTS:
            out.append(p)
    return sorted(out)

def ensure_unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = Path(str(path) + f" ({i})")
        if not cand.exists():
            return cand
    return Path(str(path) + f" ({int(time.time())})")

def move_to_duplicates(src_path: Path, src_root: Path, dry_run: bool) -> None:
    dup_root = src_root / DUPLICATES_DIRNAME
    dup_root.mkdir(parents=True, exist_ok=True)
    target = ensure_unique_dir(dup_root / src_path.name)
    log(f"DUPLICATE -> {target}")
    if dry_run:
        return
    try:
        shutil.move(str(src_path), str(target))
    except Exception as e:
        log(f"WARNING: failed moving to duplicates: {src_path} -> {target} ({e})")

def remove_empty_dirs(root: Path, dry_run: bool) -> None:
    for d in sorted([p for p in root.rglob("*") if p.is_dir()], key=lambda p: len(str(p)), reverse=True):
        try:
            if any(d.iterdir()):
                continue
            if d.name == DUPLICATES_DIRNAME:
                continue
            log(f"RMDIR: {d}")
            if not dry_run:
                d.rmdir()
        except Exception:
            pass

def track_number_ratio(files: List[Path]) -> float:
    if not files:
        return 0.0
    hits = 0
    for f in files:
        stem = sanitize_spaces(f.stem)
        if TRACKNUM_RE.match(stem):
            hits += 1
        # also treat "01. Artist - Song" as track-numbered
        elif re.match(r"^\s*\d{1,3}\s*[.\-_ ]\s*", f.name):
            hits += 1
    return hits / max(1, len(files))

def median_duration(files: List[Path], max_samples: int = 5) -> Optional[float]:
    if not files:
        return None
    samples = files[:max_samples]
    durs: List[float] = []
    for f in samples:
        dur = ffprobe_duration_seconds(f)
        if dur is not None:
            durs.append(dur)
    if not durs:
        return None
    durs.sort()
    mid = len(durs) // 2
    return durs[mid] if len(durs) % 2 == 1 else (durs[mid - 1] + durs[mid]) / 2.0

def looks_like_music_dir(dir_path: Path, media_files: List[Path], sidecars: List[Path]) -> bool:
    # scoring approach: we only classify as music when we have enough evidence.
    score = 0

    # strong signals
    if any(f.suffix.lower() in {".flac", ".wav", ".aiff"} for f in media_files):
        score += 3

    if any(s.suffix.lower() in {".m3u", ".m3u8", ".cue", ".nfo"} for s in sidecars):
        score += 2

    name_l = dir_path.name.lower()
    if any(tok in name_l for tok in ["flac", "eac", "320", "kbps", "hi res", "hi-res", "remastered"]):
        score += 2

    if ARTIST_ALBUM_SPLIT_RE.search(dir_path.name):
        # "Artist - Album" pattern
        score += 2

    # track pattern
    tr = track_number_ratio(media_files)
    if len(media_files) >= 2 and tr >= 0.6:
        score += 2

    # duration heuristic (helps avoid audiobook mp3 chapter folders)
    md = median_duration(media_files)
    if md is not None:
        if md <= MUSICISH_MEDIAN_MAX:
            score += 2
        elif md >= AUDIOBOOKISH_MEDIAN_MIN:
            score -= 2

    # negative signal: audiobook container
    if any(f.suffix.lower() == ".m4b" for f in media_files):
        score -= 3

    # negative signal: audiobook-ish folder words
    if any(w in name_l for w in ["audiobook", "unabridged", "abridged", "narrated", "narrator"]):
        score -= 2

    return score >= 3

def parse_artist_album_from_dirname(name: str) -> Tuple[Optional[str], Optional[str]]:
    raw = name.strip()
    if not raw:
        return None, None

    # remove obvious metadata wrappers
    s = sanitize_spaces(strip_brackets(raw))
    # keep year if you want it later, but strip parenthetical blobs first
    s = strip_parens_blocks(s)
    s = strip_music_junk(s)

    # split "Artist - Album"
    if ARTIST_ALBUM_SPLIT_RE.search(s):
        parts = ARTIST_ALBUM_SPLIT_RE.split(s, maxsplit=1)
        if len(parts) == 2:
            artist = parts[0].strip()
            album = parts[1].strip()
            if looks_like_artist(artist) and album:
                return artist, album

    # fallback: if no split, treat whole as album (unknown artist)
    return None, s.strip() or None

def pick_artist_album_from_tags(files: List[Path]) -> Tuple[Optional[str], Optional[str]]:
    # look at first few media files and see if tags are present
    for f in files[:5]:
        tags = ffprobe_tags(f)
        album = tags.get("album")
        artist = tags.get("album_artist") or tags.get("artist")
        if album or artist:
            return artist, album
    return None, None

def move_file_preserve_rel(src_file: Path, base_dir: Path, dst_base: Path, src_root: Path, dry_run: bool) -> None:
    rel = src_file.relative_to(base_dir)
    dst_path = dst_base / rel
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    if dst_path.exists():
        log(f"SKIP (exists): {dst_path}")
        move_to_duplicates(src_file, src_root, dry_run)
        return

    log(f"MOVE: {src_file} -> {dst_path}")
    if not dry_run:
        shutil.move(str(src_file), str(dst_path))

def process_music_dir(dir_path: Path, dest_root: Path, dry_run: bool, src_root: Path) -> None:
    media_files = gather_media_recursive(dir_path)
    if not media_files:
        return
    sidecars = gather_sidecars_recursive(dir_path)

    # Determine artist/album
    tag_artist, tag_album = pick_artist_album_from_tags(media_files)
    name_artist, name_album = parse_artist_album_from_dirname(dir_path.name)

    artist = tag_artist or name_artist or "Unknown Artist"
    album = tag_album or name_album or "Unknown Album"

    artist = safe_component(artist)
    album = safe_component(album)

    dst_dir = Path(dest_root) / artist / album

    log(f"MUSIC: '{artist}' | ALBUM: '{album}'")
    log(f"DEST:  {dst_dir}")

    # Move files preserving disc subdirs / structure
    for f in media_files:
        move_file_preserve_rel(f, dir_path, dst_dir, src_root, dry_run)
    for s in sidecars:
        move_file_preserve_rel(s, dir_path, dst_dir, src_root, dry_run)

def process_single_music_file(f: Path, dest_root: Path, dry_run: bool, src_root: Path) -> None:
    tags = ffprobe_tags(f)
    artist = tags.get("album_artist") or tags.get("artist") or "Unknown Artist"
    album = tags.get("album") or "Singles"

    # If no useful tags, infer artist/title from filename: "Artist - Title"
    stem = sanitize_spaces(strip_brackets(f.stem))
    stem = strip_music_junk(strip_parens_blocks(stem))
    if (artist == "Unknown Artist" or not artist.strip()) and " - " in stem:
        parts = stem.split(" - ", 1)
        if len(parts) == 2 and looks_like_artist(parts[0].strip()):
            artist = parts[0].strip()

    artist = safe_component(artist)
    album = safe_component(album)

    dst_dir = Path(dest_root) / artist / album
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / f.name

    log(f"MUSIC FILE: '{f.name}' -> {dst_dir}")

    if dst_path.exists():
        log(f"SKIP (exists): {dst_path}")
        move_to_duplicates(f, src_root, dry_run)
        return

    log(f"MOVE: {f} -> {dst_path}")
    if not dry_run:
        shutil.move(str(f), str(dst_path))

def process_path(p: Path, dest_root: Path, dry_run: bool, src_root: Path) -> None:
    if p.name.startswith("."):
        return
    if p.name in {DUPLICATES_DIRNAME, "_failed_zips", "_incoming_files", "_source_zips"}:
        return

    # single music file
    if p.is_file() and p.suffix.lower() in MUSIC_EXTS:
        process_single_music_file(p, dest_root, dry_run, src_root)
        return

    if not p.is_dir():
        return

    media_files = gather_media_recursive(p)
    if not media_files:
        # recurse into subdirs
        for d in sorted([d for d in p.iterdir() if d.is_dir()]):
            process_path(d, dest_root, dry_run, src_root)
        return

    sidecars = gather_sidecars_recursive(p)

    if looks_like_music_dir(p, media_files, sidecars):
        process_music_dir(p, dest_root, dry_run, src_root)
        return

    # Not confident it's music -> recurse, so we can catch nested album dirs
    for d in sorted([d for d in p.iterdir() if d.is_dir()]):
        process_path(d, dest_root, dry_run, src_root)

def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest music into /Volumes/Media/Music/<Artist>/<Album>/")
    ap.add_argument("--source", default=None, help="Source root (PlexDrop). Will also scan audiobook_queue if present.")
    ap.add_argument("--dest", default=None, help="Destination root for music library.")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without moving files.")
    ap.add_argument("positional_source", nargs="?", help="(legacy) source path")

    args = ap.parse_args()
    src_str = args.source or args.positional_source or DEFAULT_SOURCE
    dest_str = args.dest or DEFAULT_DEST

    base_src = Path(src_str).expanduser()
    dest = Path(dest_str).expanduser()

    if not base_src.exists():
        log(f"ERROR: source does not exist: {base_src}")
        return 2

    # We scan:
    #   1) audiobook_queue (because plex_pipeline stages audio-only folders there)
    #   2) base PlexDrop too (for flac-only folders that never got staged)
    roots: List[Path] = []
    queue = base_src / "audiobook_queue"
    if queue.exists() and queue.is_dir():
        roots.append(queue)
    roots.append(base_src)

    # avoid double-processing if base_src == queue, etc.
    seen: set[str] = set()

    log(f"SOURCE: {base_src}")
    log(f"DEST:   {dest}")
    log(f"DRYRUN: {args.dry_run}")
    log(f"ROOTS:  {', '.join(str(r) for r in roots)}")

    for root in roots:
        key = str(root.resolve())
        if key in seen:
            continue
        seen.add(key)

        # Only process immediate children of each root (keeps it “minimally invasive”)
        for item in sorted(root.iterdir()):
            # Skip known heavy/irrelevant dirs at the top level
            if root == base_src and item.name in {"handbrake_raw", "logs", "audiobook_queue"}:
                continue
            process_path(item, dest, args.dry_run, src_root=root)

        remove_empty_dirs(root, args.dry_run)

    log("Done.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
