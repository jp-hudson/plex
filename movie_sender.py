#!/usr/bin/env python3

import os
import json
import smtplib
import subprocess
import urllib.parse
import urllib.request
import random
import time
from datetime import datetime, timedelta
from collections import defaultdict
from email.mime.text import MIMEText

# ================= CONFIG =================

MEDIA_DIRS = {
    "Movies": "/Volumes/Media/Movies",
    "TV": "/Volumes/Media/TV",
    "Anime": "/Volumes/Media/Anime",
}

STATE_FILE = os.path.expanduser("~/.media_state.json")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "hudson.plex.media@gmail.com"

EMAIL_FROM = "Hudson Plex Media <hudson.plex.media@gmail.com>"

# Actual recipients (will NOT appear in email headers)
EMAIL_TO = [
    "jhudson2083@gmail.com",
]

# What shows in the visible "To:" header (safe single address)
# (People will NOT see the full recipient list.)
EMAIL_VISIBLE_TO = "Hudson Plex Media <hudson.plex.media@gmail.com>"

# Optional: custom message file (edit when you want a one-off note)
# - If missing/blank -> no message included
# - If not modified since last send -> not included (prevents accidental repeats)
CUSTOM_MESSAGE_FILE = os.path.expanduser("~/.plex_custom_message.txt")

# ---- Scheduling ----
SEND_WEEKDAY = 4        # Friday (Mon=0)
SEND_HOUR = 13          # 1 PM

PREVIEW_EMAIL = False   # 🔧 True = preview only, no purge, no timestamp

EMERGENCY_QUOTES = [
    "“Progress, not perfection.”",
    "“One step at a time.”",
    "“Make it work, then make it right.”",
]

# ---- Safety rails ----
MAX_PENDING_TO_SEND = 200                 # never send more than this per email
SUSPICIOUS_PENDING_THRESHOLD = 600        # if pending exceeds this, refuse to send unless overridden
ALLOW_BULK_SEND = False                   # set True only if you REALLY intend to send large batches

# If scan suddenly shrinks by this factor vs the last baseline, assume a bad scan (unmounted volume, etc.)
SCAN_SHRINK_FACTOR = 0.60                 # 60% of prior size (tune if desired)

# =========================================
# ---------- Keychain helpers ----------

def get_smtp_password():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "media_smtp_pass", "-w"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError("SMTP password not found")
    return r.stdout.strip()


def get_omdb_key():
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "omdb_api_key", "-w"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError("OMDb key not found")
    return r.stdout.strip()


# ---------- Quotes ----------

def fetch_weekly_quote():
    sources = [
        ("https://zenquotes.io/api/random", lambda d: f"“{d[0]['q']}” — {d[0]['a']}"),
        ("https://api.quotable.io/random", lambda d: f"“{d['content']}” — {d['author']}"),
        ("https://quotes-db.vercel.app/api/random", lambda d: f"“{d['quote']}” — {d['author']}"),
    ]
    for url, fmt in sources:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return fmt(json.load(r))
        except Exception:
            pass
    return random.choice(EMERGENCY_QUOTES)


# ---------- IMDb / OMDb ----------

def imdb_search_link(title):
    return f"https://www.imdb.com/find/?q={urllib.parse.quote(title)}&s=tt"


def base_show_name(title):
    return title.split("–", 1)[0].strip()


def omdb_lookup(title, media_type):
    try:
        api_key = get_omdb_key()
        if media_type in ("TV", "Anime"):
            title = base_show_name(title)
            omdb_type = "series"
        else:
            omdb_type = "movie"

        q = urllib.parse.quote(title)
        url = f"https://www.omdbapi.com/?apikey={api_key}&t={q}&type={omdb_type}&r=json"

        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.load(r)

        if data.get("Response") == "True":
            return data
    except Exception:
        pass
    return None


def imdb_title_url(title, media_type):
    data = omdb_lookup(title, media_type)
    if data and "imdbID" in data:
        return f"https://www.imdb.com/title/{data['imdbID']}/"
    return imdb_search_link(title)


def rotten_tomatoes_score(title):
    data = omdb_lookup(title, "Movies")
    if not data:
        return None
    for r in data.get("Ratings", []):
        if r.get("Source") == "Rotten Tomatoes":
            return r["Value"]
    return None


# ---------- Pending sort helper ----------

def season_sort_key(item):
    media_rank = {"Movies": 0, "TV": 1, "Anime": 2}.get(item["type"], 99)

    show = item["name"]
    season_num = 0

    if "–" in item["name"]:
        show, season = item["name"].split("–", 1)
        show = show.strip()
        digits = "".join(c for c in season if c.isdigit())
        if digits.isdigit():
            season_num = int(digits)

    return (media_rank, show.lower(), season_num)


# ---------- Media scan ----------

def scan_media():
    items = []
    for media_type, base in MEDIA_DIRS.items():
        if not os.path.isdir(base):
            continue

        if media_type == "Movies":
            for m in sorted(os.listdir(base)):
                p = os.path.join(base, m)
                if os.path.isdir(p):
                    items.append({"name": m, "path": p, "type": media_type})
        else:
            for show in sorted(os.listdir(base)):
                sp = os.path.join(base, show)
                if not os.path.isdir(sp):
                    continue
                for season in sorted(os.listdir(sp)):
                    p = os.path.join(sp, season)
                    if os.path.isdir(p):
                        items.append({
                            "name": f"{show} – {season}",
                            "path": p,
                            "type": media_type,
                        })
    return items


# ---------- State ----------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"items": [], "pending": [], "last_email_ts": None, "last_custom_msg_mtime": None}

    with open(STATE_FILE) as f:
        d = json.load(f)

    d.setdefault("items", [])
    d.setdefault("pending", [])
    d.setdefault("last_email_ts", None)
    d.setdefault("last_custom_msg_mtime", None)
    return d


def save_state(state):
    # Atomic-ish save to avoid partial writes / corruption
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------- Scheduling helper ----------
# "Once per week after 1PM on Friday" (not "exactly 7 days since last send")

def most_recent_friday_window_start(now: datetime) -> datetime:
    days_since_friday = (now.weekday() - SEND_WEEKDAY) % 7
    window = (now - timedelta(days=days_since_friday)).replace(
        hour=SEND_HOUR, minute=0, second=0, microsecond=0
    )
    if now < window:
        window -= timedelta(days=7)
    return window


# ---------- Custom message helper ----------

def load_custom_message_if_fresh(state):
    """
    Returns (message_text_or_None, file_mtime_or_None)

    - None if file missing/blank.
    - None if file mtime is not newer than last_custom_msg_mtime (prevents repeats).
    """
    path = CUSTOM_MESSAGE_FILE
    try:
        if not os.path.exists(path):
            return (None, None)

        mtime = os.path.getmtime(path)
        last_mtime = state.get("last_custom_msg_mtime")

        if last_mtime is not None and mtime <= float(last_mtime):
            return (None, mtime)

        with open(path, "r", encoding="utf-8") as f:
            msg = f.read().strip()

        if not msg:
            return (None, mtime)

        return (msg, mtime)
    except Exception:
        return (None, None)


# ---------- Email ----------

def send_email(pending, recipients, custom_message=None):
    grouped = defaultdict(list)
    for i in pending:
        grouped[i["type"]].append(i)

    counts = {k: len(v) for k, v in grouped.items()}
    quote = fetch_weekly_quote()

    end = datetime.now()
    start = end - timedelta(days=7)

    movies = grouped.get("Movies", [])
    surprise = random.choice(movies) if movies else None

    text = [
        "Good evening John Plex user!",
        "",
        "Here’s your weekly John Plex Media digest 🎬",
        "",
    ]

    if custom_message:
        text.extend([
            "📣 Note from Hudson Plex Media:",
            custom_message,
            "",
        ])

    text.extend([
        f"New media added ({start:%b %d} – {end:%b %d})",
        "Summary: " + ", ".join(f"{v} {k}" for k, v in counts.items()),
        "",
    ])

    if surprise:
        rt = rotten_tomatoes_score(surprise["name"])
        text.extend([
            "🎲 Surprise movie pick this week:",
            f"{surprise['name']}" + (f" ⭐ {rt} RT" if rt else ""),
            f"IMDb: {imdb_title_url(surprise['name'], 'Movies')}",
            "",
        ])

    for t in ("Movies", "TV", "Anime"):
        if t in grouped:
            text.append(f"{t}:")
            for i in grouped[t]:
                line = f"- {i['name']}"
                if t == "Movies":
                    rt = rotten_tomatoes_score(i["name"])
                    if rt:
                        line += f" ⭐ {rt} RT"
                text.append(line)
                text.append(f"  IMDb: {imdb_title_url(i['name'], i['type'])}")
            text.append("")

    text.extend([
        "— — —",
        "Quote of the week:",
        quote,
        "",
        "Have a movie or TV series request? Please reply to this email with what you would like and we will get it onto list!",
        "",
        "To unsubscribe, reply:",
        "PLEASE REMOVE",
        "",
        "Enjoy the shows 🍿",
        "— Hudson Plex Media",
    ])

    msg = MIMEText("\n".join(text))
    msg["Subject"] = "🎬 John Plex Weekly Digest"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_VISIBLE_TO  # hide full list

    envelope_recipients = list(dict.fromkeys(recipients))

    pw = get_smtp_password()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, pw)
        s.send_message(msg, to_addrs=envelope_recipients)


# ---------- Main ----------

def main():
    state = load_state()

    # Safety: if the media directories aren't mounted/available, do NOTHING (don't overwrite baseline)
    missing_bases = [base for base in MEDIA_DIRS.values() if not os.path.isdir(base)]
    if missing_bases:
        now = datetime.now()
        print(f"[{now}] MEDIA NOT AVAILABLE, skipping run. Missing: {missing_bases}", flush=True)
        return

    current = scan_media()

    # Initial load: populate items only, do not email
    if not state["items"] and not state["pending"] and state["last_email_ts"] is None:
        state["items"] = current
        save_state(state)
        return

    # Safety: if scan suddenly shrank a lot, assume a bad scan and do NOT overwrite baseline/state
    prev_count = len(state.get("items", []))
    curr_count = len(current)
    if prev_count > 0 and curr_count < int(prev_count * SCAN_SHRINK_FACTOR):
        now = datetime.now()
        print(f"[{now}] SCAN LOOKS BAD (prev_items={prev_count}, current_items={curr_count}). "
              f"Skipping run to avoid baseline wipe.", flush=True)
        return

    seen = {i["path"] for i in state["items"]}
    pending_paths = {i["path"] for i in state["pending"]}

    # Compute new candidates first (so we can detect explosions before polluting pending)
    new_candidates = [i for i in current if i["path"] not in seen and i["path"] not in pending_paths]

    # Safety: if we'd add a huge number of new items in one run, treat it as baseline mismatch and re-baseline
    # without touching pending (prevents "everything is new" incidents).
    if (not PREVIEW_EMAIL) and (not ALLOW_BULK_SEND) and len(new_candidates) > SUSPICIOUS_PENDING_THRESHOLD:
        now = datetime.now()
        print(f"[{now}] FAILSAFE: would_add_new={len(new_candidates)} looks suspicious. "
              f"Re-baselining items to current scan and skipping send.", flush=True)
        state["items"] = current
        save_state(state)
        return

    # Normal case: add truly new items to pending
    for i in new_candidates:
        state["pending"].append(i)

    state["pending"].sort(key=season_sort_key)

    now = datetime.now()
    is_send_day = now.weekday() == SEND_WEEKDAY
    is_after_hour = now.hour >= SEND_HOUR

    window_start = most_recent_friday_window_start(now)
    already_sent_this_window = (
        state["last_email_ts"] is not None and
        datetime.fromtimestamp(state["last_email_ts"]) >= window_start
    )

    should_send = state["pending"] and (
        PREVIEW_EMAIL or (is_send_day and is_after_hour and not already_sent_this_window)
    )

    custom_message, custom_mtime = load_custom_message_if_fresh(state)

    print(
        f"[{now}] pending={len(state['pending'])} new_added={len(new_candidates)} "
        f"is_send_day={is_send_day} is_after_hour={is_after_hour} "
        f"window_start={window_start} last_email_ts={state['last_email_ts']} "
        f"already_sent_this_window={already_sent_this_window} preview={PREVIEW_EMAIL} "
        f"custom_msg_included={'yes' if custom_message else 'no'}",
        flush=True
    )

    if should_send:
        # Hard failsafe: refuse to send if pending is suspiciously huge (unless you override)
        if (not PREVIEW_EMAIL) and (not ALLOW_BULK_SEND) and len(state["pending"]) > SUSPICIOUS_PENDING_THRESHOLD:
            print(f"[{now}] FAILSAFE: pending={len(state['pending'])} exceeds "
                  f"SUSPICIOUS_PENDING_THRESHOLD={SUSPICIOUS_PENDING_THRESHOLD}. Not sending.", flush=True)
        else:
            # Never send more than MAX_PENDING_TO_SEND in a single email
            batch = state["pending"][:MAX_PENDING_TO_SEND]

            if PREVIEW_EMAIL:
                send_email(batch, ["jhudson2083@gmail.com"], custom_message=custom_message)
            else:
                send_email(batch, EMAIL_TO, custom_message=custom_message)

                # Remove only what we sent; keep the rest for later
                state["pending"] = state["pending"][MAX_PENDING_TO_SEND:]
                state["last_email_ts"] = time.time()

                if custom_mtime is not None and custom_message:
                    state["last_custom_msg_mtime"] = float(custom_mtime)

    # Update baseline (safe because we passed mount + shrink checks)
    state["items"] = current
    save_state(state)


if __name__ == "__main__":
    main()

