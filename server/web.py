"""
Lightweight Bottle Web Server for TowerCast.
Serves podcast RSS feed, audio files with HTTP byte-range support, and the curation dashboard.
"""
import os
import json
import socket
import logging
import subprocess
import threading
from typing import Dict, Any, Optional, List
from bottle import Bottle, request, response, static_file, template, TEMPLATE_PATH

from engine.database import Database
from engine.feed_generator import FeedGenerator, format_duration

logger = logging.getLogger(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def get_lan_ip() -> str:
    """Detects local LAN IP address (e.g. 192.168.x.x) for phone access on home Wi-Fi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class WebServer:
    def __init__(
        self,
        config: Dict[str, Any],
        db: Database,
        downloader,
        youtube_engine,
        media_dir: str
    ):
        self.config = config
        self.db = db
        self.downloader = downloader
        self.youtube_engine = youtube_engine
        self.media_dir = os.path.abspath(media_dir)
        self.feed_generator = FeedGenerator(config)
        self.app = Bottle()

        # Set up template path
        template_dir = os.path.join(os.path.dirname(__file__), "templates")
        TEMPLATE_PATH.insert(0, template_dir)

        # Background worker lock and thread
        self.worker_thread: Optional[threading.Thread] = None
        self._is_processing = False

        self._register_routes()

    def _save_config(self):
        """Saves current config back to config.json and pushes to git if enabled."""
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
            logger.info("Saved config.json")
        except Exception as e:
            logger.error(f"Failed to save config.json: {e}")

        # Git push in background if github integration is enabled
        if self.config.get("github", {}).get("enabled"):
            def git_push():
                try:
                    repo_dir = os.path.dirname(CONFIG_PATH)
                    subprocess.run(["git", "-C", repo_dir, "add", "config.json"], check=False)
                    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "chore: update show rules from dashboard"], check=False)
                    subprocess.run(["git", "-C", repo_dir, "push"], check=False)
                    logger.info("Pushed updated config.json to GitHub")
                except Exception as e:
                    logger.warning(f"Could not push config update to GitHub: {e}")

            threading.Thread(target=git_push, daemon=True).start()

    def _sync_ready_episodes_to_github(self):
        """If GitHub is enabled, uploads any newly ready episodes to GitHub Releases and rebuilds feed."""
        if not self.config.get("github", {}).get("enabled"):
            return

        def run_github_sync():
            try:
                repo = self.config.get("github", {}).get("repo")
                if not repo:
                    return

                from github.feed_builder import build_release_body
                ready = self.db.get_ready_episodes()
                if not ready:
                    return

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
                    audio_path = os.path.join(self.media_dir, ep["audio_filename"])
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
                    logger.info(f"Uploading new episode to GitHub Releases ({tag}): {ep['title']}")
                    sub_res = subprocess.run([
                        "gh", "release", "create", tag,
                        audio_path,
                        "--title", ep["title"],
                        "--notes", body,
                        "--repo", repo
                    ], capture_output=True, text=True)
                    if sub_res.returncode == 0:
                        uploaded += 1

                if uploaded > 0:
                    logger.info(f"Uploaded {uploaded} episode(s) to GitHub. Triggering feed rebuild...")
                    subprocess.run(["gh", "workflow", "run", "towercast.yml", "--repo", repo], check=False)
            except Exception as e:
                logger.error(f"Error in background GitHub upload: {e}")

        threading.Thread(target=run_github_sync, daemon=True).start()

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            pending = self.db.get_pending_episodes(limit=100)
            ready = self.db.get_ready_episodes(limit=100)
            queued = self.db.get_queued_episodes(limit=100)
            port = self.config.get("server_port", 8080)
            base_url = self.config.get("public_base_url", f"http://localhost:{port}").rstrip("/")
            feed_url = f"{base_url}/feed.xml"

            lan_ip = get_lan_ip()
            lan_url = f"http://{lan_ip}:{port}"
            remote_url = self.config.get("remote_url", "")
            gh_pages_url = self.config.get("github", {}).get("pages_url", "")
            gh_feed_url = f"{gh_pages_url.rstrip('/')}/feed.xml" if gh_pages_url else ""

            return template(
                "index.html",
                pending_episodes=pending,
                ready_episodes=ready,
                queued_episodes=queued,
                feed_url=feed_url,
                gh_feed_url=gh_feed_url,
                gh_pages_url=gh_pages_url,
                lan_url=lan_url,
                remote_url=remote_url,
                auto_keywords=self.config.get("auto_include_keywords", []),
                favorites_shows=self.config.get("favorites", {}).get("shows", []),
                format_duration=format_duration
            )

        @app.route("/feed.xml")
        def feed():
            ready_episodes = self.db.get_ready_episodes(limit=self.config.get("max_episodes_in_feed", 50))
            xml_content = self.feed_generator.generate_feed_xml(ready_episodes)
            response.content_type = "application/rss+xml; charset=utf-8"
            return xml_content

        @app.route("/audio/<filepath:path>")
        def serve_audio(filepath):
            # Bottle's static_file automatically handles HTTP Range requests (206 Partial Content)
            return static_file(filepath, root=self.media_dir)

        @app.route("/api/queue", method="POST")
        def api_queue():
            data = request.json or {}
            video_id = data.get("video_id")
            if not video_id:
                response.status = 400
                return {"error": "Missing video_id"}

            success = self.db.queue_episode(video_id)
            if success:
                self.trigger_background_processing()
            return {"ok": success}

        @app.route("/api/skip", method="POST")
        def api_skip():
            data = request.json or {}
            video_id = data.get("video_id")
            if not video_id:
                response.status = 400
                return {"error": "Missing video_id"}

            success = self.db.skip_episode(video_id)
            return {"ok": success}

        @app.route("/api/sync", method="POST")
        def api_sync():
            data = request.json or {}
            limit = int(data.get("limit", 30))
            self.trigger_background_sync(limit=limit)
            return {"ok": True, "message": f"Channel sync started (limit={limit})"}

        @app.route("/api/favorites/add", method="POST")
        def api_favorites_add():
            data = request.json or {}
            keyword = (data.get("keyword") or "").strip()
            rule_type = data.get("type", "auto")  # "auto" or "favorite"
            max_per_sync = data.get("max_per_sync")

            if not keyword:
                response.status = 400
                return {"ok": False, "error": "Keyword cannot be empty"}

            try:
                if max_per_sync is not None and str(max_per_sync).isdigit():
                    max_per_sync = int(max_per_sync)
                else:
                    max_per_sync = None
            except Exception:
                max_per_sync = None

            if rule_type == "auto":
                existing = [k.lower() for k in self.config.get("auto_include_keywords", [])]
                if keyword.lower() not in existing:
                    self.config.setdefault("auto_include_keywords", []).append(keyword)
            else:
                fav = self.config.setdefault("favorites", {"enabled": True, "shows": []})
                shows = fav.setdefault("shows", [])
                fav["enabled"] = True
                existing = [s.get("keyword", "").lower() for s in shows]
                if keyword.lower() not in existing:
                    entry = {"keyword": keyword}
                    if max_per_sync is not None:
                        entry["max_per_sync"] = max_per_sync
                    shows.append(entry)

            self._save_config()

            # Refresh YouTubeEngine with updated keywords
            self.youtube_engine.auto_keywords = [k.lower() for k in self.config.get("auto_include_keywords", [])]
            self.youtube_engine.favorites_config = self.config.get("favorites", {})
            self.youtube_engine._favorites_keywords = [
                s.get("keyword", "").strip().lower()
                for s in self.config.get("favorites", {}).get("shows", [])
                if s.get("keyword")
            ]

            # Re-evaluate any pending episodes matching the new keyword
            pending = self.db.get_pending_episodes(limit=200)
            queued_count = 0
            for ep in pending:
                if keyword.lower() in (ep.get("title") or "").lower():
                    self.db.queue_episode(ep["id"])
                    queued_count += 1

            if queued_count > 0:
                self.trigger_background_processing()

            return {
                "ok": True,
                "message": f"Added rule for '{keyword}' ({queued_count} pending episodes queued)",
                "auto_keywords": self.config.get("auto_include_keywords", []),
                "favorites_shows": self.config.get("favorites", {}).get("shows", [])
            }

        @app.route("/api/favorites/delete", method="POST")
        def api_favorites_delete():
            data = request.json or {}
            keyword = (data.get("keyword") or "").strip().lower()
            if not keyword:
                response.status = 400
                return {"ok": False, "error": "Keyword cannot be empty"}

            # Remove from auto_include_keywords
            self.config["auto_include_keywords"] = [
                k for k in self.config.get("auto_include_keywords", [])
                if k.lower() != keyword
            ]

            # Remove from favorites.shows
            fav = self.config.get("favorites", {})
            if "shows" in fav:
                fav["shows"] = [
                    s for s in fav.get("shows", [])
                    if s.get("keyword", "").lower() != keyword
                ]

            self._save_config()

            # Refresh YouTubeEngine
            self.youtube_engine.auto_keywords = [k.lower() for k in self.config.get("auto_include_keywords", [])]
            self.youtube_engine.favorites_config = self.config.get("favorites", {})
            self.youtube_engine._favorites_keywords = [
                s.get("keyword", "").strip().lower()
                for s in self.config.get("favorites", {}).get("shows", [])
                if s.get("keyword")
            ]

            return {
                "ok": True,
                "message": f"Removed rule for '{keyword}'",
                "auto_keywords": self.config.get("auto_include_keywords", []),
                "favorites_shows": self.config.get("favorites", {}).get("shows", [])
            }

    def trigger_background_sync(self, limit: int = 30):
        """Runs channel discovery and starts processing in background."""
        def run_sync():
            try:
                logger.info(f"Background sync triggered (limit={limit})...")
                _, entries = self.youtube_engine.fetch_channel_entries(limit=limit)
                new_auto = 0
                for entry in entries:
                    classified = self.youtube_engine.classify_entry(entry)
                    inserted = self.db.upsert_discovered_episode(
                        video_id=classified["id"],
                        title=classified["title"],
                        url=classified["url"],
                        published_at=None,
                        duration=classified["duration"],
                        thumbnail_url=classified["thumbnail_url"],
                        status=classified["target_status"],
                        matched_keyword=classified["matched_keyword"]
                    )
                    if inserted and classified["target_status"] == "queued":
                        new_auto += 1

                logger.info(f"Sync complete. Found {len(entries)} entries ({new_auto} auto-queued).")
                self.process_queue()
            except Exception as e:
                logger.error(f"Sync error: {e}", exc_info=True)

        t = threading.Thread(target=run_sync, daemon=True)
        t.start()

    def trigger_background_processing(self):
        """Spawns thread to process queued items."""
        if self._is_processing:
            return
        t = threading.Thread(target=self.process_queue, daemon=True)
        t.start()

    def process_queue(self):
        """Processes all queued episodes one by one."""
        if self._is_processing:
            return
        self._is_processing = True
        try:
            queued = self.db.get_queued_episodes()
            for ep in queued:
                video_id = ep["id"]
                video_url = ep["url"]
                logger.info(f"Processing queued episode: {ep['title']} ({video_id})")
                self.db.mark_status(video_id, "downloading")
                try:
                    result = self.downloader.download_episode(video_id, video_url)
                    self.db.mark_status(
                        video_id,
                        "ready",
                        audio_filename=result["audio_filename"],
                        file_size=result["file_size"],
                        duration=result["duration"] or ep.get("duration"),
                        description=result["description"],
                        published_at=result["published_at"],
                        chapters=result.get("chapters", []),
                        title=result.get("title") or ep["title"]
                    )
                    logger.info(f"Successfully processed episode: {video_id}")
                except Exception as e:
                    logger.error(f"Failed to process episode {video_id}: {e}")
                    self.db.mark_status(video_id, "failed")

            # Check if any new episodes need to be synced to GitHub Releases
            self._sync_ready_episodes_to_github()
        finally:
            self._is_processing = False

    def run(self, host: str = "0.0.0.0", port: int = 8080):
        logger.info(f"Starting TowerCast Server at http://{host}:{port}")
        self.app.run(host=host, port=port, quiet=True)
