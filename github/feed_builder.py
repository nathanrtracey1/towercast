#!/usr/bin/env python3
"""
TowerCast GitHub CI Feed Builder
=================================
Runs inside GitHub Actions to:
  1. Handle incoming triggers:
     - New-video watcher (Cloudflare Worker) / fallback schedule / manual sync
     - Direct queue request (action=queue, video_id=...) — e.g. "Add URL" in the dashboard
     - Favorite rule management (action=add_favorite / delete_favorite)
     - Issue-based triggers from the owner ([Queue]: <video_id>, [Delete]: <ids>, ...)
  2. Scan YouTube channel (both uploads and live streams; Shorts are skipped)
  3. Download new episodes, store audio in Cloudflare R2, record metadata in GitHub Releases
  4. Keep a rolling feed of the newest episodes (manually added ones are protected)
  5. Publish the private feed.xml + dashboard to R2 (served by the Worker behind a password)
  6. Save the list of handled video IDs so the Worker only wakes us for new ones
"""
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from engine.youtube import YouTubeEngine, parse_youtube_url
from engine.downloader import Downloader
from engine.feed_generator import FeedGenerator, format_duration, parse_to_rfc822, parse_date_to_datetime
from github.github_api import GitHubAPI
from github import r2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("TowerCast-CI")

RELEASE_TAG_PREFIX = "ep-"
HISTORY_RELEASE_TAG = "towercast-history"
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


def delete_episode(api: GitHubAPI, video_id: str) -> bool:
    """Deletes an episode's audio in R2 and its metadata release."""
    for ext in ("m4a", "mp3"):
        try:
            r2.delete_key(r2.audio_key(video_id, ext))
        except Exception as e:
            logger.warning(f"Could not delete R2 audio for {video_id}: {e}")
    return api.delete_release_by_tag(tag_for(video_id))


def private_feed_url() -> Optional[str]:
    """Feed URL with credentials embedded, the form Overcast's "Add URL" expects."""
    base, user, pw = (os.environ.get(k) for k in ("FEED_BASE_URL", "FEED_USER", "FEED_PASS"))
    if not (base and user and pw):
        return None
    scheme, rest = base.rstrip("/").split("://", 1)
    return f"{scheme}://{user}:{pw}@{rest}/feed.xml"


def ping_overcast():
    """Asks Overcast to recrawl the private feed now instead of waiting for its schedule."""
    feed = private_feed_url()
    if not feed:
        return
    try:
        urllib.request.urlopen("https://overcast.fm/ping?" + urllib.parse.urlencode({"urlprefix": feed}), timeout=15)
        logger.info("📣 Pinged Overcast to refresh the feed")
    except Exception as e:
        logger.warning(f"Overcast ping failed: {e}")


def load_dismissed_ids(api: GitHubAPI) -> set:
    """Loads previously deleted or cleared video IDs from the towercast-history release."""
    try:
        rel = api.get_release_by_tag(HISTORY_RELEASE_TAG)
        if not rel:
            return set()
        m = re.search(r"<!-- TOWERCAST_HISTORY\n(.*?)\n-->", rel.get("body", ""), re.DOTALL)
        if not m:
            return set()
        data = json.loads(m.group(1))
        return set(data.get("deleted_ids", []))
    except Exception as e:
        logger.warning(f"Could not load dismissed IDs: {e}")
        return set()


def save_dismissed_ids(api: GitHubAPI, dismissed_ids: set):
    """Saves dismissed video IDs into the towercast-history release."""
    try:
        body = f"<!-- TOWERCAST_HISTORY\n{json.dumps({'deleted_ids': sorted(list(dismissed_ids))}, indent=2)}\n-->"
        rel = api.get_release_by_tag(HISTORY_RELEASE_TAG)
        if rel:
            api.update_release(rel["id"], body=body, name="TowerCast Sync History")
        else:
            api.create_release(tag=HISTORY_RELEASE_TAG, name="TowerCast Sync History", body=body, draft=False, prerelease=True)
        logger.info(f"💾 Saved {len(dismissed_ids)} dismissed IDs to history release.")
    except Exception as e:
        logger.error(f"Failed to save dismissed IDs: {e}")


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
        "audio_ext": meta.get("audio_ext", "m4a"),
        "is_manual_request": meta.get("is_manual_request", False),
        "added_at": meta.get("added_at") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
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
    """Returns {video_id: release_json} for all TowerCast episode releases."""
    releases = api.list_all_releases()
    result = {}
    for rel in releases:
        vid_id = video_id_from_tag(rel.get("tag_name", ""))
        if vid_id:
            result[vid_id] = rel
    return result


def episode_from_release(release: Dict) -> Optional[Dict[str, Any]]:
    """Reconstructs an episode dict from a GitHub Release for feed generation."""
    meta = parse_release_body(release.get("body", ""))
    if not meta:
        return None

    vid = meta.get("id") or video_id_from_tag(release.get("tag_name", ""))
    ext = meta.get("audio_ext")
    file_size = 0
    for asset in release.get("assets", []):  # legacy episodes created before R2
        name = asset.get("name", "")
        if name.endswith((".m4a", ".mp3", ".aac", ".opus")):
            ext = ext or name.rsplit(".", 1)[1]
            file_size = asset.get("size", 0)
            break
    ext = ext or "m4a"

    is_manual = meta.get("is_manual_request") or (meta.get("matched_keyword") in ("Manual Cloud Queue", "Manual Request", "Added via URL"))
    # For manually requested older episodes, use the added_at date for the podcast feed
    # so it pops up at the very top of Overcast rather than getting lost years in the past.
    feed_published_at = meta.get("added_at") if (is_manual and meta.get("added_at")) else (meta.get("published_at") or release.get("published_at"))

    return {
        "id": vid,
        "title": meta.get("title") or release.get("name"),
        "audio_url": r2.audio_url(vid, ext),
        "audio_filename": f"{vid}/{vid}.{ext}",
        "file_size": meta.get("file_size") or file_size,
        "duration": meta.get("duration"),
        "published_at": feed_published_at,
        "original_published_at": meta.get("published_at"),
        "thumbnail_url": meta.get("thumbnail_url"),
        "description": meta.get("description", ""),
        "chapters_json": json.dumps(meta.get("chapters", [])),
        "matched_keyword": meta.get("matched_keyword"),
        "is_manual_request": is_manual,
    }


def download_and_upload(video_id: str, video_url: str, classified: Dict,
                        api: GitHubAPI, config: Dict) -> Optional[Dict[str, Any]]:
    """Downloads audio, stores it in R2, records a metadata release, returns episode metadata."""
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
        for key in ("is_manual_request", "added_at"):
            if classified.get(key):
                result[key] = classified[key]

        # Audio goes to R2 first, so any release that exists always has its audio
        ext = os.path.splitext(audio_path)[1].lstrip(".") or "m4a"
        content_type = "audio/mp4" if ext == "m4a" else "audio/mpeg"
        logger.info(f"⬆  Uploading {video_id}.{ext} to R2 ({result['file_size'] // (1024*1024)} MB)...")
        try:
            r2.upload_file(audio_path, r2.audio_key(video_id, ext), content_type)
        except Exception as e:
            logger.error(f"R2 upload failed for {video_id}: {e}")
            return None

        result["audio_ext"] = ext
        result["audio_url"] = r2.audio_url(video_id, ext)

        tag = tag_for(video_id)
        release_name = result.get("title") or title
        logger.info(f"📦 Creating metadata release: {tag}")
        try:
            api.create_release(tag=tag, name=release_name, body=build_release_body(result))
        except Exception as e:
            logger.error(f"Failed to create release for {video_id}: {e}")
            return None

        logger.info(f"✅ {release_name} uploaded successfully")
        return result


def build_pages_output(episodes: List[Dict], pending_episodes: List[Dict], config: Dict, feed_base_url: str) -> Path:
    """Generates feed.xml and the dashboard, and publishes both to R2 behind the Worker."""
    out = ROOT / "gh-pages-output"
    out.mkdir(exist_ok=True)

    gh_config = dict(config)
    gh_config["public_base_url"] = feed_base_url.rstrip("/")

    fg = FeedGenerator(gh_config)
    xml = fg.generate_feed_xml_with_full_urls(episodes)

    feed_path = out / "feed.xml"
    feed_path.write_text(xml, encoding="utf-8")
    r2.upload_bytes(xml.encode("utf-8"), "feed.xml", "application/rss+xml; charset=utf-8", cache_control="no-cache")
    logger.info(f"📄 Published feed.xml ({len(xml)} bytes, {len(episodes)} episodes)")

    # Render dashboard template (served at the Worker root, behind the same password)
    try:
        from bottle import template, TEMPLATE_PATH
        TEMPLATE_PATH.insert(0, str(ROOT / "server" / "templates"))

        overcast_url = private_feed_url() or f"{feed_base_url.rstrip('/')}/feed.xml"
        index_html = template(
            "index.html",
            pending_episodes=pending_episodes,
            ready_episodes=episodes,
            queued_episodes=[],
            feed_url=overcast_url,
            gh_feed_url=overcast_url,
            gh_pages_url=feed_base_url,
            lan_url="",
            remote_url="",
            auto_keywords=config.get("auto_include_keywords", []),
            favorites_shows=config.get("favorites", {}).get("shows", []),
            format_duration=format_duration
        )
        (out / "index.html").write_text(index_html, encoding="utf-8")
        r2.upload_bytes(index_html.encode("utf-8"), "index.html", "text/html; charset=utf-8", cache_control="no-cache")
        logger.info("🌐 Published dashboard index.html")
    except Exception as e:
        logger.error(f"Failed to render dashboard template: {e}")

    return out


def save_known_ids(known: set):
    """Tells the Worker which videos are handled, so it only starts a run for new ones."""
    payload = {"ids": sorted(known), "updated_at": datetime.now(timezone.utc).isoformat()}
    r2.upload_bytes(json.dumps(payload).encode("utf-8"), "state/known_ids.json", "application/json")
    logger.info(f"🧭 Saved {len(known)} known video IDs for the watcher.")


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

        # Check for [Queue]: <video_id_or_url> or [AddUrl]: <url>
        m_q = re.search(r"\[(?:Queue|AddUrl)\]:\s*(\S+)", title, re.IGNORECASE)
        if m_q:
            raw_target = m_q.group(1).strip()
            vid = parse_youtube_url(raw_target) or raw_target
            return "queue", vid, issue_num

        # Check for [ClearFeed] or [DeleteAll]
        if re.search(r"\[(?:ClearFeed|DeleteAll|Clear)\]", title, re.IGNORECASE):
            return "clear_feed", "all", issue_num

        # Check for [Delete]: <video_id> or [DeleteBatch]: <ids>
        m_d = re.search(r"\[(?:Delete|DeleteBatch)\]:\s*([^\n\r]+)", title, re.IGNORECASE)
        if m_d:
            raw_target = m_d.group(1).strip()
            if raw_target.lower() in ("all", "clear"):
                return "clear_feed", "all", issue_num
            return "delete_batch", raw_target, issue_num

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
    if not r2.is_configured():
        logger.error(f"R2 storage is not configured. Set these repo secrets: {', '.join(r2.REQUIRED_ENV)}")
        sys.exit(1)

    scan_limit = int(os.environ.get("SCAN_LIMIT", "30"))
    action = os.environ.get("ACTION", "sync").lower().strip()
    target_video_id = os.environ.get("VIDEO_ID", "").strip()
    keyword = os.environ.get("KEYWORD", "").strip()

    config = load_config()
    feed_base_url = os.environ["FEED_BASE_URL"].rstrip("/")

    api = GitHubAPI(token=token, repo=gh_repo)

    # Check if triggered by an issue (the workflow only lets the repo owner's issues through)
    issue_action, issue_arg, issue_num = handle_issue_event(api, config)
    if issue_action:
        action = issue_action
        if action in ("queue", "add_url", "delete", "delete_episode", "delete_batch", "clear_feed"):
            target_video_id = issue_arg
        elif action in ("add_favorite", "favorite", "delete_favorite"):
            keyword = issue_arg

    dismissed_ids = load_dismissed_ids(api)
    logger.info(f"Loaded {len(dismissed_ids)} dismissed video IDs from history.")

    # Handle clearing the entire feed
    if action in ("clear_feed", "delete_all") or (action in ("delete", "delete_episode", "delete_batch") and target_video_id.lower() in ("all", "clear")):
        logger.info("🗑 Clearing all episodes from podcast feed...")
        all_releases = api.list_all_releases()
        cleared_count = 0
        for rel in all_releases:
            vid_id = video_id_from_tag(rel.get("tag_name", ""))
            if vid_id:
                try:
                    delete_episode(api, vid_id)
                    dismissed_ids.add(vid_id)
                    cleared_count += 1
                except Exception as e:
                    logger.warning(f"Could not delete episode {vid_id}: {e}")
        save_dismissed_ids(api, dismissed_ids)
        logger.info(f"✅ Cleared {cleared_count} episodes and recorded to history.")
        if issue_num:
            try:
                api.comment_issue(issue_num, f"Cleared all {cleared_count} episodes from TowerCast feed.")
                api.close_issue(issue_num)
            except Exception as e:
                logger.warning(f"Could not close issue #{issue_num}: {e}")

    # Handle episode deletion (single or batch)
    elif action in ("delete", "delete_episode", "delete_batch") and target_video_id:
        raw_ids = [x.strip() for x in re.split(r"[\s,]+", target_video_id) if x.strip()]
        deleted_count = 0
        for raw_id in raw_ids:
            vid = parse_youtube_url(raw_id) or raw_id
            logger.info(f"🗑 Deleting episode: {vid}")
            try:
                delete_episode(api, vid)
                dismissed_ids.add(vid)
                deleted_count += 1
                logger.info(f"✅ Successfully deleted {vid}")
            except Exception as e:
                logger.warning(f"Could not delete episode {vid}: {e}")
        save_dismissed_ids(api, dismissed_ids)
        logger.info(f"💾 Added {deleted_count} video ID(s) to dismissed history.")
        if issue_num:
            try:
                api.comment_issue(issue_num, f"Deleted {deleted_count} episode(s) from TowerCast.")
                api.close_issue(issue_num)
            except Exception as e:
                logger.warning(f"Could not close issue #{issue_num}: {e}")

    # Handle single video queueing (dashboard "Add URL", issues, manual dispatch)
    if action == "queue" and target_video_id:
        target_video_id = parse_youtube_url(target_video_id) or target_video_id
        if target_video_id in dismissed_ids:
            dismissed_ids.remove(target_video_id)
            save_dismissed_ids(api, dismissed_ids)
            logger.info(f"Un-dismissed {target_video_id} for queueing.")
        logger.info(f"⚡ Processing direct queue for video: {target_video_id}")
        video_url = f"https://www.youtube.com/watch?v={target_video_id}"
        classified = {
            "id": target_video_id,
            "title": f"Episode {target_video_id}",
            "url": video_url,
            "thumbnail_url": f"https://i.ytimg.com/vi/{target_video_id}/hq720.jpg",
            "matched_keyword": "Manual Request",
            "is_manual_request": True,
            "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        }
        res = download_and_upload(target_video_id, video_url, classified, api, config)
        if issue_num:
            try:
                if res:
                    api.comment_issue(issue_num, f"Successfully downloaded and added to feed!")
                    api.close_issue(issue_num)
                else:
                    api.comment_issue(issue_num, f"Download failed (YouTube may be blocking the cloud runner). Check the workflow log, or queue it again later.")
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
        auto_download_all_new=config.get("auto_download_all_new", True),
    )

    logger.info(f"📡 Scanning uploads & live streams (limit={scan_limit})...")
    pending_entries = []
    skipped_ids = set()
    try:
        _, entries = yt.fetch_channel_entries(limit=scan_limit)
        logger.info(f"Found {len(entries)} total entries across uploads and live streams.")

        # All entries within current scan limit are eligible for auto-download
        new_batch_threshold = scan_limit

        for idx, entry in enumerate(entries):
            is_backlog = not entry.get("is_recent", True) if "is_recent" in entry else (idx >= new_batch_threshold)
            classified = yt.classify_entry(entry, is_backlog=is_backlog)
            vid_id = classified["id"]

            if classified.get("is_scheduled_or_live") or vid_id in existing:
                continue

            if classified["is_short"]:
                skipped_ids.add(vid_id)
                continue

            if vid_id in dismissed_ids:
                # Previously deleted or cleared; do not auto-queue into feed
                pending_entries.append(classified)
                continue

            if classified["target_status"] == "queued":
                # Auto-download new episodes or matched shows
                result = download_and_upload(
                    video_id=vid_id,
                    video_url=classified["url"],
                    classified=classified,
                    api=api,
                    config=config,
                )
                if result:
                    existing[vid_id] = result
            else:
                # Add to Backlog list for on-demand downloading
                pending_entries.append(classified)

        # Sort pending entries chronologically (newest first)
        pending_entries.sort(
            key=lambda ep: parse_date_to_datetime(ep.get("published_at")),
            reverse=True
        )

    except Exception as e:
        logger.error(f"Channel scan failed: {e}")

    # ── Step 3: Collect all episodes from Releases for the feed ──────────
    logger.info("🔄 Rebuilding feed from all GitHub Releases...")
    all_releases = api.list_all_releases()
    all_episodes: List[Dict] = []

    for rel in all_releases:
        ep = episode_from_release(rel)
        if ep:
            all_episodes.append(ep)

    # Sort strictly chronologically by original YouTube published_at (newest first)
    all_episodes.sort(
        key=lambda ep: parse_date_to_datetime(ep.get("published_at")),
        reverse=True
    )

    max_eps = config.get("max_episodes_in_feed", 50)
    keep_episodes = all_episodes[:max_eps]
    prune_episodes = all_episodes[max_eps:]

    # Automatic rolling retention: delete episodes older than retention limit
    if prune_episodes and config.get("auto_prune_old_episodes", True):
        logger.info(f"🧹 Auto-pruning {len(prune_episodes)} old episode(s) beyond feed retention limit ({max_eps})...")
        for ep in prune_episodes:
            if ep.get("is_manual_request"):
                # User specifically requested this older episode; protect it in the feed
                keep_episodes.append(ep)
                continue
            vid = ep["id"]
            try:
                delete_episode(api, vid)
                dismissed_ids.add(vid)
                logger.info(f"Deleted old episode: {vid}")
            except Exception as e:
                logger.warning(f"Could not prune episode {vid}: {e}")
        save_dismissed_ids(api, dismissed_ids)

    all_episodes = keep_episodes

    logger.info(f"📊 Total episodes in podcast feed: {len(all_episodes)} | Pending review: {len(pending_entries)}")

    # ── Step 4: Publish feed + dashboard ─────────────────────────────────
    build_pages_output(all_episodes, pending_entries, config, feed_base_url)
    ping_overcast()

    # ── Step 5: Tell the watcher what's been handled ─────────────────────
    # Upcoming live streams and failed downloads stay out of this list, so the
    # Worker keeps retrying them (with backoff) until they make it into the feed.
    known = set(existing) | dismissed_ids | skipped_ids | {e["id"] for e in pending_entries}
    save_known_ids(known)
    logger.info("🚀 Done.")


if __name__ == "__main__":
    main()
