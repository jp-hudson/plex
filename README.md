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
