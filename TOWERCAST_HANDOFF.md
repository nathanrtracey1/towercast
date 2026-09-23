# TowerCast — Developer / Model Handoff

> **Updated**: September 23, 2026  
> **Sessions**: 3 (build → GitHub deploy → UI redesign + always-on CI)  
> **Project directory**: `/Users/nathantracey/.gemini/antigravity/scratch/TowerCast/`  
> **GitHub repo**: `https://github.com/nathanrtracey1/towercast`  
> **Target**: macOS M4 MacBook Air + GitHub Actions (always-on)

---

## 1. What TowerCast Is

A self-contained Python tool that converts **The Dice Tower** YouTube channel into a private, always-on podcast RSS feed compatible with **Overcast** (Add by URL). It:

- Auto-downloads episodes matching configurable keyword rules (Board Game Breakfast, Dice Tower News, Crowdsurfing, Top 10, and any user-added Favorites)
- Excludes YouTube Shorts (≤ 60s)
- Puts everything else in a curation queue you pick from the web dashboard or CLI
- Embeds cover art (JPEG), metadata, and chapter markers into `.m4a` audio files
- Serves the feed both **locally** (Bottle HTTP server) and **always-on via GitHub** (even when Mac is off)

---

## 2. Full File Map

```
TowerCast/
├── config.json                       # All settings: keywords, favorites, github, port, format
├── tower_cast.py                     # CLI: sync / pick / serve / daemon / status /
│                                     #   github-setup / favorites-list / favorites-add
├── tunnel.py                         # Cloudflare Quick Tunnel (Option A fallback, local-only)
│
├── engine/
│   ├── database.py                   # SQLite: episodes table, status machine
│   ├── youtube.py                    # yt-dlp flat-playlist scanner, shorts detector,
│   │                                 #   keyword classifier — supports favorites_config
│   ├── downloader.py                 # yt-dlp audio extraction, resilient error detection
│   │                                 #   (checks for output file, not just exit code)
│   └── feed_generator.py            # iTunes RSS 2.0 — two variants:
│                                     #   generate_feed_xml()              ← local server
│                                     #   generate_feed_xml_with_full_urls() ← GitHub CI
│
├── github/
│   ├── github_api.py                 # GitHub REST API v3 wrapper (stdlib urllib only)
│   ├── feed_builder.py               # Main CI script — runs in GitHub Actions
│   └── setup_wizard.py              # Interactive `github-setup` wizard
│
├── .github/workflows/towercast.yml  # GitHub Actions: daily cron + manual dispatch
│
├── server/
│   ├── web.py                        # Bottle server, HTTP 206 range support
│   └── templates/index.html         # Redesigned dark dashboard (Apple HIG + frontend-design skill)
│
├── launchd/                          # macOS LaunchAgent (local daemon, optional)
│   ├── com.towercast.daemon.plist
│   ├── install.sh
│   └── uninstall.sh
│
├── tests/test_towercast.py          # 4 passing unit tests
├── data/audio/                       # Downloaded audio by video_id/
├── data/towercast.db                 # SQLite DB (local mode only)
├── README.md
└── TOWERCAST_HANDOFF.md             # THIS FILE
```

---

## 3. Two Operating Modes

### Mode A — Local (Mac must be on)
```bash
cd /Users/nathantracey/.gemini/antigravity/scratch/TowerCast
python3 tower_cast.py sync --limit 30   # scan + download auto-matched episodes
python3 tower_cast.py serve             # start web dashboard + feed at localhost:8080
python3 tunnel.py                       # expose feed via Cloudflare Quick Tunnel HTTPS URL
```
Feed URL changes every time `tunnel.py` restarts. Good for testing; not permanent.

### Mode B — Always-On GitHub (Mac can be off) ← PRIMARY
- **Feed URL** (permanent, never changes): `https://nathanrtracey1.github.io/towercast/feed.xml`
- **Audio storage**: GitHub Releases (one release per episode, tagged `ep-{video_id}`)
- **Feed hosting**: GitHub Pages (`gh-pages` branch, `feed.xml` + `index.html`)
- **Compute**: GitHub Actions cron `0 10 * * *` (6am EDT daily) + manual dispatch
- **State**: GitHub Releases are the source of truth — no DB in CI

---

## 4. Current Status (as of September 23, 2026)

### ✅ Working
| Component | Status |
|---|---|
| 4/4 unit tests | ✅ Passing |
| Local sync + download | ✅ Working — `data/audio/0zvDGuYD5yo/0zvDGuYD5yo.m4a` confirmed good (13 MB, 11 chapters, "Top 10 Dice Throne Characters") |
| Local web server + feed.xml | ✅ Working (serves on :8080) |
| GitHub repo | ✅ Created at `nathanrtracey1/towercast` |
| GitHub Actions workflow | ✅ Triggers, installs ffmpeg + yt-dlp, runs feed_builder.py |
| GitHub Pages | ✅ Enabled on `gh-pages` branch, `feed.xml` returns HTTP 200 |
| feed.xml structure | ✅ Valid iTunes RSS 2.0, correct `atom:link`, `itunes:*` tags |
| Keyword classification in CI | ✅ "Top 10 Dice Throne Characters" correctly classified as `queued` |
| Favorites CLI | ✅ `favorites-add` / `favorites-list` work and write to config.json |
| New dark UI | ✅ Apple HIG + frontend-design skill applied |

### ❌ Broken — CI Downloads Fail (Two Root Causes)
The GitHub Actions run completes successfully but produces **0 episodes** in the feed. The download step logs show two errors:

**Error 1 — Wrong ffmpeg path on Linux:**
```
WARNING: ffmpeg-location /opt/homebrew/bin does not exist! Continuing without ffmpeg
```
`/opt/homebrew/bin` is a macOS Homebrew path. On Ubuntu runners, ffmpeg is at `/usr/bin/ffmpeg`. The `--ffmpeg-location` flag in `engine/downloader.py` line 40 needs to be dynamic.

**Fix**: Detect the OS or find ffmpeg via `shutil.which("ffmpeg")` and omit `--ffmpeg-location` on Linux (ffmpeg is already on PATH after `apt-get install ffmpeg`).

**Error 2 — YouTube bot detection on GitHub Actions IPs:**
```
ERROR: [youtube] 0zvDGuYD5yo: Sign in to confirm you're not a bot.
Use --cookies-from-browser or --cookies for the authentication.
```
GitHub Actions runner IPs are flagged by YouTube. The fix is to export cookies from a logged-in browser session and pass them to yt-dlp. The standard approach:

1. Export `cookies.txt` from Chrome/Firefox (using a browser extension like "Get cookies.txt LOCALLY")
2. Store the file contents as a GitHub repository secret named `YOUTUBE_COOKIES`
3. In the workflow: write the secret to a temp file, pass `--cookies /tmp/cookies.txt` to yt-dlp

---

## 5. Exact Fixes Needed (Next Session)

### Fix 1: Dynamic ffmpeg path in `engine/downloader.py`

Replace lines 38-40 (the `--convert-thumbnails` + `--ffmpeg-location` block) with:

```python
import shutil, platform

# Locate ffmpeg — Homebrew on macOS, system PATH on Linux
ffmpeg_dir = None
ffmpeg_bin = shutil.which("ffmpeg")
if ffmpeg_bin:
    ffmpeg_dir = os.path.dirname(ffmpeg_bin)
elif platform.system() == "Darwin":
    ffmpeg_dir = "/opt/homebrew/bin"

cmd = [
    "yt-dlp",
    "-f", "bestaudio/best",
    "--extract-audio",
    "--audio-format", self.audio_format,
    "--audio-quality", self.audio_quality,
    "--convert-thumbnails", "jpg",
    "--embed-thumbnail",
    "--embed-metadata",
    "--embed-chapters",
    "--write-thumbnail",
    "--write-info-json",
    "--no-playlist",
]
if ffmpeg_dir:
    cmd += ["--ffmpeg-location", ffmpeg_dir]

# Inject cookies file if provided
if self.cookies_file and os.path.exists(self.cookies_file):
    cmd += ["--cookies", self.cookies_file]

cmd += ["-o", output_template, video_url]
```

Also add `cookies_file: Optional[str] = None` to `Downloader.__init__`.

### Fix 2: Cookies support in `github/feed_builder.py`

In `download_and_upload()`, pass `cookies_file` to `Downloader`:

```python
cookies_path = None
cookies_content = os.environ.get("YOUTUBE_COOKIES")
if cookies_content:
    cookies_path = "/tmp/yt_cookies.txt"
    with open(cookies_path, "w") as f:
        f.write(cookies_content)

dl = Downloader(
    media_dir=tmpdir,
    audio_format=config.get("audio_format", "m4a"),
    audio_quality=config.get("audio_quality", "192k"),
    cookies_file=cookies_path,
)
```

### Fix 3: Add `YOUTUBE_COOKIES` secret to `.github/workflows/towercast.yml`

```yaml
- name: Run TowerCast feed builder
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
    GITHUB_REPOSITORY: ${{ github.repository }}
    SCAN_LIMIT: ${{ github.event.inputs.scan_limit || '30' }}
    YOUTUBE_COOKIES: ${{ secrets.YOUTUBE_COOKIES }}    # ← add this line
  run: python3 github/feed_builder.py
```

### Fix 4: User must add the secret (requires manual step in browser)

1. Export `cookies.txt` from a YouTube-logged-in Chrome session:  
   Install extension **"Get cookies.txt LOCALLY"** → visit youtube.com → click extension → Export
2. Go to `https://github.com/nathanrtracey1/towercast/settings/secrets/actions`
3. Click **New repository secret** → Name: `YOUTUBE_COOKIES` → paste file contents → Save
4. Re-run the workflow

---

## 6. Environment Facts

| Tool | Path | Version |
|---|---|---|
| Python | `/Library/Frameworks/Python.framework/Versions/3.14/bin/python3` | 3.14.4 |
| yt-dlp | `/Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp` | 2026.8.19 |
| ffmpeg | `/opt/homebrew/bin/ffmpeg` | ✅ |
| ffprobe | `/opt/homebrew/bin/ffprobe` | ✅ |
| bottle | installed | 0.13.4 |
| gh CLI | installed | authenticated as `nathanrtracey1` |
| cloudflared | ❌ not installed | needed for local tunnel only |

---

## 7. Key Architecture Decisions

### GitHub Releases as audio CDN
- Each episode → one release, tagged `ep-{video_id}`
- Audio `.m4a` uploaded as release asset → served from `github.com/.../releases/download/...` CDN
- Episode metadata (title, duration, chapters, thumbnail URL) encoded as a JSON comment in the release body (`<!-- TOWERCAST_META {...} -->`)
- `feed_builder.py` reconstructs all episodes by listing releases + parsing bodies — **no DB in CI**

### Favorites system
```json
"favorites": {
  "enabled": true,
  "shows": [
    { "keyword": "Zee Garcia", "max_per_sync": 3 },
    { "keyword": "Heavy Cardboard" }
  ]
}
```
- `favorites.shows` keywords are auto-downloaded just like `auto_include_keywords`
- Optional `max_per_sync` cap: if set, the CI builder counts how many of that keyword it has downloaded in the current run and stops at the cap
- Add via CLI: `python3 tower_cast.py favorites-add "Keyword" --max-per-sync 2`
- After adding, push to GitHub: `git add config.json && git commit -m "Add favorite" && git push`

### Downloader resilience
`engine/downloader.py` was rewritten to check for the **output file** rather than yt-dlp's **exit code**. yt-dlp exits non-zero on warnings (JS runtime missing, webp thumbnail quirks) but still produces valid audio. The old code was triggering false failures.

### Feed generation — two paths
- `FeedGenerator.generate_feed_xml()` — local server path, constructs audio URL as `{public_base_url}/audio/{filename}`
- `FeedGenerator.generate_feed_xml_with_full_urls()` — CI path, uses `ep["audio_url"]` (GitHub CDN URL) directly in the `<enclosure>` tag
- Shared via `_add_episode_item(use_full_audio_url: bool)` helper

---

## 8. Web Dashboard

Redesigned in last session using:
- **Anthropic `frontend-design` skill** — distinctive point of view, no blue/purple defaults
- **Apple Human Interface Guidelines** — SF Pro type stack, semantic label layers, rounded rectangles, thin material glassmorphism
- **Amber ochre accent** (`#d4883a`) — the color of a wooden dice tray; no blue anywhere
- **macOS sidebar layout** — mirrors Finder/Music, sidebar nav left, content right
- **Single `fadeUp` entrance animation** only — no scattered hover effects (per skill guidance)

Template: `server/templates/index.html`

---

## 9. Config Reference (`config.json`)

```json
{
  "channel_url": "https://www.youtube.com/@TheDiceTower/videos",
  "auto_include_keywords": ["Board Game Breakfast", "Dice Tower News", "Crowdsurfing", "Top 10"],
  "favorites": {
    "enabled": true,
    "shows": []
  },
  "exclude_shorts": true,
  "shorts_max_seconds": 60,
  "audio_format": "m4a",
  "audio_quality": "192k",
  "server_host": "0.0.0.0",
  "server_port": 8080,
  "public_base_url": "http://localhost:8080",
  "sync_interval_minutes": 60,
  "max_episodes_in_feed": 50,
  "feed_title": "The Dice Tower (TowerCast)",
  "feed_description": "...",
  "feed_author": "The Dice Tower",
  "feed_image_url": "https://yt3.googleusercontent.com/...",
  "github": {
    "enabled": true,
    "username": "nathanrtracey1",
    "repo": "nathanrtracey1/towercast",
    "pages_url": "https://nathanrtracey1.github.io/towercast"
  }
}
```

---

## 10. After the Cookies Fix — Expected Behavior

Once `YOUTUBE_COOKIES` is set and the downloader uses dynamic ffmpeg path:

1. Daily at 6am EDT: workflow triggers automatically
2. Scans last 30 Dice Tower videos
3. Auto-downloads any matching `auto_include_keywords` or `favorites.shows` not already in Releases
4. Uploads audio to `github.com/nathanrtracey1/towercast/releases`
5. Rebuilds `feed.xml` from all releases
6. Deploys to `https://nathanrtracey1.github.io/towercast/feed.xml`
7. Overcast picks it up on next poll (every 24h by default, or force-refresh)

User's Mac never needs to be on. The Overcast feed URL never changes.

---

## 11. Remaining Optional Work
- [ ] **LaunchAgent install** (`./launchd/install.sh`) — makes local daemon start at login if user ever wants the local server permanently
- [ ] **Cloudflared install** (`brew install cloudflared` + `python3 tunnel.py`) — only needed if user wants to use the local server path instead of GitHub
- [ ] **Overcast subscription** — once feed has episodes, paste `https://nathanrtracey1.github.io/towercast/feed.xml` into Overcast → + → Add URL
- [ ] **Expand keywords** — add more show titles to `auto_include_keywords` or use `python3 tower_cast.py favorites-add`
