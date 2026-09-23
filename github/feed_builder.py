#!/usr/bin/env python3
"""
TowerCast GitHub CI Feed Builder
=================================
Runs inside GitHub Actions to:
  1. Scan the Dice Tower YouTube channel via yt-dlp
  2. Check existing GitHub Releases to skip already-downloaded episodes
  3. Download + tag new matching audio files
  4. Upload audio to GitHub Releases (free CDN)
  5. Rebuild feed.xml from all releases
  6. Write feed.xml + a simple status page to ./gh-pages-output/ for GitHub Pages deployment

Environment variables (all provided automatically by GitHub Actions):
  GITHUB_TOKEN       - Actions token with contents:write permission
  GITHUB_REPOSITORY  - "owner/repo"
  SCAN_LIMIT         - optional, defaults to 30
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# Add project root to path so we can import engine/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.youtube import YouTubeEngine
from engine.downloader import Downloader
from engine.feed_generator import FeedGenerator, format_duration, parse_to_rfc822
from github.github_api import GitHubAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TowerCast-CI")

RELEASE_TAG_PREFIX = "ep-"


def load_config() -> Dict[str, Any]:
    cfg_path = ROOT / "config.json"
    with open(cfg_path) as f:
        return json.load(f)


def tag_for(video_id: str) -> str:
    return f"{RELEASE_TAG_PREFIX}{video_id}"


def video_id_from_tag(tag: str) -> Optional[str]:
    if tag.startswith(RELEASE_TAG_PREFIX):
        return tag[len(RELEASE_TAG_PREFIX):]
    return None


def build_release_body(meta: Dict[str, Any]) -> str:
    """Encodes episode metadata into the release body for later feed reconstruction."""
    payload = {
        "id": meta.get("id"),
        "title": meta.get("title"),
        "duration": meta.get("duration"),
        "published_at": meta.get("published_at"),
        "thumbnail_url": meta.get("thumbnail_url"),
        "description": (meta.get("description") or "")[:4000],  # GH limit
        "chapters": meta.get("chapters", []),
        "matched_keyword": meta.get("matched_keyword"),
        "file_size": meta.get("file_size", 0),
    }
    return f"<!-- TOWERCAST_META\n{json.dumps(payload, indent=2)}\n-->"


def parse_release_body(body: str) -> Optional[Dict[str, Any]]:
    """Extracts TowerCast metadata from a release body comment."""
    import re
    m = re.search(r"<!-- TOWERCAST_META\n(.*?)\n-->", body or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def get_existing_episode_ids(api: GitHubAPI) -> Dict[str, Dict]:
    """
    Returns {video_id: release_json} for all TowerCast releases that have
    at least one audio asset successfully uploaded.
    """
    releases = api.list_releases(per_page=100)
    result = {}
    for rel in releases:
        vid_id = video_id_from_tag(rel.get("tag_name", ""))
        if vid_id and rel.get("assets"):
            result[vid_id] = rel
    return result


def episode_from_release(release: Dict) -> Optional[Dict[str, Any]]:
    """Reconstructs an episode dict from a GitHub Release for feed generation."""
    meta = parse_release_body(release.get("body", ""))
    if not meta:
        return None

    # Find the audio asset download URL
    audio_url = None
    file_size = 0
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.endswith((".m4a", ".mp3", ".aac", ".opus")):
            audio_url = asset.get("browser_download_url")
            file_size = asset.get("size", 0)
            break

    if not audio_url:
        return None

    return {
        "id": meta.get("id") or video_id_from_tag(release.get("tag_name", "")),
        "title": meta.get("title") or release.get("name"),
        "audio_url": audio_url,           # Full URL — used directly in enclosure
        "audio_filename": None,            # Not used when audio_url is set
        "file_size": meta.get("file_size") or file_size,
        "duration": meta.get("duration"),
        "published_at": meta.get("published_at") or release.get("published_at"),
        "thumbnail_url": meta.get("thumbnail_url"),
        "description": meta.get("description", ""),
        "chapters_json": json.dumps(meta.get("chapters", [])),
        "matched_keyword": meta.get("matched_keyword"),
    }


def should_auto_download(title: str, config: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Returns (should_download, matched_keyword_or_none).
    Checks auto_include_keywords first, then favorites.shows.
    """
    lower = title.lower()

    for kw in config.get("auto_include_keywords", []):
        if kw.lower() in lower:
            return True, kw

    fav_cfg = config.get("favorites", {})
    if fav_cfg.get("enabled", False):
        for show in fav_cfg.get("shows", []):
            kw = show.get("keyword", "")
            if kw and kw.lower() in lower:
                return True, kw

    return False, None


def download_and_upload(video_id: str, video_url: str, classified: Dict,
                        api: GitHubAPI, config: Dict) -> Optional[Dict[str, Any]]:
    """Downloads audio, uploads to a new GitHub Release, returns episode metadata."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dl = Downloader(
            media_dir=tmpdir,
            audio_format=config.get("audio_format", "m4a"),
            audio_quality=config.get("audio_quality", "192k"),
        )

        logger.info(f"⬇  Downloading: {classified['title']}")
        try:
            result = dl.download_episode(video_id, video_url)
        except Exception as e:
            logger.error(f"Download failed for {video_id}: {e}")
            return None

        audio_path = result["audio_full_path"]
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"No audio file after download for {video_id}")
            return None

        # Enrich with info we know from classify step
        result["thumbnail_url"] = classified.get("thumbnail_url")
        result["matched_keyword"] = classified.get("matched_keyword")

        # Create GitHub Release
        tag = tag_for(video_id)
        release_name = result.get("title") or classified["title"]
        release_body = build_release_body(result)

        logger.info(f"📦 Creating GitHub Release: {tag}")
        try:
            release = api.create_release(
                tag=tag,
                name=release_name,
                body=release_body,
            )
        except Exception as e:
            logger.error(f"Failed to create release for {video_id}: {e}")
            return None

        release_id = release["id"]
        audio_filename = os.path.basename(audio_path)
        content_type = "audio/mp4" if audio_filename.endswith(".m4a") else "audio/mpeg"

        logger.info(f"⬆  Uploading {audio_filename} ({result['file_size'] // (1024*1024)} MB)...")
        try:
            asset = api.upload_asset_from_file(release_id, audio_path, content_type)
        except Exception as e:
            logger.error(f"Upload failed for {video_id}: {e}")
            return None

        result["audio_url"] = asset["browser_download_url"]
        result["audio_filename"] = None
        result["file_size"] = asset.get("size", result["file_size"])

        logger.info(f"✅ {release_name} uploaded successfully")
        return result


def build_pages_output(episodes: List[Dict], config: Dict, pages_url: str) -> Path:
    """Generates gh-pages-output/ with feed.xml and index.html."""
    out = ROOT / "gh-pages-output"
    out.mkdir(exist_ok=True)

    # Patch config so FeedGenerator uses the Pages URL, not localhost
    gh_config = dict(config)
    gh_config["public_base_url"] = pages_url.rstrip("/")

    fg = FeedGenerator(gh_config)
    xml = fg.generate_feed_xml_with_full_urls(episodes)

    feed_path = out / "feed.xml"
    feed_path.write_text(xml, encoding="utf-8")
    logger.info(f"📄 Wrote feed.xml ({len(xml)} bytes, {len(episodes)} episodes)")

    # Simple but useful status page
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    episode_rows = ""
    for ep in episodes:
        dur = format_duration(ep.get("duration"))
        kw = ep.get("matched_keyword") or ""
        badge = f'<span class="badge">{kw}</span>' if kw else ""
        thumb = ep.get("thumbnail_url") or ""
        thumb_html = f'<img src="{thumb}" alt="">' if thumb else ""
        episode_rows += f"""
        <div class="ep">
          {thumb_html}
          <div class="ep-info">
            <div class="ep-title">{ep.get('title','')}</div>
            <div class="ep-meta">{dur} {badge}</div>
          </div>
        </div>"""

    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>TowerCast — The Dice Tower Private Feed</title>
  <style>
    :root {{--amber:#d4883a;--bg:#0a0a0c;--card:rgba(22,22,28,.85);--sep:rgba(255,255,255,.08);--text:rgba(255,255,255,.9);--sub:rgba(255,255,255,.5);}}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{background:var(--bg);color:var(--text);font-family:-apple-system,"SF Pro Display",sans-serif;padding:40px 24px 80px;max-width:680px;margin:0 auto}}
    h1{{font-size:28px;font-weight:700;letter-spacing:-.021em;margin-bottom:4px}}
    .sub{{color:var(--sub);font-size:14px;margin-bottom:32px}}
    .feed-box{{background:var(--card);border:1px solid var(--sep);border-radius:12px;padding:16px 20px;margin-bottom:32px}}
    .feed-label{{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--sub);margin-bottom:8px}}
    .feed-url{{font-family:"SF Mono",monospace;font-size:13px;color:var(--amber);word-break:break-all}}
    .feed-hint{{font-size:12px;color:var(--sub);margin-top:8px}}
    .ep{{display:flex;gap:14px;align-items:flex-start;padding:14px 0;border-bottom:1px solid var(--sep)}}
    .ep:last-child{{border-bottom:none}}
    .ep img{{width:120px;aspect-ratio:16/9;object-fit:cover;border-radius:6px;flex-shrink:0}}
    .ep-title{{font-size:14px;font-weight:600;line-height:1.35;margin-bottom:6px}}
    .ep-meta{{font-size:12px;color:var(--sub);display:flex;gap:8px;align-items:center}}
    .badge{{background:rgba(212,136,58,.18);color:var(--amber);border-radius:4px;padding:1px 7px;font-size:11px;font-weight:600}}
    h2{{font-size:17px;font-weight:600;margin-bottom:16px;letter-spacing:-.013em}}
    footer{{font-size:12px;color:var(--sub);margin-top:40px;text-align:center}}
    a{{color:var(--amber)}}
  </style>
</head>
<body>
  <h1>🎲 TowerCast</h1>
  <p class="sub">The Dice Tower · Private Podcast Feed · Last sync: {now}</p>

  <div class="feed-box">
    <div class="feed-label">Your Podcast Feed URL — Paste into Overcast</div>
    <div class="feed-url">{pages_url.rstrip("/")}/feed.xml</div>
    <div class="feed-hint">In Overcast: tap + → Add URL → paste above → Subscribe</div>
  </div>

  <h2>{len(episodes)} Episodes in Feed</h2>
  <div class="episodes">{episode_rows}</div>

  <footer>Generated by <a href="https://github.com">TowerCast</a> via GitHub Actions</footer>
</body>
</html>"""

    (out / "index.html").write_text(index_html, encoding="utf-8")
    logger.info(f"🌐 Wrote index.html")
    return out


def main():
    token = os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not gh_repo:
        logger.error("GITHUB_TOKEN and GITHUB_REPOSITORY must be set.")
        sys.exit(1)

    scan_limit = int(os.environ.get("SCAN_LIMIT", "30"))

    config = load_config()
    gh_cfg = config.get("github", {})
    pages_url = gh_cfg.get("pages_url", "").rstrip("/")
    if not pages_url:
        pages_url = f"https://{gh_repo.split('/')[0]}.github.io/{gh_repo.split('/')[1]}"
        logger.info(f"pages_url not set in config, using default: {pages_url}")

    api = GitHubAPI(token=token, repo=gh_repo)

    # ── Step 1: What's already uploaded? ─────────────────────────────────
    logger.info("📚 Checking existing GitHub Releases...")
    existing = get_existing_episode_ids(api)
    logger.info(f"Found {len(existing)} already-uploaded episodes in Releases.")

    # ── Step 2: Scan YouTube channel ─────────────────────────────────────
    yt = YouTubeEngine(
        channel_url=config["channel_url"],
        auto_keywords=config.get("auto_include_keywords", []),
        exclude_shorts=config.get("exclude_shorts", True),
        shorts_max_seconds=config.get("shorts_max_seconds", 60),
        favorites_config=config.get("favorites", {}),
    )

    logger.info(f"📡 Scanning channel: {config['channel_url']} (limit={scan_limit})")
    try:
        _, entries = yt.fetch_channel_entries(limit=scan_limit)
    except Exception as e:
        logger.error(f"Channel scan failed: {e}")
        sys.exit(1)

    logger.info(f"Found {len(entries)} entries. Classifying...")

    # Tracks favorites per-sync limits
    favorites_counts: Dict[str, int] = {}
    new_episodes_metadata: List[Dict] = []

    for entry in entries:
        classified = yt.classify_entry(entry)
        vid_id = classified["id"]

        if classified["is_short"]:
            continue
        if vid_id in existing:
            logger.info(f"  ✓ Already uploaded: {classified['title']}")
            continue
        if classified["target_status"] != "queued":
            logger.info(f"  ⏭ Skipping (pending/manual): {classified['title']}")
            continue

        # Check favorites max_per_sync cap
        kw = classified.get("matched_keyword", "")
        if kw and _is_favorites_keyword(kw, config):
            max_ps = _favorites_max_per_sync(kw, config)
            if max_ps is not None:
                count = favorites_counts.get(kw, 0)
                if count >= max_ps:
                    logger.info(f"  ⚠ Favorites cap reached for '{kw}', skipping: {classified['title']}")
                    continue
                favorites_counts[kw] = count + 1

        # Download & upload
        result = download_and_upload(
            video_id=vid_id,
            video_url=classified["url"],
            classified=classified,
            api=api,
            config=config,
        )
        if result:
            new_episodes_metadata.append(result)

    # ── Step 3: Collect all episodes from Releases for the feed ──────────
    logger.info("🔄 Rebuilding feed from all GitHub Releases...")
    all_releases = api.list_releases(per_page=100)
    all_episodes: List[Dict] = []

    for rel in sorted(all_releases, key=lambda r: r.get("published_at", ""), reverse=True):
        ep = episode_from_release(rel)
        if ep:
            all_episodes.append(ep)

    max_eps = config.get("max_episodes_in_feed", 50)
    all_episodes = all_episodes[:max_eps]

    logger.info(f"📊 Total episodes for feed: {len(all_episodes)} "
                f"(+{len(new_episodes_metadata)} new this sync)")

    # ── Step 4: Write GitHub Pages output ────────────────────────────────
    build_pages_output(all_episodes, config, pages_url)
    logger.info("🚀 Done. The gh-pages-output/ directory is ready for deployment.")


def _is_favorites_keyword(kw: str, config: Dict) -> bool:
    fav = config.get("favorites", {})
    if not fav.get("enabled"):
        return False
    kw_lower = kw.lower()
    for show in fav.get("shows", []):
        if show.get("keyword", "").lower() == kw_lower:
            return True
    return False


def _favorites_max_per_sync(kw: str, config: Dict) -> Optional[int]:
    kw_lower = kw.lower()
    for show in config.get("favorites", {}).get("shows", []):
        if show.get("keyword", "").lower() == kw_lower:
            return show.get("max_per_sync")  # None means unlimited
    return None


if __name__ == "__main__":
    main()
