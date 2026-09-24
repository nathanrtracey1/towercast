#!/usr/bin/env python3
"""
TowerCast - Local Podcast Feed Generator for The Dice Tower
Turns any YouTube channel into a private, iTunes/Overcast-compatible podcast feed.
"""
import argparse
import json
import logging
import os
import sys
import time
import threading
import subprocess
from typing import Dict, Any

from engine.database import Database
from engine.youtube import YouTubeEngine
from engine.downloader import Downloader
from engine.feed_generator import FeedGenerator, format_duration
from server.web import WebServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("TowerCast")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "towercast.db")
MEDIA_DIR = os.path.join(DATA_DIR, "audio")

def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def get_components(config: Dict[str, Any]):
    db = Database(DB_PATH)
    yt = YouTubeEngine(
        channel_url=config["channel_url"],
        auto_keywords=config.get("auto_include_keywords", []),
        exclude_shorts=config.get("exclude_shorts", True),
        shorts_max_seconds=config.get("shorts_max_seconds", 60),
        favorites_config=config.get("favorites", {}),
        auto_download_all_new=config.get("auto_download_all_new", True),
    )
    dl = Downloader(
        media_dir=MEDIA_DIR,
        audio_format=config.get("audio_format", "m4a"),
        audio_quality=config.get("audio_quality", "192k")
    )
    server = WebServer(config, db, dl, yt, MEDIA_DIR)
    return db, yt, dl, server

def process_github_triggers(config: Dict[str, Any], db: Database):
    """Checks open GitHub Issues for remote queue, add-from-URL, or delete requests submitted from mobile."""
    gh_cfg = config.get("github", {})
    if not gh_cfg.get("enabled"):
        return
    repo = gh_cfg.get("repo")
    if not repo:
        return

    try:
        res = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open", "--json", "number,title,body"],
            capture_output=True, text=True, check=True
        )
        issues = json.loads(res.stdout or "[]")
        if not issues:
            return

        from engine.youtube import parse_youtube_url

        for issue in issues:
            num = issue.get("number")
            title = (issue.get("title") or "").strip()

            # Case 1: [Queue]: <video_id>
            if title.startswith("[Queue]:"):
                vid_id = title[len("[Queue]:"):].strip()
                if vid_id and vid_id != "all":
                    print(f"📥 Remote queue request from GitHub Issue #{num}: {vid_id}")
                    db.upsert_discovered_episode(
                        video_id=vid_id,
                        title=f"Episode {vid_id}",
                        url=f"https://www.youtube.com/watch?v={vid_id}",
                        published_at=None,
                        duration=None,
                        thumbnail_url=f"https://i.ytimg.com/vi/{vid_id}/hq720.jpg",
                        status="queued",
                        matched_keyword="Mobile Queue"
                    )
                    subprocess.run(
                        ["gh", "issue", "close", str(num), "--comment", f"Queued for download on Mac: `{vid_id}`", "--repo", repo],
                        check=False
                    )

            # Case 2: [AddUrl]: <url or id> or [QueueUrl]: <url>
            elif title.startswith("[AddUrl]:") or title.startswith("[QueueUrl]:"):
                prefix = "[AddUrl]:" if title.startswith("[AddUrl]:") else "[QueueUrl]:"
                raw_val = title[len(prefix):].strip()
                vid_id = parse_youtube_url(raw_val) or raw_val
                if vid_id:
                    print(f"📥 Remote Add-from-URL request from GitHub Issue #{num}: {vid_id}")
                    db.upsert_discovered_episode(
                        video_id=vid_id,
                        title=f"Episode {vid_id}",
                        url=f"https://www.youtube.com/watch?v={vid_id}",
                        published_at=None,
                        duration=None,
                        thumbnail_url=f"https://i.ytimg.com/vi/{vid_id}/hq720.jpg",
                        status="queued",
                        matched_keyword="Added via URL"
                    )
                    subprocess.run(
                        ["gh", "issue", "close", str(num), "--comment", f"Added to download queue on Mac: `{vid_id}`", "--repo", repo],
                        check=False
                    )

            # Case 3: [Delete]: <video_id>
            elif title.startswith("[Delete]:"):
                vid_id = title[len("[Delete]:"):].strip()
                if vid_id:
                    print(f"🗑 Remote delete request from GitHub Issue #{num}: {vid_id}")
                    db.skip_episode(vid_id, allow_ready=True)
                    ep_dir = os.path.join(MEDIA_DIR, vid_id)
                    if os.path.exists(ep_dir):
                        import shutil
                        try:
                            shutil.rmtree(ep_dir)
                        except Exception:
                            pass
                    subprocess.run(["gh", "release", "delete", f"ep-{vid_id}", "--yes", "--repo", repo], check=False)
                    subprocess.run(
                        ["gh", "issue", "close", str(num), "--comment", f"Episode `{vid_id}` removed from feed and Releases.", "--repo", repo],
                        check=False
                    )
    except Exception as e:
        logger.warning(f"Could not check GitHub issues: {e}")


def cmd_sync(args, config):
    db, yt, dl, _ = get_components(config)

    # First, process any pending remote requests from GitHub Issues
    process_github_triggers(config, db)

    limit = 150 if getattr(args, "backlog", False) else (args.limit or 30)
    print(f"\n📡 Scanning channel sources (uploads + live streams, limit={limit} each)...")

    try:
        channel_info, entries = yt.fetch_channel_entries(limit=limit)
    except Exception as e:
        print(f"❌ Error scanning YouTube: {e}")
        return

    print(f"📺 Channel: {channel_info['channel']} - Found {len(entries)} entries.\n")

    new_auto_queued = 0
    new_pending = 0
    shorts_skipped = 0

    new_batch_threshold = 15
    for idx, entry in enumerate(entries):
        is_backlog = (idx >= new_batch_threshold) if getattr(args, "backlog", False) or limit > 30 else False
        classified = yt.classify_entry(entry, is_backlog=is_backlog)
        vid_id = classified["id"]
        title = classified["title"]

        if classified["is_short"]:
            shorts_skipped += 1
            db.upsert_discovered_episode(
                video_id=vid_id,
                title=title,
                url=classified["url"],
                published_at=None,
                duration=classified["duration"],
                thumbnail_url=classified["thumbnail_url"],
                status="skipped"
            )
            continue

        inserted = db.upsert_discovered_episode(
            video_id=vid_id,
            title=title,
            url=classified["url"],
            published_at=None,
            duration=classified["duration"],
            thumbnail_url=classified["thumbnail_url"],
            status=classified["target_status"],
            matched_keyword=classified["matched_keyword"]
        )

        if inserted:
            if classified["target_status"] == "queued":
                new_auto_queued += 1
                print(f"  ⚡ Auto-queued: {title} (matched: '{classified['matched_keyword']}')")
            else:
                new_pending += 1
                print(f"  ⏳ Held for review: {title}")

    print(f"\n📊 Summary: {new_auto_queued} auto-queued, {new_pending} pending review, {shorts_skipped} shorts excluded.")

    # Process all queued
    queued = db.get_queued_episodes()
    if queued:
        print(f"\n⬇️ Downloading {len(queued)} queued episode(s)...")
        for ep in queued:
            vid_id = ep["id"]
            print(f"   Downloading: {ep['title']}...")
            try:
                result = dl.download_episode(vid_id, ep["url"])
                db.mark_status(
                    vid_id,
                    "ready",
                    audio_filename=result["audio_filename"],
                    file_size=result["file_size"],
                    duration=result["duration"] or ep.get("duration"),
                    description=result["description"],
                    published_at=result["published_at"],
                    chapters=result.get("chapters", []),
                    title=result.get("title") or ep["title"]
                )
                print(f"   ✅ Ready: {ep['title']} ({result['file_size'] // (1024*1024)} MB)")
            except Exception as e:
                print(f"   ❌ Failed: {e}")
                db.mark_status(vid_id, "failed")

    # Generate feed preview
    ready = db.get_ready_episodes()
    print(f"\n🎉 Sync complete! Total active podcast episodes in feed: {len(ready)}")

    # Automatically publish to GitHub Releases and update GitHub Pages feed if enabled
    if config.get("github", {}).get("enabled"):
        cmd_publish(args, config)

def cmd_pick(args, config):
    """Interactive terminal picker to select pending videos to grab."""
    db, yt, dl, _ = get_components(config)
    pending = db.get_pending_episodes(limit=args.limit or 50)

    if not pending:
        print("\nChecking YouTube for any new videos first...")
        cmd_sync(argparse.Namespace(limit=25), config)
        pending = db.get_pending_episodes(limit=50)

    if not pending:
        print("\n✨ All caught up! No pending videos awaiting your choice.")
        return

    print("\n" + "="*70)
    print("🎲 TOWERCAST - PICK & CHOOSE EPISODES")
    print("="*70)
    print("The following videos were not auto-downloaded (Shorts excluded):")
    print("-" * 70)

    for idx, ep in enumerate(pending, 1):
        dur = format_duration(ep.get("duration"))
        print(f" [{idx:2d}] {ep['title']}")
        print(f"      Duration: {dur}  |  ID: {ep['id']}")

    print("-" * 70)
    print("Options:")
    print("  - Enter numbers separated by commas or spaces to download (e.g., 1, 3, 4)")
    print("  - 'all' to download everything")
    print("  - 'skip 1, 2' to dismiss specific videos")
    print("  - 'q' to quit")
    print("-" * 70)

    choice = input("Your selection > ").strip()
    if not choice or choice.lower() == 'q':
        print("Exited.")
        return

    if choice.lower() == 'all':
        for ep in pending:
            db.queue_episode(ep["id"])
        print(f"Queued all {len(pending)} episodes for download.")
    elif choice.lower().startswith('skip'):
        parts = choice[4:].replace(',', ' ').split()
        for p in parts:
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(pending):
                    db.skip_episode(pending[idx]["id"])
                    print(f"Skipped: {pending[idx]['title']}")
        return
    else:
        parts = choice.replace(',', ' ').split()
        selected_ids = []
        for p in parts:
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < len(pending):
                    ep = pending[idx]
                    db.queue_episode(ep["id"])
                    selected_ids.append(ep)
                    print(f"➕ Queued: {ep['title']}")

        if not selected_ids:
            print("No valid episodes selected.")
            return

    # Download queued episodes immediately
    queued = db.get_queued_episodes()
    print(f"\n⬇️ Downloading {len(queued)} selected episode(s)...")
    for ep in queued:
        print(f"Downloading: {ep['title']}...")
        try:
            result = dl.download_episode(ep["id"], ep["url"])
            db.mark_status(
                ep["id"],
                "ready",
                audio_filename=result["audio_filename"],
                file_size=result["file_size"],
                duration=result["duration"] or ep.get("duration"),
                description=result["description"],
                published_at=result["published_at"],
                chapters=result.get("chapters", []),
                title=result.get("title") or ep["title"]
            )
            print(f"✅ Ready: {ep['title']}")
        except Exception as e:
            print(f"❌ Failed: {e}")
            db.mark_status(ep["id"], "failed")

    print("\n🎉 Done! New episodes have been added to your podcast feed.")

def start_tunnel_background(port: int, config: Dict[str, Any]):
    """Launches Cloudflare Quick Tunnel in background and displays public HTTPS URL for any-browser access."""
    from tunnel import check_cloudflared
    bin_path = check_cloudflared()
    if not bin_path:
        logger.warning("cloudflared not found; tunnel could not be started.")
        return

    def run_tunnel():
        import re
        import subprocess
        cmd = [bin_path, "tunnel", "--url", f"http://localhost:{port}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', line)
            if match:
                tunnel_url = match.group(0)
                config["remote_url"] = tunnel_url
                print("\n" + "="*70)
                print("🌐 REMOTE DASHBOARD ACTIVE (ACCESS FROM ANY PHONE / BROWSER)")
                print(f"   {tunnel_url}")
                print("   Open this link on your phone from any cellular network or remote Wi-Fi!")
                print("="*70 + "\n")
                break

    t = threading.Thread(target=run_tunnel, daemon=True)
    t.start()

def cmd_serve(args, config):
    _, _, _, server = get_components(config)
    host = args.host or config.get("server_host", "0.0.0.0")
    port = args.port or config.get("server_port", 8080)
    base_url = config.get("public_base_url", f"http://localhost:{port}")

    if getattr(args, "tunnel", False):
        start_tunnel_background(port, config)

    from server.web import get_lan_ip
    lan_ip = get_lan_ip()

    print("\n" + "="*70)
    print("🎲 TOWERCAST SERVER RUNNING")
    print("="*70)
    print(f"  Local Dashboard:  http://localhost:{port}")
    print(f"  Home Wi-Fi Phone: http://{lan_ip}:{port}")
    print(f"  Podcast Feed:     {base_url}/feed.xml")
    if getattr(args, "tunnel", False):
        print("  Starting Cloudflare Remote Tunnel...")
    print("="*70 + "\n")

    server.run(host=host, port=port)

def cmd_daemon(args, config):
    """Runs periodic channel sync in background while serving the feed."""
    db, yt, dl, server = get_components(config)
    interval = config.get("sync_interval_minutes", 60) * 60

    def sync_loop():
        time.sleep(5)  # Short initial grace period
        while True:
            try:
                logger.info("Daemon running periodic channel sync...")
                cmd_sync(argparse.Namespace(limit=25), config)
            except Exception as e:
                logger.error(f"Daemon sync error: {e}")
            time.sleep(interval)

    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

    host = config.get("server_host", "0.0.0.0")
    port = config.get("server_port", 8080)
    print(f"🚀 TowerCast Daemon active (sync every {config.get('sync_interval_minutes', 60)} min). Serving on http://{host}:{port}")
    server.run(host=host, port=port)

def cmd_status(args, config):
    db, _, _, _ = get_components(config)
    ready = db.get_ready_episodes(100)
    pending = db.get_pending_episodes(100)
    queued = db.get_queued_episodes(100)

    base_url = config.get("public_base_url", "http://localhost:8080").rstrip("/")
    feed_url = f"{base_url}/feed.xml"

    total_bytes = sum(ep.get("file_size", 0) for ep in ready)
    mb = total_bytes / (1024 * 1024)

    print("\n" + "="*60)
    print("🎲 TOWERCAST STATUS")
    print("="*60)
    print(f"Channel:          {config['channel_url']}")
    print(f"Podcast Feed URL: {feed_url}")
    print(f"Episodes Ready:   {len(ready)} ({mb:.1f} MB total storage)")
    print(f"Pending Choice:   {len(pending)}")
    print(f"Currently Queued: {len(queued)}")
    print(f"Auto-Keywords:    {', '.join(config.get('auto_include_keywords', []))}")
    print("="*60 + "\n")

def cmd_github_setup(args, config):
    """Runs the interactive GitHub setup wizard."""
    from github.setup_wizard import run_wizard
    run_wizard()


def cmd_publish(args, config):
    """Uploads local ready episodes to GitHub Releases and triggers GitHub Pages feed rebuild."""
    gh_cfg = config.get("github", {})
    if not gh_cfg.get("enabled"):
        print("❌ GitHub integration is not enabled in config.json. Run 'python3 tower_cast.py github-setup' first.")
        return
    repo = gh_cfg.get("repo")
    db, _, _, _ = get_components(config)
    ready = db.get_ready_episodes()
    if not ready:
        print("ℹ No ready episodes found to upload.")
        return

    from github.feed_builder import build_release_body
    import subprocess

    res = subprocess.run(
        ["gh", "release", "list", "--repo", repo, "--limit", "100", "--json", "tagName", "--jq", ".[].tagName"],
        capture_output=True, text=True
    )
    existing_tags = set(line.strip() for line in res.stdout.strip().splitlines() if line.strip())

    uploaded = 0
    for ep in ready:
        tag = f"ep-{ep['id']}"
        if tag in existing_tags:
            continue
        audio_path = os.path.join(MEDIA_DIR, ep["audio_filename"])
        if not os.path.exists(audio_path):
            continue

        meta = {
            'id': ep['id'],
            'title': ep['title'],
            'duration': ep['duration'],
            'published_at': ep['published_at'],
            'thumbnail_url': ep['thumbnail_url'],
            'description': ep['description'],
            'chapters': json.loads(ep['chapters_json'] or '[]'),
            'matched_keyword': ep['matched_keyword'],
            'file_size': ep['file_size']
        }
        body = build_release_body(meta)
        print(f"⬆ Uploading to GitHub Release ({tag}): {ep['title']}...")
        sub_res = subprocess.run([
            "gh", "release", "create", tag,
            audio_path,
            "--title", ep["title"],
            "--notes", body,
            "--repo", repo
        ], capture_output=True, text=True)
        if sub_res.returncode == 0:
            print(f"  ✓ Uploaded {tag}")
            uploaded += 1
        else:
            print(f"  ❌ Failed to upload {tag}: {sub_res.stderr.strip()}")

    print(f"\n📊 {uploaded} new episode(s) uploaded to GitHub Releases.")
    if uploaded > 0 or getattr(args, "rebuild", False):
        print("🚀 Triggering GitHub Actions feed rebuild...")
        subprocess.run(["gh", "workflow", "run", "towercast.yml", "--repo", repo])
        pages_url = gh_cfg.get("pages_url", "").rstrip("/")
        print(f"✅ Feed will be updated at: {pages_url}/feed.xml")


def main():
    parser = argparse.ArgumentParser(description="TowerCast: YouTube → Private Podcast Feed")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Scan YouTube, auto-download matching shows, update feed")
    sync_parser.add_argument("--limit", type=int, default=None, help="Number of recent videos to scan per source")
    sync_parser.add_argument("--backlog", action="store_true", help="Deep backlog scan (150 videos per source)")

    # pick
    pick_parser = subparsers.add_parser("pick", help="Interactive terminal picker to select pending videos")
    pick_parser.add_argument("--limit", type=int, default=50, help="Number of pending videos to display")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the feed server and web dashboard")
    serve_parser.add_argument("--host", default=None, help="Host address to bind")
    serve_parser.add_argument("--port", type=int, default=None, help="Port to bind")
    serve_parser.add_argument("--tunnel", action="store_true", help="Launch Cloudflare Remote Tunnel for access anywhere outside home")

    # daemon
    daemon_parser = subparsers.add_parser("daemon", help="Run background periodic sync and feed server continuously")
    daemon_parser.add_argument("--tunnel", action="store_true", help="Launch Cloudflare Remote Tunnel for access anywhere outside home")

    # status
    subparsers.add_parser("status", help="Show current feed statistics and URL")

    # github-setup
    subparsers.add_parser(
        "github-setup",
        help="Interactive wizard: create GitHub repo, enable Pages, configure always-on feed"
    )

    # publish
    pub_parser = subparsers.add_parser("publish", help="Upload local ready episodes to GitHub Releases and rebuild feed")
    pub_parser.add_argument("--rebuild", action="store_true", help="Force rebuild even if no new episodes were uploaded")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config()

    if args.command == "sync":
        cmd_sync(args, config)
    elif args.command == "pick":
        cmd_pick(args, config)
    elif args.command == "serve":
        cmd_serve(args, config)
    elif args.command == "daemon":
        cmd_daemon(args, config)
    elif args.command == "status":
        cmd_status(args, config)
    elif args.command == "github-setup":
        cmd_github_setup(args, config)
    elif args.command == "publish":
        cmd_publish(args, config)

if __name__ == "__main__":
    main()
