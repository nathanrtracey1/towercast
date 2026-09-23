# TowerCast Project Handoff & Architecture Guide

> **Date**: September 22, 2026  
> **Status**: Core Implementation Complete, Tested, and Ready for Deployment  
> **Target OS**: macOS (Apple Silicon / M4 MacBook Air)  
> **Project Directory**: `/Users/nathantracey/.gemini/antigravity/scratch/TowerCast/`

---

## 1. Executive Summary & Purpose
TowerCast is a standalone Python daemon and CLI suite that converts **The Dice Tower** YouTube channel (or any YouTube channel/playlist) into a genuine, private podcast RSS feed (`feed.xml`) tailored for **Overcast** (and Apple Podcasts) on iOS and macOS.

### Key Solved Problems
1. **No Cloud Fees**: Replaces third-party subscription converters (like Pigeon Pod) by running natively on the user's M4 Mac using `yt-dlp` and `ffmpeg`.
2. **Shorts Exclusion**: Automatically identifies and excludes YouTube Shorts ($\le$ 60 seconds).
3. **Automated vs. Manual Curation**:
   - **Auto-prepares** videos matching target show titles: `"Board Game Breakfast"`, `"Dice Tower News"`, `"Crowdsurfing"`, `"Top 10"`.
   - **Pending Review List**: Collects all other non-matching regular videos into a curation queue.
4. **Interactive Interfaces**:
   - Terminal picker (`python3 tower_cast.py pick`) with numbered multi-select.
   - Modern local Web Dashboard (`http://localhost:8080`) with 1-click downloads and an in-browser audio preview player.
5. **Podcast Tagging**: Embeds video thumbnails as JPEG cover art, chapter markers into the audio container, and formats chapter timestamps into podcast show notes.
6. **Overcast Option A Support**: Overcast requires a publicly accessible HTTPS feed. TowerCast includes `tunnel.py` to launch a free Cloudflare Quick Tunnel (`*.trycloudflare.com`) that automatically registers into `config.json`.

---

## 2. Project Layout
```
/Users/nathantracey/.gemini/antigravity/scratch/TowerCast/
├── config.json                     # Main configuration (keywords, base URL, format, port)
├── tower_cast.py                   # Main CLI (sync, pick, serve, daemon, status)
├── tunnel.py                       # Option A Cloudflare Quick Tunnel launcher
├── engine/
│   ├── __init__.py
│   ├── database.py                 # SQLite engine (episodes table, statuses, chapters)
│   ├── youtube.py                  # yt-dlp flat-playlist scraper, shorts detector, keyword classifier
│   ├── downloader.py               # Audio extraction (-f bestaudio), --convert-thumbnails jpg, chapters
│   └── feed_generator.py           # Valid RFC 822 / iTunes RSS 2.0 XML generator
├── server/
│   ├── __init__.py
│   ├── web.py                      # Bottle web server with HTTP Range byte support (206 Partial Content)
│   └── templates/
│       └── index.html              # Dark-mode Apple/macOS-styled dashboard
├── launchd/
│   ├── com.towercast.daemon.plist  # Native macOS LaunchAgent definition
│   ├── install.sh                  # One-click install to ~/Library/LaunchAgents
│   └── uninstall.sh                # Clean uninstaller
├── tests/
│   └── test_towercast.py           # Unit tests (database CRUD, shorts, keywords, feed XML)
├── data/
│   ├── towercast.db                # SQLite database (auto-created)
│   ├── audio/                      # Downloaded audio & thumbnails by video ID
│   ├── towercast.log               # Background daemon standard log
│   └── towercast_err.log           # Background daemon error log
├── README.md                       # User documentation & Overcast setup guide
└── TOWERCAST_HANDOFF.md            # THIS FILE (Comprehensive developer handoff)
```

---

## 3. How the Pipeline Works

```
                        [ YouTube Channel ]
                                │
                                ▼
                     yt-dlp --flat-playlist
                                │
             ┌──────────────────┴──────────────────┐
             ▼                                     ▼
      Duration ≤ 60s?                       Duration > 60s
     (YouTube Shorts)                              │
             │                                     ▼
        [ SKIPPED ]                      Title Keyword Match?
                                         ("Board Game Breakfast",
                                          "Dice Tower News",
                                          "Crowdsurfing", "Top 10")
                                                   │
                                      ┌────────────┴────────────┐
                                      ▼                         ▼
                                    Matched?                 No Match?
                                      │                         │
                                      ▼                         ▼
                                 [ QUEUED ]                [ PENDING ]
                                      │                         │
                                      ▼                         ▼
                           yt-dlp Audio Extract        CLI / Web Dashboard
                          + Cover Art (JPG)            "Pick & Choose"
                          + Embedded Chapters                   │
                                      │                         ▼
                                      ▼                   User selects
                                  [ READY ] ─────────────► Add to Podcast
                                      │
                                      ▼
                             Update `feed.xml`
                                      │
                                      ▼
                        Served to Overcast / Safari
```

---

## 4. Current State & Verification
- **Unit Tests**: Pass completely with `python3 -m unittest discover -s tests -p "test_*.py"`.
- **System Environment**:
  - Python: `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` (v3.14.4)
  - ffmpeg: `/opt/homebrew/bin/ffmpeg`
  - ffprobe: `/opt/homebrew/bin/ffprobe`
  - yt-dlp: `/Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp`
  - bottle: installed
- **Key Settings in `engine/downloader.py`**:
  - `-f bestaudio/best` (prevents downloading massive video files, fetches pure audio stream).
  - `--convert-thumbnails jpg` (ensures YouTube webp thumbnails convert to JPEG so they can embed into `.m4a` / `.mp3`).
  - `--ffmpeg-location /opt/homebrew/bin` (ensures ffmpeg and ffprobe are found).

---

## 5. Next Steps for Next Model / Developer
1. **Option A Setup with Overcast**:
   - Run `brew install cloudflared` if not already installed.
   - Run `python3 tunnel.py` to get an instant HTTPS URL (e.g. `https://xxx.trycloudflare.com`).
   - Add `https://xxx.trycloudflare.com/feed.xml` to Overcast via **Add by URL**.
2. **Background Automation**:
   - Run `./launchd/install.sh` to install the service into macOS LaunchAgents so it runs permanently on boot/login.
3. **Extending Auto-Keywords**:
   - Simply edit `config.json`'s `auto_include_keywords` array to include any other series (e.g. `"Top 100"`, `"Tom Vasel Review"`).
