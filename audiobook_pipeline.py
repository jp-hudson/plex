#!/usr/bin/env python3
import argparse
import difflib
import json
import re
import shutil
import subprocess
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

# NEW: ebook formats that sometimes ride along with audiobook drops
EBOOK_EXTS = {".mobi", ".epub"}

# NEW: “book media” = audio + ebook files
MEDIA_EXTS = set(AUDIO_EXTS) | set(EBOOK_EXTS)

JUNK_PHRASES = [
    # IMPORTANT: longer phrases should win over shorter ones
    "mp3 split chapters",
    "split chapters",
    "full-cast edition",
    "full cast edition",
    "full-cast",
    "full cast",
    "fullcast",
    "audiobook",
    "audiobooks",
    "stereo",
    "mono",
    "unabridged",
    "abridged",
    "complete",
    "collection",
    "freepaidbooks.online",
    "freepaidbooks online",
    "freepaidbooks",
    "tgx",
    "rartv",
    "webrip",
    "web-dl",
    "web dl",
    "web",
    "bluray",
    "bdrip",
    "hdtv",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "av1",
    "ddp",
    "eac3",
    "aac",
    "atmos",
    "esub",
    "subs",
    "subtitles",
    "1080p",
    "720p",
    "2160p",
    "10bit",
    "8bit",
    # NEW: folder-name “format labels”
    "epub",
    "mobi",
]

OPENLIB_TIMEOUT_SECS = 6

SIDE_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".cue", ".m3u", ".m3u8", ".txt"}

# If destination already has the incoming content, do NOT create "(2)".
# Move incoming duplicates into <source>/_duplicates instead.
DUPLICATES_DIRNAME = "_duplicates"

# High-confidence hint -> author shortcuts (used before Open Library lookup)
KNOWN_HINT_AUTHORS = {
    "harry potter": "J. K. Rowling",
    "witcher": "Andrzej Sapkowski",
}

# Multipart markers for “single book split into parts/discs”
MULTIPART_RE = re.compile(
    r"\b(?:part|pt|cd|disc|disk|vol|volume)\s*0*\d{1,3}\b",
    re.IGNORECASE
)
DISC_DIR_RE = re.compile(r"^(?:cd|disc|disk|part|pt|vol|volume)\s*0*\d{1,3}\b", re.IGNORECASE)

# ============================================================
# HELPERS
# ============================================================

def log(msg: str) -> None:
    print(msg, flush=True)

def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def safe_component(s: str) -> str:
    s = s.strip()
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

def strip_parens_year(s: str) -> Tuple[str, Optional[str]]:
    year = None
    m = re.search(r"\b(19|20)\d{2}\b", s)
    if m:
        year = m.group(0)
    s2 = re.sub(r"\(\s*(19|20)\d{2}\s*\)", " ", s)
    return s2, year

def strip_junk_phrases(s: str) -> str:
    for phrase in sorted(JUNK_PHRASES, key=len, reverse=True):
        s = re.sub(re.escape(phrase), " ", s, flags=re.IGNORECASE)
    return s

def remove_empty_parentheses(s: str) -> str:
    # remove any parens with no alphanumerics inside
    s = re.sub(r"\(\s*[^A-Za-z0-9]*\s*\)", " ", s)
    return s

def clean_title_text(s: str) -> Tuple[str, Optional[str], str]:
    original = s

    s = strip_brackets(s)
    s = sanitize_spaces(s)
    s, year = strip_parens_year(s)

    # Remove "The Witcher, Book 3" style suffixes (keep title itself)
    s = re.sub(r"\bthe witcher\s*,?\s*book\s*\d+\b", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\bbook\s*\d+\b", " ", s, flags=re.IGNORECASE)

    s = strip_junk_phrases(s)

    # Remove stray filetype tokens that can linger after junk stripping
    s = re.sub(r"\b(mp3|m4b|m4a|aax|mobi|epub)\b", " ", s, flags=re.IGNORECASE)

    s = remove_empty_parentheses(s)
    s = re.sub(r"[-–—]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    hint = ""
    if re.search(r"\bwitcher\b", original, re.IGNORECASE):
        hint = "witcher"
    elif re.search(r"\bharry potter\b", original, re.IGNORECASE):
        hint = "harry potter"

    return s, year, hint

def looks_like_author_name(s: str) -> bool:
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
        if re.fullmatch(r"[A-Z]\.", t):
            continue
        if re.fullmatch(r"[A-Z][a-z]+(?:[\'-][A-Z]?[a-z]+)*", t):
            continue
        return False

    return True

def parse_title_author_from_text(text: str) -> Tuple[str, Optional[str]]:
    t = text.strip()

    m = re.search(r"\s+by\s+", t, flags=re.IGNORECASE)
    if m:
        title, author = re.split(r"\s+by\s+", t, maxsplit=1, flags=re.IGNORECASE)
        return title.strip(), author.strip()

    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", t) if p.strip()]
    if len(parts) >= 2 and looks_like_author_name(parts[0]):
        author = parts[0]
        title = " - ".join(parts[1:])
        return title.strip(), author.strip()

    return t, None

def ffprobe_tags(path: Path) -> Dict[str, str]:
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
        return {k.lower(): str(v).strip() for k, v in tags.items() if str(v).strip()}
    except Exception:
        return {}

def openlibrary_lookup_author(title: str, hint: str = "") -> Optional[str]:
    t = title.strip()
    if not t:
        return None

    def fetch(params: Dict[str, str]) -> Optional[dict]:
        url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "audiobook_ingest/1.0"})
            with urllib.request.urlopen(req, timeout=OPENLIB_TIMEOUT_SECS) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
        except Exception:
            return None

    data = fetch({"title": t, "limit": "10"})
    if not data:
        q = f"{t} {hint}".strip() if hint else t
        data = fetch({"q": q, "limit": "10"})
        if not data:
            return None

    docs = data.get("docs") or []
    if not docs:
        return None

    def score(doc) -> float:
        dt = (doc.get("title") or "").strip()
        if not dt:
            return 0.0
        return difflib.SequenceMatcher(None, t.lower(), dt.lower()).ratio()

    docs_sorted = sorted(docs, key=score, reverse=True)
    for d in docs_sorted[:5]:
        authors = d.get("author_name") or []
        if authors:
            return str(authors[0]).strip()

    return None

def is_chapterish_mp3_names(files: List[Path]) -> bool:
    if not files:
        return False
    mp3s = [p for p in files if p.suffix.lower() == ".mp3"]
    if len(mp3s) < 2:
        return False

    def looks_num(stem: str) -> bool:
        stem = stem.strip()
        return bool(re.match(r"^\d{1,3}(\s*[-_.].*)?$", stem))

    hits = sum(1 for p in mp3s if looks_num(p.stem))
    return hits >= max(2, int(len(mp3s) * 0.6))

def gather_media_recursive(d: Path) -> List[Path]:
    out: List[Path] = []
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            out.append(p)
    return sorted(out)

def gather_sidecars_recursive(d: Path) -> List[Path]:
    out: List[Path] = []
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in SIDE_EXTS:
            out.append(p)
    return sorted(out)

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

def pick_author_from_tags(tags: Dict[str, str]) -> Optional[str]:
    for k in ("album_artist", "artist", "composer"):
        v = tags.get(k)
        if v and looks_like_author_name(v):
            return v
    return None

def resolve_title_author(title_text: str, author_text: Optional[str], author_hint: Optional[str]) -> Tuple[str, str]:
    title_clean, _year, hint = clean_title_text(title_text)

    author: Optional[str] = author_text.strip() if author_text else None
    if author and not looks_like_author_name(author):
        author = None

    if not author and author_hint and looks_like_author_name(author_hint):
        author = author_hint

    if not author and hint in KNOWN_HINT_AUTHORS:
        author = KNOWN_HINT_AUTHORS[hint]

    if not author:
        author = openlibrary_lookup_author(title_clean, hint=hint)

    if not author:
        author = "Unknown Author"

    title_clean = safe_component(title_clean)
    author = safe_component(author)
    return title_clean, author

def ensure_unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = Path(str(path) + f" ({i})")
        if not cand.exists():
            return cand
    return Path(str(path) + f" ({int(time.time())})")

def files_exist_in_dest(src_files: List[Path], dst_dir: Path) -> bool:
    if not dst_dir.exists():
        return False
    existing_any = any((dst_dir / f.name).exists() for f in src_files)
    if not existing_any:
        return False
    return all((dst_dir / f.name).exists() for f in src_files)

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

def move_file_skip_or_move_dup(src_file: Path, dst_dir: Path, src_root: Path, dry_run: bool) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_path = dst_dir / src_file.name

    if dst_path.exists():
        log(f"SKIP (exists): {dst_path}")
        move_to_duplicates(src_file, src_root, dry_run)
        return

    log(f"MOVE: {src_file} -> {dst_path}")
    if not dry_run:
        shutil.move(str(src_file), str(dst_path))

def is_multipart_single_book(files: List[Path]) -> bool:
    """
    Detect cases like:
      Family Upstairs-Part01.mp3 ... Part08.mp3
      Games Traitors Play - CD 01.mp3 ... CD 02.mp3
    """
    if len(files) < 2:
        return False

    mp3s = [f for f in files if f.suffix.lower() == ".mp3"]
    if len(mp3s) < 2:
        return False

    stems = [sanitize_spaces(f.stem) for f in mp3s]
    bases: List[str] = []
    marker_hits = 0

    for st in stems:
        st2 = strip_brackets(st)
        st2 = re.sub(r"\s+", " ", st2).strip()
        if MULTIPART_RE.search(st2):
            marker_hits += 1
            st2 = re.sub(
                r"[\s\-_.]*\b(?:part|pt|cd|disc|disk|vol|volume)\s*0*\d{1,3}\b.*$",
                "",
                st2,
                flags=re.IGNORECASE
            ).strip()
        bases.append(st2.lower())

    if marker_hits < max(2, int(len(stems) * 0.6)):
        return False

    counts: Dict[str, int] = {}
    for b in bases:
        if not b:
            continue
        counts[b] = counts.get(b, 0) + 1
    if not counts:
        return False

    _top_base, top_n = max(counts.items(), key=lambda kv: kv[1])
    return top_n >= max(2, int(len(bases) * 0.8))

def has_disc_subdirs_with_audio(p: Path) -> bool:
    if not p.is_dir():
        return False

    subdirs = [d for d in p.iterdir() if d.is_dir() and DISC_DIR_RE.match(d.name.strip())]
    if len(subdirs) < 2:
        return False

    with_audio = 0
    for d in subdirs:
        if any(x.is_file() and x.suffix.lower() in AUDIO_EXTS for x in d.rglob("*")):
            with_audio += 1

    return with_audio >= 2

def process_book_dir(book_dir: Path, dest_root: Path, dry_run: bool, author_hint: Optional[str], src_root: Path) -> None:
    title_text, author_text = parse_title_author_from_text(book_dir.name)
    title_clean, author = resolve_title_author(title_text, author_text, author_hint)

    dst_dir = dest_root / author / title_clean

    media_files = gather_media_recursive(book_dir)
    if not media_files:
        return

    sidecars = gather_sidecars_recursive(book_dir)

    if files_exist_in_dest(media_files + sidecars, dst_dir):
        log(f"SKIP BOOK (already ingested): '{title_clean}' | AUTHOR: '{author}'")
        log(f"DEST: {dst_dir}")
        move_to_duplicates(book_dir, src_root, dry_run)
        return

    log(f"BOOK: '{title_clean}' | AUTHOR: '{author}'")
    log(f"DEST: {dst_dir}")

    for f in media_files:
        move_file_skip_or_move_dup(f, dst_dir, src_root, dry_run)
    for f in sidecars:
        move_file_skip_or_move_dup(f, dst_dir, src_root, dry_run)

def process_single_media_file(f: Path, dest_root: Path, dry_run: bool, author_hint: Optional[str], src_root: Path) -> None:
    stem = f.stem
    title_text, author_text = parse_title_author_from_text(stem)

    # Only try ffprobe tags for actual audio formats
    if f.suffix.lower() in AUDIO_EXTS:
        tags = ffprobe_tags(f)
        tag_author = pick_author_from_tags(tags)
        if not author_text and tag_author:
            author_text = tag_author

    title_clean, author = resolve_title_author(title_text, author_text, author_hint)
    dst_dir = dest_root / author / title_clean

    log(f"BOOK: '{title_clean}' | AUTHOR: '{author}'")
    log(f"DEST: {dst_dir}")

    move_file_skip_or_move_dup(f, dst_dir, src_root, dry_run)

def process_path(p: Path, dest_root: Path, dry_run: bool, author_hint: Optional[str], src_root: Path) -> None:
    if p.name.startswith("."):
        return
    if p.name in ("_failed_zips", DUPLICATES_DIRNAME):
        return

    if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
        process_single_media_file(p, dest_root, dry_run, author_hint, src_root)
        return

    if not p.is_dir():
        return

    local_author_hint = author_hint
    if looks_like_author_name(p.name):
        local_author_hint = p.name

    direct_media = sorted([x for x in p.iterdir() if x.is_file() and x.suffix.lower() in MEDIA_EXTS])
    direct_mp3 = [x for x in direct_media if x.suffix.lower() == ".mp3"]
    direct_audio = [x for x in direct_media if x.suffix.lower() in AUDIO_EXTS]

    if not direct_audio and has_disc_subdirs_with_audio(p):
        process_book_dir(p, dest_root, dry_run, local_author_hint, src_root)
        return

    if direct_media:
        # If it's clearly a “single book folder” (chapters / multipart / single file),
        # ingest the whole folder as one book.
        if (direct_audio and (is_chapterish_mp3_names(direct_mp3) or len(direct_media) == 1 or is_multipart_single_book(direct_media))) or (not direct_audio):
            process_book_dir(p, dest_root, dry_run, local_author_hint, src_root)
        else:
            # Otherwise treat each file as its own “book”
            for f in direct_media:
                process_single_media_file(f, dest_root, dry_run, local_author_hint, src_root)
        return

    for d in sorted([d for d in p.iterdir() if d.is_dir()]):
        process_path(d, dest_root, dry_run, local_author_hint, src_root)

def main() -> int:
    ap = argparse.ArgumentParser(description="Ingest audiobooks into /Volumes/Media/Audiobooks/<Author>/<Title>/")
    ap.add_argument("--source", default=None, help="Source root (or PlexDrop; will use audiobook_queue if present).")
    ap.add_argument("--dest", default=None, help="Destination root.")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without moving files.")
    ap.add_argument("positional_source", nargs="?", help="(legacy) source path")

    args = ap.parse_args()

    src_str = args.source or args.positional_source or DEFAULT_SOURCE
    dest_str = args.dest or DEFAULT_DEST

    src = Path(src_str).expanduser()
    dest = Path(dest_str).expanduser()

    if (src / "audiobook_queue").exists():
        src = src / "audiobook_queue"

    if not src.exists():
        log(f"ERROR: source does not exist: {src}")
        return 2

    log(f"SOURCE: {src}")
    log(f"DEST:   {dest}")
    log(f"DRYRUN: {args.dry_run}")

    for item in sorted(src.iterdir()):
        process_path(item, dest, args.dry_run, author_hint=None, src_root=src)

    remove_empty_dirs(src, args.dry_run)
    log("Done.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
