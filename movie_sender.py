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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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

        # If we've already used this exact file version, do not include again
        if last_mtime is not None and mtime <= float(last_mtime):
            return (None, mtime)

        with open(path, "r", encoding="utf-8") as f:
            msg = f.read().strip()

        if not msg:
            return (None, mtime)

        return (msg, mtime)
    except Exception:
        # Fail safe: if anything goes wrong, do not include custom message
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

    # Optional custom note (only included when provided)
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

    # IMPORTANT: hide recipient list by not putting it in headers
    msg["To"] = EMAIL_VISIBLE_TO
    # Do NOT set msg["Bcc"] (not needed, and you said you don't want it)

    # SMTP envelope recipients (actual delivery list)
    envelope_recipients = list(dict.fromkeys(recipients))

    pw = get_smtp_password()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, pw)
        s.send_message(msg, to_addrs=envelope_recipients)


# ---------- Main ----------

def main():
    state = load_state()
    current = scan_media()

    # Initial load: populate items only, do not email
    if not state["items"] and not state["pending"] and state["last_email_ts"] is None:
        state["items"] = current
        save_state(state)
        return

    seen = {i["path"] for i in state["items"]}
    pending_paths = {i["path"] for i in state["pending"]}

    for i in current:
        if i["path"] not in seen and i["path"] not in pending_paths:
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

    # Load custom message (only included if changed since last send)
    custom_message, custom_mtime = load_custom_message_if_fresh(state)

    # Decision log (goes to stdout; launchd will capture it if logs are configured)
    print(
        f"[{now}] pending={len(state['pending'])} "
        f"is_send_day={is_send_day} is_after_hour={is_after_hour} "
        f"window_start={window_start} last_email_ts={state['last_email_ts']} "
        f"already_sent_this_window={already_sent_this_window} preview={PREVIEW_EMAIL} "
        f"custom_msg_included={'yes' if custom_message else 'no'}",
        flush=True
    )

    if should_send:
        if PREVIEW_EMAIL:
            send_email(state["pending"], ["jhudson2083@gmail.com"], custom_message=custom_message)
        else:
            send_email(state["pending"], EMAIL_TO, custom_message=custom_message)
            state["pending"] = []
            state["last_email_ts"] = time.time()

            # Only record custom message usage when we actually sent a real email
            if custom_mtime is not None and custom_message:
                state["last_custom_msg_mtime"] = float(custom_mtime)

    state["items"] = current
    save_state(state)


if __name__ == "__main__":
    main()

