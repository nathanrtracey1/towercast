# TowerCast

Private podcast feed generator that turns a YouTube channel (configured for **The Dice Tower**) into an Overcast- and Apple Podcasts-compatible RSS feed (`feed.xml`) with artwork, show notes, chapter markers, and byte-range streaming.

---

## How It Works

TowerCast runs primarily as a serverless pipeline on **Cloudflare Workers + Cloudflare R2 + GitHub Actions**, with an optional local macOS CLI and web dashboard.

1. **New-Video Watcher (`worker/src/index.js`)**: A Cloudflare Worker runs on a 10-minute cron (`*/10 * * * *`) checking YouTube's RSS feeds (and optionally the YouTube Data API). When an unhandled, non-Short video is ready, it dispatches the GitHub Actions workflow. Upcoming and active live streams are held until they finish.
2. **Download & Packaging (`github/feed_builder.py`, `.github/workflows/towercast.yml`)**: GitHub Actions scans channel uploads and live streams, skips YouTube Shorts (`<= 60` seconds), extracts `m4a` audio (`192k`) using `yt-dlp` and `ffmpeg`, uploads audio to **Cloudflare R2**, and saves episode metadata and chapters to **GitHub Releases** (`ep-<video_id>`).
3. **Private Feed & Streaming**: The Worker serves `/feed.xml` and the web dashboard (`/`) behind HTTP Basic Auth (`FEED_USER` / `FEED_PASS`), and serves audio from R2 at `/audio/<AUDIO_TOKEN>/<video_id>.m4a` with HTTP `206` byte-range support for seeking in Overcast.
4. **Rolling Feed & Overcast Ping**: Maintains up to `max_episodes_in_feed` (`50`) episodes (auto-pruning older episodes while keeping manually added ones) and pings Overcast when the feed updates.
5. **Fallback Schedule**: GitHub Actions also runs a 3-hourly cron (`23 */3 * * *`) as a safety net.

---

## Features

- **Private RSS Feed (`feed.xml`)**: RFC 822 & iTunes-compliant feed protected by HTTP Basic Auth, with token-authenticated audio enclosure URLs.
- **Uploads + Live Streams, No Shorts**: Scans both channel uploads and live streams while automatically skipping YouTube Shorts (`<= 60s`).
- **Auto-Download & Manual Curation**: Configured with `auto_download_all_new: true` to pull new videos automatically, plus dashboard and workflow support for adding any YouTube URL, queuing, or deleting episodes.
- **Metadata & Chapters**: Embeds video thumbnails as artwork, descriptions as show notes, and YouTube chapters for navigation in podcast players.
- **Web Dashboard**: Manage episodes, add videos by URL, and preview audio. In cloud mode it triggers GitHub Actions directly (or via owner-opened GitHub Issues).

---

## Cloud Setup (Cloudflare R2 + Worker + GitHub Actions)

1. **Cloudflare R2**: Create a bucket named `towercast` and an R2 API token with Object Read & Write permissions.
2. **Generate Secrets**: Create values (e.g. with `openssl rand -hex 24`) for `FEED_USER`, `FEED_PASS`, and `AUDIO_TOKEN`.
3. **GitHub Token for Worker**: Create a fine-grained Personal Access Token scoped to this repository with **Actions: Read and write** (`GH_DISPATCH_TOKEN`).
4. **Deploy the Worker** (`worker/wrangler.toml` sets `YT_CHANNEL_ID` and `GH_REPO`):
   ```bash
   cd worker
   npx wrangler deploy
   npx wrangler secret put FEED_USER
   npx wrangler secret put FEED_PASS
   npx wrangler secret put AUDIO_TOKEN
   npx wrangler secret put GH_DISPATCH_TOKEN
   ```
5. **GitHub Actions Secrets** (Repository Settings -> Secrets and variables -> Actions):
   - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
   - `FEED_BASE_URL` (your `https://towercast.<subdomain>.workers.dev` URL)
   - `AUDIO_TOKEN`, `FEED_USER`, `FEED_PASS`
   - `YOUTUBE_COOKIES` (Netscape-format cookies if needed for `yt-dlp`)
6. **Build Initial Feed**: Run the **TowerCast Sync** workflow (`sync` action) from GitHub Actions.
7. **Subscribe in Overcast**: Tap **+** -> **Add URL** and enter:
   ```text
   https://<FEED_USER>:<FEED_PASS>@towercast.<subdomain>.workers.dev/feed.xml
   ```

See [SETUP.md](SETUP.md) and [TOWERCAST_HANDOFF.md](TOWERCAST_HANDOFF.md) for additional setup and migration notes.

---

## Local CLI & Dashboard (Optional)

You can also run TowerCast locally on macOS (`yt-dlp` and `ffmpeg` required):

```bash
# Scan channel, download queued episodes, and publish to R2 (if R2 env vars are set)
python3 tower_cast.py sync

# Interactive terminal picker for pending videos
python3 tower_cast.py pick

# Start local feed server and web dashboard (http://localhost:8080)
python3 tower_cast.py serve
python3 tower_cast.py serve --tunnel   # expose via Cloudflare Quick Tunnel

# Run continuous background sync + local server
python3 tower_cast.py daemon

# Show current local database status and feed URL
python3 tower_cast.py status

# Upload local ready episodes to R2 / GitHub Releases and trigger feed rebuild
python3 tower_cast.py publish
```

For macOS `LaunchAgent` installation, see `./launchd/install.sh` and `./launchd/uninstall.sh`.

---

## Configuration (`config.json`)

```json
{
  "channel_url": "https://www.youtube.com/@TheDiceTower/videos",
  "auto_download_all_new": true,
  "exclude_shorts": true,
  "shorts_max_seconds": 60,
  "audio_format": "m4a",
  "audio_quality": "192k",
  "server_host": "0.0.0.0",
  "server_port": 8080,
  "public_base_url": "http://localhost:8080",
  "sync_interval_minutes": 60,
  "max_episodes_in_feed": 50,
  "auto_prune_old_episodes": true,
  "private_feed": true,
  "feed_title": "The Dice Tower (TowerCast)",
  "feed_description": "Private audio podcast feed generated from The Dice Tower YouTube channel via TowerCast.",
  "feed_author": "The Dice Tower",
  "github": {
    "enabled": true,
    "username": "nathanrtracey1",
    "repo": "nathanrtracey1/towercast",
    "pages_url": ""
  }
}
```

---

## Project Structure

- `tower_cast.py` - Local CLI entrypoint (`sync`, `pick`, `serve`, `daemon`, `status`, `github-setup`, `publish`)
- `engine/` - SQLite database (`database.py`), YouTube scanner (`youtube.py`), `yt-dlp`/`ffmpeg` downloader (`downloader.py`), and RSS XML builder (`feed_generator.py`)
- `github/` - CI feed builder (`feed_builder.py`), R2 storage client (`r2.py`), release/R2 publisher (`publish.py`), GitHub API helper (`github_api.py`), and migration script (`migrate_to_r2.py`)
- `worker/` - Cloudflare Worker (`src/index.js`, `wrangler.toml`) for auth, R2 byte-range audio serving, and 10-minute YouTube watcher
- `server/` - Local Bottle web server (`web.py`) and dashboard UI (`templates/index.html`)
- `launchd/` - macOS LaunchAgent plists and install/uninstall scripts
- `tests/` - Unit tests (`test_towercast.py`)
