# TowerCast 🎲

> **Local Podcast Feed Generator for The Dice Tower**
> A lightweight, standalone Python daemon for macOS (optimized for Apple Silicon / M4) that turns YouTube channels into a genuine, private podcast RSS feed compatible with Overcast and Apple Podcasts.

---

## Features

- **Private Podcast RSS Feed (`feed.xml`)**: Produces an RFC 822 & iTunes-compliant podcast feed with enclosure URLs, durations, album art, and show notes.
- **Shorts Filter**: Automatically detects and excludes YouTube Shorts ($\le$ 60 seconds).
- **Smart Auto-Prep Rules**: Automatically downloads and tags shows matching your favorite keywords:
  - *Board Game Breakfast*
  - *Dice Tower News*
  - *Crowdsurfing*
  - *Top 10*
- **Pick & Choose (Manual Curation)**: Any video that doesn't match auto-rules is held in a "Pending Review" list. You can pick and choose episodes via:
  - An interactive terminal selector (`python3 tower_cast.py pick`)
  - A modern, local web dashboard at `http://localhost:8080`
- **Metadata & Chapters**: Tags audio with video thumbnails as cover art, video descriptions as show notes, and embeds chapter markers so you can jump between topics right in Overcast.
- **Purely Local & Lightweight**: Zero cloud subscriptions (no Pigeon Pod fees). Built on `yt-dlp` and `ffmpeg`.
- **Native macOS Daemon**: Includes a `LaunchAgent` configuration to run silently in the background on your M4 MacBook Air at login.

---

## Option A: Overcast Setup Guide (Public HTTPS URL)

Because Overcast's cloud engine crawls RSS feeds and downloads audio from the web, your feed needs a publicly reachable HTTPS URL. TowerCast provides **Option A** using **Cloudflare Quick Tunnels** (completely free, zero-config, no domain needed).

### Step 1: Install Cloudflare Tunnel (One-time)
If you have Homebrew installed:
```bash
brew install cloudflared
```

### Step 2: Start TowerCast & Quick Tunnel
In one terminal, start the server or daemon:
```bash
python3 tower_cast.py daemon
```

In another terminal (or run as background service):
```bash
python3 tunnel.py
```
`tunnel.py` will automatically:
1. Generate an instant, secure HTTPS URL (e.g., `https://random-words.trycloudflare.com`).
2. Update `config.json` with this URL.
3. Display your one-time Overcast Feed URL.

### Step 3: Add to Overcast
1. Open **Overcast** on your iPhone / iPad / Mac.
2. Tap the **+** (Add Podcast) button in the upper-right corner.
3. Tap **Add URL**.
4. Paste your `https://your-tunnel.trycloudflare.com/feed.xml`.
5. Tap **Add** — your Dice Tower episodes will appear with full artwork, show notes, and chapter markers!

*(Note: If you use Tailscale, you can also run `tailscale funnel 8080` and set `"public_base_url"` in `config.json`.)*

---

## Quick Start & Commands

```bash
# 1. Sync channel & auto-download matching shows
python3 tower_cast.py sync

# 2. Pick & choose from other recent videos in the terminal
python3 tower_cast.py pick

# 3. Start local web dashboard & feed server (http://localhost:8080)
python3 tower_cast.py serve

# 4. Run background daemon (syncs every hour + serves feed)
python3 tower_cast.py daemon

# 5. Check current status & feed URL
python3 tower_cast.py status
```

---

## Web Dashboard

Start the server (`python3 tower_cast.py serve` or `daemon`) and open:
👉 **[http://localhost:8080](http://localhost:8080)**

The dashboard features:
- **One-click URL Copy**: Quickly copy your live feed URL for Overcast.
- **Pick & Choose Grid**: Browse thumbnails and durations of recent uncurated videos, with 1-click **Add to Podcast** and **Skip** buttons.
- **In-Browser Audio Player**: Preview any downloaded episode directly in Safari with full scrubber and duration indicators.
- **Manual Sync**: Trigger a channel refresh anytime.

---

## Running as a macOS Background Daemon (LaunchAgent)

To run TowerCast continuously in the background on your M4 MacBook Air (so it checks for new shows and keeps the feed active even when your terminal is closed):

```bash
# Install and start the LaunchAgent:
./launchd/install.sh

# View live logs:
tail -f data/towercast.log

# To stop and uninstall:
./launchd/uninstall.sh
```

---

## Configuration (`config.json`)

You can customize rules and preferences in `config.json`:

```json
{
  "channel_url": "https://www.youtube.com/@TheDiceTower/videos",
  "auto_include_keywords": [
    "Board Game Breakfast",
    "Dice Tower News",
    "Crowdsurfing",
    "Top 10"
  ],
  "exclude_shorts": true,
  "shorts_max_seconds": 60,
  "audio_format": "m4a",
  "audio_quality": "192k",
  "server_host": "0.0.0.0",
  "server_port": 8080,
  "public_base_url": "http://localhost:8080",
  "sync_interval_minutes": 60,
  "max_episodes_in_feed": 50
}
```

- Add any show title keywords to `auto_include_keywords` (e.g. `"Tom's Top 100"`, `"Crowdfunding"`).
- Change `sync_interval_minutes` to check YouTube more or less frequently.
