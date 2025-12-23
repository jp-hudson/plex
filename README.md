Media Scanner is setup with hudson.plex.media@gmail.com email account. Credentials are in bitwarded


In order to get things working so it querried the movies I had to create an omdb API key. I was able to do that by visiting:

http://www.omdbapi.com/apikey.aspx

I generated a free (1000 license key)

I put the key into my keychain via:

API for omdb
security add-generic-password \                  
  -a "omdb" \
  -s "omdb_api_key" \
  -w ''

For the gmail account I created a google app password. Then I added it to my keychain like

security add-generic-password \                                  
  -a "hudson.plex.media@gmail.com" \
  -s "media_smtp_pass" \
  -w ""

launchctl start com.hudson.media-scan

This was necessary setup to send emails. Once I did that I could run my code at:

/Users/jhudson/code/movie_sender.py

Check logs

tail ~/.media_scan.log
tail ~/.media_scan.err

#Setting up the Cron

On my mac.

vi ~/Library/LaunchAgents/com.hudson.media-scan.plist

and put in below:

```
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>

  <!-- Name of the job -->
  <key>Label</key>
  <string>com.hudson.media-scan</string>

  <!-- What to run -->
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/jhudson/code/movie_sender.py</string>
  </array>

  <!-- Run once a week -->
  <key>StartCalendarInterval</key>
  <dict>
    <!-- Sunday -->
    <key>Weekday</key><integer>7</integer>
    <!-- 8 PM -->
    <key>Hour</key><integer>20</integer>
    <key>Minute</key><integer>0</integer>
  </dict>

  <!-- Logging -->
  <key>StandardOutPath</key>
  <string>/Users/jhudson/.media_scan.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/jhudson/.media_scan.err</string>

</dict>
</plist>
```

Load the job

```
launchctl load ~/Library/LaunchAgents/com.hudson.media-scan.plist
```

Test it immediatly:

```
launchctl start com.hudson.media-scan
```
