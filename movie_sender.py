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
from collections import defaultdict, Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# ================= CONFIG =================

MEDIA_DIRS = {
    "Movies": "/Volumes/Media/Movies",
    "TV": "/Volumes/Media/TV",
    "Anime": "/Volumes/Media/Anime",
}

STATE_FILE = os.path.expanduser("~/.media_state.json")
UNSUB_FILE = os.path.expanduser("~/.media_unsubscribed.json")

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = "hudson.plex.media@gmail.com"

EMAIL_FROM = "Hudson Plex Media <hudson.plex.media@gmail.com>"
EMAIL_TO = [
    "jhudson2083@gmail.com",
    "torpaulson@gmail.com",
]

EMERGENCY_QUOTES = [
    "“Progress, not perfection.”",
    "“One step at a time.”",
    "“Make it work, then make it right.”",
]

WEEK_SECONDS = 7 * 24 * 60 * 60

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


# ---------- Quote helper ----------

def fetch_weekly_quote():
    for url, fmt in [
        ("https://zenquotes.io/api/random", lambda d: f"“{d[0]['q']}” — {d[0]['a']}"),
        ("https://api.quotable.io/random", lambda d: f"“{d['content']}” — {d['author']}"),
        ("https://quotes-db.vercel.app/api/random", lambda d: f"“{d['quote']}” — {d['author']}"),
    ]:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return fmt(json.load(r))
        except Exception:
            pass

    return random.choice(EMERGENCY_QUOTES)


# ---------- IMDb helpers ----------

def imdb_search_link(title):
    return f"https://www.imdb.com/find/?q={urllib.parse.quote(title)}&s=tt"


def base_show_name(title):
    return title.split("–", 1)[0].strip()


def imdb_title_url(title, media_type):
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
            return f"https://www.imdb.com/title/{data['imdbID']}/"

    except Exception:
        pass

    return imdb_search_link(title)


# ---------- Media scanning ----------

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
        print("🆕 No state file found — initializing baseline")
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
    print("💾 State saved")


# ---------- Email ----------

def send_email(pending):
    if not pending:
        return

    grouped = defaultdict(list)
    for i in pending:
        grouped[i["type"]].append(i)

    counts = {k: len(v) for k, v in grouped.items()}
    quote = fetch_weekly_quote()

    end = datetime.now()
    start = end - timedelta(days=7)

    names = [base_show_name(i["name"]) for i in pending]
    popular = Counter(names).most_common(1)
    popular_line = f"🔥 Popular this week: {popular[0][0]}" if popular else None

    text = [
        "Good evening John Plex user!\n",
        f"New media added ({start:%b %d} – {end:%b %d})\n",
        "Summary: " + ", ".join(f"{v} {k}" for k, v in counts.items()) + "\n",
    ]

    if popular_line:
        text.append(popular_line + "\n")

    for t in ("Movies", "TV", "Anime"):
        if t in grouped:
            text.append(f"{t}:")
            for i in grouped[t]:
                text.append(f"- {i['name']}")
            text.append("")

    text.extend(["— — —", quote])

    msg = MIMEText("\n".join(text))
    msg["Subject"] = "🎬 John Plex Weekly Digest"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(EMAIL_TO)

    pw = get_smtp_password()
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, pw)
        s.send_message(msg)

    print(f"📧 Email sent with {len(pending)} items")


# ---------- Main ----------

def main():
    state = load_state()
    current = scan_media()

    print(f"🔍 Scan complete: {len(current)} total items found")

    # ---- Baseline guard ----
    if not state["items"] and not state["pending"] and state["last_email_ts"] is None:
        state["items"] = current
        save_state(state)
        print("✅ Baseline established (no pending, no email)")
        return

    seen = {i["path"] for i in state["items"]}
    pending_paths = {i["path"] for i in state["pending"]}

    added = 0
    for i in current:
        if i["path"] not in seen and i["path"] not in pending_paths:
            state["pending"].append(i)
            added += 1

    if added:
        print(f"➕ Added {added} new items to pending")
    else:
        print("➖ No new items detected")

    now = datetime.now()
    friday = now.weekday() == 4 and now.hour >= 13
    elapsed = (
        state["last_email_ts"] is None or
        time.time() - state["last_email_ts"] >= WEEK_SECONDS
    )

    if state["pending"] and friday and elapsed:
        send_email(state["pending"])
        state["pending"] = []
        state["last_email_ts"] = time.time()
    else:
        print("⏳ Accumulating items (email not sent)")

    state["items"] = current
    save_state(state)


if __name__ == "__main__":
    main()

