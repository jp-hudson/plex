There are two scripts here:

```
movie_sender.py
```

This script scans the plex media folder and looks for new additions. It scans every hour on your mac via launchctl. If it sees any new additions to the media diretory it will place the changes in pending. It is set to not send an email out under these conditions:

1) Mac must be on
2) It must be Friday after 1PM
3) There is a item in the pending state
4) There has not been another email sent since the previous Friday

If these conditions are met, an email with the media is sent to a list of users specified. 

The second script:

```
plex_pipeline.sh
```
This script is used for ingesting downloaded media content that come as either .mkz or .zip. It will create a folder, unzip the contents, then rename the contents as it sees fit. Then it will create a folder on your media server location of the media and proceed to run either a 1080P or 2160P burn using handbrake. Once it completes it deletes the .zip and the extracted folder it created. A log file is created in the folder that it is run in.


More in-depth setup / view of movie_sender.py below:

movie_sender.py is setup with hudson.plex.media@gmail.com email account. Credentials are in bitwarden

In order to get things working so it querried the movies I had to create an omdb API key. This is necessary to check the IMDB DB and pull the rotten tomatoe rating if I can find it. I was able to do that by visiting:

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

# Setting up the Cron job to run hourly on my mac. If you are running on a server you will use cron most likely. Here we use launchctl.

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

  <!-- Run every hour -->
  <key>StartInterval</key>
  <integer>3600</integer>

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

