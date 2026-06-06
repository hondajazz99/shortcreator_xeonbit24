# ShortCreator — YouTube Shorts Automation

Automatically create and publish YouTube Shorts from Telegram channel content.

**Pipeline:** Telegram photo post → Vietnamese TTS → Ken Burns video → YouTube (scheduled)

-----

## 🔑 Getting a YouTube OAuth Refresh Token

Playlist management requires the **full YouTube scope**. Follow these steps exactly:

1. Go to **https://developers.google.com/oauthplayground/**
1. Click the **gear icon ⚙️** (top-right) → check **“Use your own OAuth credentials”**
- Enter your **OAuth Client ID** and **OAuth Client Secret** from Google Cloud Console
1. In the left panel, find **“YouTube Data API v3”** and select:
   
   ```
   https://www.googleapis.com/auth/youtube
   ```

> ⚠️ Do NOT use `youtube.upload` — that scope alone blocks playlist operations.
1. Click **“Authorize APIs”** → sign in with your YouTube account → allow access
1. Click **“Exchange authorization code for tokens”**
1. Copy the **Refresh token** value shown
1. Build your `YOUTUBE_CLIENT_SECRETS` JSON (store in GitHub Secrets):
   
   ```json
   {
     "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
     "client_secret": "YOUR_CLIENT_SECRET",
     "refresh_token": "PASTE_NEW_REFRESH_TOKEN_HERE",
     "token_uri": "https://oauth2.googleapis.com/token"
   }
   ```

-----

## Setup

### 1. Telegram Bot Token

- Create a bot via [@BotFather](https://t.me/BotFather)
- Add the bot as an **admin** to your channel(s)
- Copy the HTTP API token

### 2. YouTube API (Google Cloud Console)

- Go to [Google API Console](https://console.cloud.google.com/apis/dashboard)
- Create a project → enable **YouTube Data API v3**
- Create **OAuth 2.0 credentials** (type: Web application)
- Add `https://developers.google.com/oauthplayground` as an authorized redirect URI
- Note your Client ID + Secret

### 3. GitHub Secrets

|Secret                  |Value                                                            |
|------------------------|-----------------------------------------------------------------|
|`TELEGRAM_TOKEN`        |Your Telegram bot token                                          |
|`TELEGRAM_CHANNELS`     |`["@yourchannel"]` (JSON array)                                  |
|`YOUTUBE_CLIENT_SECRETS`|Full JSON with client_id, client_secret, refresh_token, token_uri|

### 4. Environment Variables (with defaults)

|Variable             |Default                                               |Description                                                  |
|---------------------|------------------------------------------------------|-------------------------------------------------------------|
|`PLAYLIST_ID`        |`PL3B7UtjF3P8ya2XNvBX8fgKOoqsCza8dv`                  |YouTube playlist to add each video to                        |
|`PUBLISH_DELAY_HOURS`|`1`                                                   |Hours until scheduled publish                                |
|`BRAND_HASHTAGS`     |`["cryptohieuqua","cryptohieu.com"]`                  |Always-on hashtags appended to every video                   |
|`DURATION`           |`15`                                                  |Minimum video duration in seconds (extended if TTS is longer)|
|`PRIVACY_STATUS`     |`private`                                             |Upload privacy (always private when scheduled)               |
|`TAGS`               |`["Shorts","Auto-generated"]`                         |Base YouTube tags                                            |
|`DESCRIPTION`        |`"Automated YouTube Short"`                           |Video description prefix                                     |
|`MUSIC_OPTION`       |`music.mp3`                                           |Background music file path or HTTP URL                       |
|`FONT_PATH`          |`/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`     |Regular font                                                 |
|`FONT_BOLD_PATH`     |`/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`|Bold font for captions                                       |
|`MAX_TELEGRAM_POSTS` |`10`                                                  |Max new posts to process per run                             |

-----

## Features

### Telegram → YouTube Pipeline

Fetches up to `MAX_TELEGRAM_POSTS` new photo posts from each configured channel, newest-first. For each post, one Short is created and uploaded.

### Duplicate Prevention

Every `channel:message_id` that is successfully uploaded is saved to `.published_ids.json`. Posts already in this file are skipped on future runs. The file is committed back to the repo after each GitHub Actions run so state persists across ephemeral runners.

### Vietnamese TTS with Word-Synced Captions

- Caption text is read aloud using `edge-tts` with the `vi-VN-HoaiMyNeural` voice
- A Vietnamese subscribe call-to-action is automatically appended: *“Đừng quên đăng ký kênh để xem thêm nhiều video hữu ích nhé!”*
- TTS audio is sped up to **1.25×** via ffmpeg `atempo` filter
- Word timings come from `WordBoundary` events; if the Vietnamese voice does not fire them, timings are calculated by evenly distributing words across the real TTS duration
- Each spoken word is displayed on screen in yellow bold text with a grey semi-transparent rounded background (karaoke-style), positioned in the lower third of the frame

### Ken Burns Zoom Effect

Each frame applies a smooth zoom-in then zoom-out over the full video duration (peaks at **1.15× scale** at the midpoint), giving static images a dynamic, broadcast-quality feel.

### Brand Logo Overlay

`brand_logo.png` (repo root) is composited into the **top-right corner** of every frame at 140px, with a 20px margin.

### Background Music Mixing

- Background music plays at **30% volume** when TTS is present, **80%** otherwise
- Music loops automatically if shorter than the video duration
- Audio fades in (1s) and out (1.5s)
- TTS audio plays at full volume layered on top of the music

### Video Output

- Resolution: **1080×1920** (portrait / Shorts format)
- Frame rate: **24 fps**
- Codec: H.264 video + AAC audio
- Video fades in and out (0.5s each)
- Duration: whichever is longer — `DURATION` seconds or TTS length + 1s

### YouTube Upload

- Uploaded as **private** with a scheduled `publishAt` (`PUBLISH_DELAY_HOURS` from now)
- Title: `Video Short {caption[:50]} YYYY-MM-DD HH:MM`
- Description: base description + original caption + brand hashtags + top caption-derived hashtags
- Video is added to `PLAYLIST_ID` immediately after upload

-----

## Run Locally

```bash
pip install -r requirements.txt

export TELEGRAM_TOKEN="..."
export TELEGRAM_CHANNELS='["@yourchannel"]'
export YOUTUBE_CLIENT_SECRETS='{"client_id":"...","client_secret":"...","refresh_token":"...","token_uri":"https://oauth2.googleapis.com/token"}'
export PLAYLIST_ID="PL3B7UtjF3P8ya2XNvBX8fgKOoqsCza8dv"

python short_creator.py
```

You also need `ffmpeg` installed and the DejaVu fonts available:

```bash
sudo apt-get install -y ffmpeg fonts-dejavu-core fonts-noto-color-emoji
```

-----

## GitHub Actions Workflow

The workflow runs every **6 hours** and on manual dispatch.

```yaml
on:
  schedule:
    - cron: '0 */6 * * *'
  workflow_dispatch:

jobs:
  build-and-upload:
    runs-on: ubuntu-latest
    permissions:
      contents: write   # needed to push .published_ids.json back

    steps:
      - name: Install fonts
        run: |
          sudo apt-get install -y fonts-dejavu-core fonts-noto-color-emoji
          pip install pilmoji

      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.8'

      - name: Install sys dependencies
        run: sudo apt-get install -y ffmpeg

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run automation
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHANNELS: ${{ secrets.TELEGRAM_CHANNELS }}
          YOUTUBE_CLIENT_SECRETS: ${{ secrets.YOUTUBE_CLIENT_SECRETS }}
        run: python short_creator.py

      - name: Save state files back to repo
        run: |
          git config user.name  "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add .published_ids.json
          git diff --cached --quiet || git commit -m "chore: update state files [skip ci]"
          git push
```

> `[skip ci]` in the commit message prevents the push from triggering another workflow run.

**Required repo setting:** `Settings → Actions → General → Workflow permissions → Read and write permissions`

-----

## State File Setup

**First time:**

```bash
echo "[]" > .published_ids.json
git add .published_ids.json
git commit -m "chore: add initial state file for duplicate prevention"
git push
```

**Reset (reprocess old posts):**

```bash
echo "[]" > .published_ids.json
git add .published_ids.json
git commit -m "chore: reset duplicate prevention state"
git push
```

-----

## File Structure

```
shortcreator/
├── .github/
│   └── workflows/
│       └── shortcreator.yml
├── .published_ids.json     ← committed, updated each run
├── .gitignore
├── README.md
├── brand_logo.png          ← overlaid top-right on every frame
├── music.mp3               ← default background music
├── requirements.txt
└── short_creator.py
```

-----

## Dependencies

|Package                                  |Purpose                             |
|-----------------------------------------|------------------------------------|
|`moviepy`                                |Video assembly, audio mixing        |
|`pillow`                                 |Image processing, caption rendering |
|`numpy`                                  |Frame array manipulation            |
|`requests`                               |Telegram API, music download        |
|`edge-tts`                               |Vietnamese text-to-speech           |
|`google-auth`, `google-api-python-client`|YouTube Data API v3                 |
|`pilmoji`                                |Emoji-aware font rendering          |
|`ffmpeg` (system)                        |TTS speed-up (1.25×), video encoding|
