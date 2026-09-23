#!/usr/bin/env python3
"""
TowerCast GitHub CI Feed Builder
=================================
Runs inside GitHub Actions to:
  1. Handle incoming triggers:
     - Scheduled scan / manual sync
     - Direct queue request (action=queue, video_id=...)
     - Favorite rule management (action=add_favorite / delete_favorite)
     - Issue-based triggers from mobile GitHub Pages ([Queue]: <video_id>, [Favorite]: <keyword>)
  2. Scan YouTube channel (both uploads and live streams)
  3. Auto-download matching episodes and upload audio to GitHub Releases
  4. Collect pending review episodes and active show rules
  5. Deploy complete interactive Apple HIG dashboard & feed.xml to GitHub Pages
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

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
CONFIG_PATH = ROOT / "config.json"


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: Dict[str, Any]):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


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
        "description": (meta.get("description") or "")[:4000],
        "chapters": meta.get("chapters", []),
        "matched_keyword": meta.get("matched_keyword"),
        "file_size": meta.get("file_size", 0),
    }
    return f"<!-- TOWERCAST_META\n{json.dumps(payload, indent=2)}\n-->"


def parse_release_body(body: str) -> Optional[Dict[str, Any]]:
    """Extracts TowerCast metadata from a release body comment."""
    m = re.search(r"<!-- TOWERCAST_META\n(.*?)\n-->", body or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def get_existing_episode_ids(api: GitHubAPI) -> Dict[str, Dict]:
    """Returns {video_id: release_json} for all TowerCast releases with an audio asset."""
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
        "audio_url": audio_url,
        "audio_filename": f"{meta.get('id')}/{meta.get('id')}.m4a",
        "file_size": meta.get("file_size") or file_size,
        "duration": meta.get("duration"),
        "published_at": meta.get("published_at") or release.get("published_at"),
        "thumbnail_url": meta.get("thumbnail_url"),
        "description": meta.get("description", ""),
        "chapters_json": json.dumps(meta.get("chapters", [])),
        "matched_keyword": meta.get("matched_keyword"),
    }


def download_and_upload(video_id: str, video_url: str, classified: Dict,
                        api: GitHubAPI, config: Dict) -> Optional[Dict[str, Any]]:
    """Downloads audio, uploads to a new GitHub Release, returns episode metadata."""
    cookies_path = None
    cookies_content = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_content:
        cookies_path = "/tmp/yt_cookies.txt"
        try:
            with open(cookies_path, "w", encoding="utf-8") as f:
                f.write(cookies_content)
            logger.info("🍪 Loaded YouTube cookies from YOUTUBE_COOKIES")
        except Exception as e:
            logger.warning(f"Could not write cookies file: {e}")
            cookies_path = None

    with tempfile.TemporaryDirectory() as tmpdir:
        dl = Downloader(
            media_dir=tmpdir,
            audio_format=config.get("audio_format", "m4a"),
            audio_quality=config.get("audio_quality", "192k"),
            cookies_file=cookies_path,
        )

        title = classified.get("title") or f"Episode {video_id}"
        logger.info(f"⬇  Downloading: {title}")
        try:
            result = dl.download_episode(video_id, video_url)
        except Exception as e:
            logger.error(f"Download failed for {video_id}: {e}")
            return None

        audio_path = result.get("audio_full_path")
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"No audio file after download for {video_id}")
            return None

        result["thumbnail_url"] = classified.get("thumbnail_url")
        result["matched_keyword"] = classified.get("matched_keyword")

        tag = tag_for(video_id)
        release_name = result.get("title") or title
        release_body = build_release_body(result)

        logger.info(f"📦 Creating GitHub Release: {tag}")
        try:
            release = api.create_release(tag=tag, name=release_name, body=release_body)
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
        result["file_size"] = asset.get("size", result["file_size"])

        logger.info(f"✅ {release_name} uploaded successfully")
        return result


def build_pages_output(episodes: List[Dict], pending_episodes: List[Dict], config: Dict, pages_url: str) -> Path:
    """Generates gh-pages-output/ with feed.xml and full interactive dashboard index.html."""
    out = ROOT / "gh-pages-output"
    out.mkdir(exist_ok=True)

    gh_config = dict(config)
    gh_config["public_base_url"] = pages_url.rstrip("/")

    fg = FeedGenerator(gh_config)
    xml = fg.generate_feed_xml_with_full_urls(episodes)

    feed_path = out / "feed.xml"
    feed_path.write_text(xml, encoding="utf-8")
    logger.info(f"📄 Wrote feed.xml ({len(xml)} bytes, {len(episodes)} episodes)")

    # Render dashboard template for GitHub Pages
    try:
        from bottle import template, TEMPLATE_PATH
        TEMPLATE_PATH.insert(0, str(ROOT / "server" / "templates"))

        index_html = template(
            "index.html",
            pending_episodes=pending_episodes,
            ready_episodes=episodes,
            queued_episodes=[],
            feed_url=f"{pages_url.rstrip('/')}/feed.xml",
            gh_feed_url=f"{pages_url.rstrip('/')}/feed.xml",
            gh_pages_url=pages_url,
            lan_url="",
            remote_url="",
            auto_keywords=config.get("auto_include_keywords", []),
            favorites_shows=config.get("favorites", {}).get("shows", []),
            format_duration=format_duration
        )
        (out / "index.html").write_text(index_html, encoding="utf-8")
        logger.info("🌐 Wrote interactive dashboard index.html for GitHub Pages")
    except Exception as e:
        logger.error(f"Failed to render dashboard template: {e}")

    return out


def handle_issue_event(api: GitHubAPI, config: Dict) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Checks GITHUB_EVENT_PATH if workflow was triggered by an issue."""
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path or not os.path.exists(event_path):
        return None, None, None

    try:
        with open(event_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        issue = data.get("issue")
        if not issue:
            return None, None, None

        title = issue.get("title", "").strip()
        issue_num = issue.get("number")
        logger.info(f"Processing Issue #{issue_num}: {title}")

        # Check for [Queue]: <video_id>
        m_q = re.search(r"\[Queue\]:\s*([a-zA-Z0-9_-]+)", title, re.IGNORECASE)
        if m_q:
            vid = m_q.group(1)
            return "queue", vid, issue_num

        # Check for [Delete]: <video_id>
        m_d = re.search(r"\[Delete\]:\s*([a-zA-Z0-9_-]+)", title, re.IGNORECASE)
        if m_d:
            vid = m_d.group(1)
            return "delete", vid, issue_num

        # Check for [Favorite]: <keyword>
        m_f = re.search(r"\[Favorite\]:\s*(.+)", title, re.IGNORECASE)
        if m_f:
            kw = m_f.group(1).strip()
            return "add_favorite", kw, issue_num

        # Check for [DeleteFavorite]: <keyword>
        m_df = re.search(r"\[DeleteFavorite\]:\s*(.+)", title, re.IGNORECASE)
        if m_df:
            kw = m_df.group(1).strip()
            return "delete_favorite", kw, issue_num

    except Exception as e:
        logger.warning(f"Error parsing issue event: {e}")

    return None, None, None


def main():
    token = os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not gh_repo:
        logger.error("GITHUB_TOKEN and GITHUB_REPOSITORY must be set.")
        sys.exit(1)

    scan_limit = int(os.environ.get("SCAN_LIMIT", "30"))
    action = os.environ.get("ACTION", "sync").lower().strip()
    target_video_id = os.environ.get("VIDEO_ID", "").strip()
    keyword = os.environ.get("KEYWORD", "").strip()

    config = load_config()
    gh_cfg = config.get("github", {})
    pages_url = gh_cfg.get("pages_url", "").rstrip("/")
    if not pages_url:
        pages_url = f"https://{gh_repo.split('/')[0]}.github.io/{gh_repo.split('/')[1]}"

    api = GitHubAPI(token=token, repo=gh_repo)

    # Check if triggered by an issue (from mobile GitHub Pages)
    issue_action, issue_arg, issue_num = handle_issue_event(api, config)
    if issue_action:
        action = issue_action
        if action in ("queue", "delete", "delete_episode"):
            target_video_id = issue_arg
        elif action in ("add_favorite", "favorite", "delete_favorite"):
            keyword = issue_arg

    # Handle episode deletion
    if action in ("delete", "delete_episode") and target_video_id:
        tag = tag_for(target_video_id)
        logger.info(f"🗑 Deleting episode release: {tag}")
        try:
            api.delete_release_by_tag(tag)
            logger.info(f"✅ Successfully deleted release {tag}")
        except Exception as e:
            logger.warning(f"Could not delete release {tag}: {e}")
        if issue_num:
            try:
                api.comment_issue(issue_num, f"Deleted episode `{target_video_id}` from TowerCast.")
                api.close_issue(issue_num)
            except Exception as e:
                logger.warning(f"Could not close issue #{issue_num}: {e}")

    # Handle favorite additions
    if action in ("add_favorite", "favorite") and keyword:
        logger.info(f"⭐ Adding favorite keyword: {keyword}")
        fav = config.setdefault("favorites", {"enabled": True, "shows": []})
        shows = fav.setdefault("shows", [])
        if keyword.lower() not in [s.get("keyword", "").lower() for s in shows]:
            shows.append({"keyword": keyword})
            save_config(config)
            # Commit config.json change
            try:
                subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
                subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
                subprocess.run(["git", "add", "config.json"], check=False)
                subprocess.run(["git", "commit", "-m", f"chore: add favorite {keyword}"], check=False)
                subprocess.run(["git", "push"], check=False)
            except Exception as e:
                logger.warning(f"Could not commit favorite: {e}")
        if issue_num:
            try:
                api.comment_issue(issue_num, f"Added favorite rule: `{keyword}`.")
                api.close_issue(issue_num)
            except Exception as e:
                pass

    # Handle favorite removals
    if action in ("delete_favorite",) and keyword:
        logger.info(f"🗑 Removing favorite keyword: {keyword}")
        fav = config.setdefault("favorites", {"enabled": True, "shows": []})
        shows = fav.setdefault("shows", [])
        fav["shows"] = [s for s in shows if s.get("keyword", "").lower() != keyword.lower()]
        auto_kws = config.get("auto_include_keywords", [])
        config["auto_include_keywords"] = [k for k in auto_kws if k.lower() != keyword.lower()]
        save_config(config)
        try:
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], check=False)
            subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=False)
            subprocess.run(["git", "add", "config.json"], check=False)
            subprocess.run(["git", "commit", "-m", f"chore: delete favorite {keyword}"], check=False)
            subprocess.run(["git", "push"], check=False)
        except Exception as e:
            logger.warning(f"Could not commit favorite removal: {e}")
        if issue_num:
            try:
                api.comment_issue(issue_num, f"Removed favorite rule: `{keyword}`.")
                api.close_issue(issue_num)
            except Exception as e:
                pass

    # Handle single video queueing
    if action == "queue" and target_video_id:
        logger.info(f"⚡ Processing direct queue for video: {target_video_id}")
        video_url = f"https://www.youtube.com/watch?v={target_video_id}"
        classified = {
            "id": target_video_id,
            "title": f"Episode {target_video_id}",
            "url": video_url,
            "thumbnail_url": f"https://i.ytimg.com/vi/{target_video_id}/hq720.jpg",
            "matched_keyword": "Manual Cloud Queue"
        }
        res = download_and_upload(target_video_id, video_url, classified, api, config)
        if issue_num:
            try:
                status_txt = "Successfully downloaded and added to feed!" if res else "Download failed (may require cookies in CI)."
                api.comment_issue(issue_num, f"Queue result for `{target_video_id}`: {status_txt}")
                api.close_issue(issue_num)
            except Exception as e:
                pass

    # ── Step 1: Existing releases ─────────────────────────────────────────
    logger.info("📚 Checking existing GitHub Releases...")
    existing = get_existing_episode_ids(api)
    logger.info(f"Found {len(existing)} already-uploaded episodes in Releases.")

    # ── Step 2: Channel Discovery ─────────────────────────────────────────
    yt = YouTubeEngine(
        channel_url=config["channel_url"],
        auto_keywords=config.get("auto_include_keywords", []),
        exclude_shorts=config.get("exclude_shorts", True),
        shorts_max_seconds=config.get("shorts_max_seconds", 60),
        favorites_config=config.get("favorites", {}),
    )

    logger.info(f"📡 Scanning uploads & live streams (limit={scan_limit})...")
    pending_entries = []
    try:
        _, entries = yt.fetch_channel_entries(limit=scan_limit)
        logger.info(f"Found {len(entries)} total entries across uploads and live streams.")

        favorites_counts: Dict[str, int] = {}
        for entry in entries:
            classified = yt.classify_entry(entry)
            vid_id = classified["id"]

            if classified["is_short"] or vid_id in existing:
                continue

            if classified["target_status"] == "queued":
                # Auto-download
                result = download_and_upload(
                    video_id=vid_id,
                    video_url=classified["url"],
                    classified=classified,
                    api=api,
                    config=config,
                )
            else:
                # Add to pending list for review
                pending_entries.append(classified)

    except Exception as e:
        logger.error(f"Channel scan failed: {e}")

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

    logger.info(f"📊 Total episodes in podcast feed: {len(all_episodes)} | Pending review: {len(pending_entries)}")

    # ── Step 4: Write GitHub Pages output ────────────────────────────────
    build_pages_output(all_episodes, pending_entries, config, pages_url)
    logger.info("🚀 Done. gh-pages-output/ ready for deployment.")


if __name__ == "__main__":
    main()
