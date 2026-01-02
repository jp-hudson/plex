# Media Automation Scripts

This repository contains two primary scripts used to automate Plex media ingestion and weekly media notification emails.

---

## Overview

### Scripts Included

- `movie_sender.py`
- `plex_pipeline.sh`

Each script serves a distinct role in the overall media workflow.

---
Prereqs:
---
```
pip install requests
pip install keyring
pip install python-dateutil
brew install gawk handbrake ffmpeg
brew install gawk
```
---

## `movie_sender.py`

This script scans Plex media directories for new content and sends a summary email when specific conditions are met.

### What It Does

- Scans Plex media folders **hourly**
- Detects newly added media
- Tracks new items in a **pending** state
- Sends a **single weekly email** when all criteria are satisfied

### Email Send Conditions

An email **will only be sent** if **all** of the following conditions are met:

1. The Mac is powered on  
2. It is **Friday after 1:00 PM**  
3. There is **at least one item** in the pending state  
4. No email has been sent since the **previous Friday**

If all conditions are met, a summary email listing newly added media is sent to a configured list of recipients.

---

## `plex_pipeline.sh`

Quick commands and info, this I setup on my mac M2 to launch as an app and run on schedule via launchctl I created a plist to do that:

First I created the app using Automator.app. >> Search for `Run Shell Script` Inside the block put in 

```
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

/Users/$USER/code/plex/plex_pipeline.sh /Users/$USER/PlexDrop
```

The export is necessary to have access to the homebrew commands for Handbrake, and Gawk.

Next we create a launchctl job to run it at midnight

vi ~/Library/LaunchAgents/com.jhudson.plexpipeline.plist

```
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.jhudson.plexpipeline</string>

    <!-- Every day at 00:00 -->
    <key>StartCalendarInterval</key>
    <dict>
      <key>Hour</key>
      <integer>0</integer>
      <key>Minute</key>
      <integer>0</integer>
    </dict>

    <!-- Launch hidden / background -->
    <key>ProgramArguments</key>
    <array>
      <string>/usr/bin/open</string>
      <string>-gj</string>
      <string>-a</string>
      <string>/Applications/Plexpipeline.app</string>
    </array>

    <key>StandardOutPath</key>
    <string>/Users/jhudson/Library/Logs/plex_pipeline.launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/jhudson/Library/Logs/plex_pipeline.launchd.err.log</string>

    <!-- Optional: helps it run like a foreground job if needed -->
    <key>ProcessType</key>
    <string>Interactive</string>
    <key>Nice</key>
    <integer>0</integer>
    <key>LowPriorityIO</key>
    <false/>
  </dict>
</plist>

```
### Setup your folders for the file zip / mkv file drop

mkdir -p /Users/$USER/PlexDrop/logs
mkdir -p /Users/$USER/PlexDrop/handbrake_raw

### Initialize the launchctl command to run your plex command

launchctl unload -w ~/Library/LaunchAgents/com.jhudson.plexpipeline.plist
launchctl load -w ~/Library/LaunchAgents/com.jhudson.plexpipeline.plist
launchctl kickstart -k gui/$(id -u)/com.jhudson.plexpipeline

This launchctl runs at 12AM or midnight) the kickstart comman will launch one for you

To see the launchtcl log files look at:

tail -n 200 ~/Library/Logs/plex_pipeline.launchd.err.log
tail -n 200 ~/Library/Logs/plex_pipeline.launchd.out.log

This script handles **media ingestion and transcoding** for downloaded content.

### Supported Input Formats

- `.mkv`
- `.zip`

### What It Does

1. Creates a working directory  
2. Unzips media when necessary  
3. Renames files according to expected naming conventions  
4. Creates a destination folder on the Plex media server  
5. Runs HandBrake to transcode media:
   - 1080p **or**
   - 2160p (4K)  
6. Performs cleanup:
   - Deletes the original `.zip`
   - Deletes temporary extracted directories  
7. Writes a log file in the directory where the script is executed

---

## Detailed Setup: `movie_sender.py`

### Email Account

- Uses the Gmail account:  
  **`hudson.plex.media@gmail.com`**
- Credentials are stored securely in **Bitwarden**
- No secrets are committed to the repository

---

### OMDb API (Movie Metadata)

To retrieve IMDb and Rotten Tomatoes ratings, an OMDb API key is required.

#### Obtain an API Key

Visit:

http://www.omdbapi.com/apikey.aspx

A free tier (1,000 requests per day) is sufficient.

#### Store the API Key in macOS Keychain

```bash
security add-generic-password \
  -a "omdb" \
  -s "omdb_api_key" \
  -w ""
```

---

### Gmail App Password

A Google App Password is used for SMTP authentication.

#### Store the Gmail App Password in Keychain

```bash
security add-generic-password \
  -a "hudson.plex.media@gmail.com" \
  -s "media_smtp_pass" \
  -w ""
```

---

## Running `movie_sender.py` Manually

```bash
/Users/jhudson/code/movie_sender.py
```

---

## Logs

```bash
tail ~/.media_scan.log
tail ~/.media_scan.err
```

---

## Scheduling with `launchctl` (macOS)

The script is configured to run **hourly** using `launchctl`.

### Create the Launch Agent

Edit the file:

```bash
vi ~/Library/LaunchAgents/com.hudson.media-scan.plist
```

Paste the following:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

  <key>Label</key>
  <string>com.hudson.media-scan</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/jhudson/code/movie_sender.py</string>
  </array>

  <key>StartInterval</key>
  <integer>3600</integer>

  <key>StandardOutPath</key>
  <string>/Users/jhudson/.media_scan.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/jhudson/.media_scan.err</string>

</dict>
</plist>
```

Safe to run, it parses the .media.state.json and tells you when the next email is going to send out.

```
(base) jhudson@Mac-Studio plex % python3 - <<'PY'
import json, os, time
from datetime import datetime
STATE=os.path.expanduser("~/.media_state.json")
d=json.load(open(STATE))
now=datetime.now()
SEND_WEEKDAY=4
SEND_HOUR=13
WEEK_SECONDS=7*24*60*60

last=d.get("last_email_ts")
pending=len(d.get("pending",[]))
is_send_day = now.weekday() == SEND_WEEKDAY
is_after_hour = now.hour >= SEND_HOUR
week_elapsed = (last is None) or (time.time() - last >= WEEK_SECONDS)

print("now:", now)
print("pending:", pending)
print("is_send_day:", is_send_day, "is_after_hour:", is_after_hour, "week_elapsed:", week_elapsed)
if last:
    last_dt=datetime.fromtimestamp(last)
    remaining=max(0, WEEK_SECONDS - (time.time()-last))
    print("last_email:", last_dt)
    print("seconds_remaining_until_week_elapsed:", int(remaining))
PY

now: 2026-01-02 15:30:14.916842
pending: 39
is_send_day: True is_after_hour: True week_elapsed: False
last_email: 2025-12-26 23:08:59.830999
seconds_remaining_until_week_elapsed: 27524
```
Output looks like this:

```
now: 2026-01-02 15:30:14.916842
pending: 39
is_send_day: True is_after_hour: True week_elapsed: False
last_email: 2025-12-26 23:08:59.830999
seconds_remaining_until_week_elapsed: 27524
```

---

### Load the Job

```bash
launchctl load ~/Library/LaunchAgents/com.hudson.media-scan.plist
```

---

### Run Immediately (for Testing)

```bash
launchctl start com.hudson.media-scan
```

---

## Notes

- If running on a **server**, use `cron` instead of `launchctl`
- All credentials are stored in **macOS Keychain**
- No secrets are committed to version control
- Logs are written to the user’s home directory for easy inspection
