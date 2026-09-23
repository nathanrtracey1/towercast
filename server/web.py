"""
Lightweight Bottle Web Server for TowerCast.
Serves podcast RSS feed, audio files with HTTP byte-range support, and the curation dashboard.
"""
import os
import json
import logging
import threading
from typing import Dict, Any, Optional
from bottle import Bottle, request, response, static_file, template, TEMPLATE_PATH

from engine.database import Database
from engine.feed_generator import FeedGenerator, format_duration

logger = logging.getLogger(__name__)

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

    def _register_routes(self):
        app = self.app

        @app.route("/")
        def index():
            pending = self.db.get_pending_episodes(limit=50)
            ready = self.db.get_ready_episodes(limit=50)
            queued = self.db.get_queued_episodes(limit=50)
            base_url = self.config.get("public_base_url", f"http://localhost:{self.config.get('server_port', 8080)}").rstrip("/")
            feed_url = f"{base_url}/feed.xml"

            return template(
                "index.html",
                pending_episodes=pending,
                ready_episodes=ready,
                queued_episodes=queued,
                feed_url=feed_url,
                auto_keywords=self.config.get("auto_include_keywords", []),
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
            self.trigger_background_sync()
            return {"ok": True, "message": "Channel sync & processing started"}

    def trigger_background_sync(self):
        """Runs channel discovery and starts processing in background."""
        def run_sync():
            try:
                logger.info("Background sync triggered...")
                _, entries = self.youtube_engine.fetch_channel_entries(limit=30)
                for entry in entries:
                    classified = self.youtube_engine.classify_entry(entry)
                    self.db.upsert_discovered_episode(
                        video_id=classified["id"],
                        title=classified["title"],
                        url=classified["url"],
                        published_at=None,
                        duration=classified["duration"],
                        thumbnail_url=classified["thumbnail_url"],
                        status=classified["target_status"],
                        matched_keyword=classified["matched_keyword"]
                    )
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
        finally:
            self._is_processing = False

    def run(self, host: str = "0.0.0.0", port: int = 8080):
        logger.info(f"Starting TowerCast Server at http://{host}:{port}")
        self.app.run(host=host, port=port, quiet=True)
