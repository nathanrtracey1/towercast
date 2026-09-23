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
    )
    dl = Downloader(
        media_dir=MEDIA_DIR,
        audio_format=config.get("audio_format", "m4a"),
        audio_quality=config.get("audio_quality", "192k")
    )
    server = WebServer(config, db, dl, yt, MEDIA_DIR)
    return db, yt, dl, server

def cmd_sync(args, config):
    db, yt, dl, _ = get_components(config)
    limit = args.limit or 30
    print(f"\n📡 Scanning channel: {config['channel_url']} (latest {limit} videos)...")

    try:
        channel_info, entries = yt.fetch_channel_entries(limit=limit)
    except Exception as e:
        print(f"❌ Error scanning YouTube: {e}")
        return

    print(f"📺 Channel: {channel_info['channel']} - Found {len(entries)} entries.\n")

    new_auto_queued = 0
    new_pending = 0
    shorts_skipped = 0

    for entry in entries:
        classified = yt.classify_entry(entry)
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

def cmd_serve(args, config):
    _, _, _, server = get_components(config)
    host = args.host or config.get("server_host", "0.0.0.0")
    port = args.port or config.get("server_port", 8080)
    base_url = config.get("public_base_url", f"http://localhost:{port}")

    print("\n" + "="*70)
    print("🎲 TOWERCAST SERVER RUNNING")
    print("="*70)
    print(f"  Web Dashboard:  http://localhost:{port}")
    print(f"  Podcast Feed:   {base_url}/feed.xml")
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


def cmd_favorites_list(args, config):
    """Lists configured favorites shows."""
    fav = config.get("favorites", {})
    shows = fav.get("shows", [])
    auto_kw = config.get("auto_include_keywords", [])

    print("\n🎲 TowerCast — Configured Shows\n")
    print("Auto-include (always grab, no cap):")
    for kw in auto_kw:
        print(f"  ⚡ {kw}")

    print("\nFavorites (grab with optional per-sync cap):")
    if not shows:
        print("  (none — add with: python3 tower_cast.py favorites-add \"Show Keyword\")")
    else:
        for s in shows:
            cap = s.get("max_per_sync")
            cap_str = f"  [max {cap}/sync]" if cap else "  [unlimited]"
            print(f"  ★  {s['keyword']}{cap_str}")
    print()


def cmd_favorites_add(args, config):
    """Adds a keyword to favorites.shows in config.json."""
    keyword = args.keyword.strip()
    max_per_sync = args.max_per_sync  # may be None

    fav = config.setdefault("favorites", {"enabled": True, "shows": []})
    shows = fav.setdefault("shows", [])
    fav["enabled"] = True

    # Avoid duplicates
    existing_kws = [s.get("keyword", "").lower() for s in shows]
    if keyword.lower() in existing_kws:
        print(f"  ℹ  '{keyword}' is already in favorites.")
        return

    entry = {"keyword": keyword}
    if max_per_sync is not None:
        entry["max_per_sync"] = max_per_sync

    shows.append(entry)

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    cap_str = f" (max {max_per_sync}/sync)" if max_per_sync else ""
    print(f"  ★  Added favorite: '{keyword}'{cap_str}")
    print(f"     Episodes matching this keyword will be auto-downloaded.")
    if config.get("github", {}).get("enabled"):
        print(f"     Push config.json to GitHub to apply: git add config.json && git commit -m 'Add favorite' && git push")


def main():
    parser = argparse.ArgumentParser(description="TowerCast: YouTube → Private Podcast Feed")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sync
    sync_parser = subparsers.add_parser("sync", help="Scan YouTube, auto-download matching shows, update feed")
    sync_parser.add_argument("--limit", type=int, default=30, help="Number of recent videos to scan")

    # pick
    pick_parser = subparsers.add_parser("pick", help="Interactive terminal picker to select pending videos")
    pick_parser.add_argument("--limit", type=int, default=50, help="Number of pending videos to display")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the feed server and web dashboard")
    serve_parser.add_argument("--host", default=None, help="Host address to bind")
    serve_parser.add_argument("--port", type=int, default=None, help="Port to bind")

    # daemon
    subparsers.add_parser("daemon", help="Run background periodic sync and feed server continuously")

    # status
    subparsers.add_parser("status", help="Show current feed statistics and URL")

    # github-setup
    subparsers.add_parser(
        "github-setup",
        help="Interactive wizard: create GitHub repo, enable Pages, configure always-on feed"
    )

    # favorites-list
    subparsers.add_parser("favorites-list", help="List configured auto-include keywords and favorites")

    # favorites-add
    fav_add = subparsers.add_parser("favorites-add", help="Add a show keyword to favorites (auto-downloaded)")
    fav_add.add_argument("keyword", help="Keyword to match in video titles (e.g. 'Zee Garcia')")
    fav_add.add_argument("--max-per-sync", type=int, default=None,
                         help="Max episodes to grab per sync (default: unlimited)")

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
    elif args.command == "favorites-list":
        cmd_favorites_list(args, config)
    elif args.command == "favorites-add":
        cmd_favorites_add(args, config)

if __name__ == "__main__":
    main()
