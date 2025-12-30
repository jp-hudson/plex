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
EMAIL_TO = [
    "jhudson2083@gmail.com",
]

# ---- Scheduling ----
SEND_WEEKDAY = 4        # Friday (Mon=0)
SEND_HOUR = 13          # 1 PM
WEEK_SECONDS = 7 * 24 * 60 * 60

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
        return {"items": [], "pending": [], "last_email_ts": None}

    with open(STATE_FILE) as f:
        d = json.load(f)

    d.setdefault("items", [])
    d.setdefault("pending", [])
    d.setdefault("last_email_ts", None)
    return d


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------- Email ----------

def send_email(pending, recipients):
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
        f"New media added ({start:%b %d} – {end:%b %d})",
        "Summary: " + ", ".join(f"{v} {k}" for k, v in counts.items()),
        "",
    ]

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
    msg["To"] = ", ".join(recipients)

    pw = get_smtp_password()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, pw)
        s.send_message(msg, to_addrs=recipients)


# ---------- Main ----------

def main():
    state = load_state()
    current = scan_media()

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
    week_elapsed = (
        state["last_email_ts"] is None or
        time.time() - state["last_email_ts"] >= WEEK_SECONDS
    )

    if state["pending"] and (PREVIEW_EMAIL or (is_send_day and is_after_hour and week_elapsed)):
        if PREVIEW_EMAIL:
            send_email(state["pending"], ["jhudson2083@gmail.com"])
        else:
            send_email(state["pending"], EMAIL_TO)
            state["pending"] = []
            state["last_email_ts"] = time.time()

    state["items"] = current
    save_state(state)


if __name__ == "__main__":
    main()

