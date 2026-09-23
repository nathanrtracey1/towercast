"""
Unit tests for TowerCast components.
"""
import os
import tempfile
import unittest
from xml.etree import ElementTree as ET

from engine.database import Database
from engine.youtube import YouTubeEngine
from engine.feed_generator import FeedGenerator, format_duration

class TestTowerCast(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.db = Database(self.db_path)

        self.config = {
            "channel_url": "https://www.youtube.com/@TheDiceTower/videos",
            "auto_include_keywords": [
                "Board Game Breakfast",
                "Dice Tower News",
                "Crowdsurfing",
                "Top 10"
            ],
            "exclude_shorts": True,
            "shorts_max_seconds": 60,
            "public_base_url": "https://example.trycloudflare.com",
            "feed_title": "The Dice Tower (TowerCast)",
            "feed_author": "The Dice Tower"
        }
        self.yt = YouTubeEngine(
            self.config["channel_url"],
            self.config["auto_include_keywords"],
            self.config["exclude_shorts"],
            self.config["shorts_max_seconds"]
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_database_crud(self):
        inserted = self.db.upsert_discovered_episode(
            video_id="test123",
            title="Board Game Breakfast #500",
            url="https://youtube.com/watch?v=test123",
            published_at=None,
            duration=1200,
            thumbnail_url="https://img.youtube.com/vi/test123/hqdefault.jpg",
            status="queued",
            matched_keyword="board game breakfast"
        )
        self.assertTrue(inserted)

        # Duplicate check
        dup = self.db.upsert_discovered_episode(
            video_id="test123",
            title="Duplicate",
            url="https://youtube.com/watch?v=test123",
            published_at=None,
            duration=1200,
            thumbnail_url="",
            status="queued"
        )
        self.assertFalse(dup)

        # Mark ready
        self.db.mark_status(
            "test123",
            "ready",
            audio_filename="test123/test123.m4a",
            file_size=15000000,
            description="Episode description",
            chapters=[{"title": "Intro", "start_time": 0}, {"title": "Main", "start_time": 120}]
        )

        ep = self.db.get_episode("test123")
        self.assertEqual(ep["status"], "ready")
        self.assertEqual(ep["audio_filename"], "test123/test123.m4a")
        self.assertEqual(ep["file_size"], 15000000)

    def test_shorts_detection(self):
        short_entry = {"id": "s1", "title": "Quick Rule #shorts", "duration": 45}
        self.assertTrue(self.yt.is_short(short_entry))

        regular_entry = {"id": "r1", "title": "Dice Tower News Sept 2026", "duration": 1500}
        self.assertFalse(self.yt.is_short(regular_entry))

    def test_keyword_matching(self):
        e1 = {"id": "1", "title": "Board Game Breakfast 420", "duration": 1800}
        c1 = self.yt.classify_entry(e1)
        self.assertEqual(c1["target_status"], "queued")
        self.assertEqual(c1["matched_keyword"], "board game breakfast")

        e2 = {"id": "2", "title": "Top 10 Worker Placement Games", "duration": 2400}
        c2 = self.yt.classify_entry(e2)
        self.assertEqual(c2["target_status"], "queued")

        e3 = {"id": "3", "title": "Crowdsurfing with Zee Garcia", "duration": 1900}
        c3 = self.yt.classify_entry(e3)
        self.assertEqual(c3["target_status"], "queued")

        e4 = {"id": "4", "title": "Random Review: Space Base", "duration": 800}
        c4 = self.yt.classify_entry(e4)
        self.assertEqual(c4["target_status"], "pending")
        self.assertIsNone(c4["matched_keyword"])

    def test_feed_generation(self):
        feed_gen = FeedGenerator(self.config)
        episodes = [
            {
                "id": "ep1",
                "title": "Dice Tower News Live",
                "published_at": "2026-09-22 18:00:00",
                "duration": 1845,
                "audio_filename": "ep1/ep1.m4a",
                "file_size": 25000000,
                "description": "Weekly board gaming news update.",
                "thumbnail_url": "https://img.youtube.com/ep1.jpg",
                "chapters_json": '[{"title": "Crowdfunding", "start_time": 60}, {"title": "New Releases", "start_time": 300}]'
            }
        ]
        xml_str = feed_gen.generate_feed_xml(episodes)
        self.assertIn("<rss", xml_str)
        self.assertIn("Dice Tower News Live", xml_str)
        self.assertIn("https://example.trycloudflare.com/audio/ep1/ep1.m4a", xml_str)
        self.assertIn('type="audio/mp4"', xml_str)
        self.assertIn("01:00 - Crowdfunding", xml_str)

        # Parse XML to verify valid structure
        root = ET.fromstring(xml_str)
        channel = root.find("channel")
        self.assertIsNotNone(channel)
        item = channel.find("item")
        self.assertIsNotNone(item)
        enclosure = item.find("enclosure")
        self.assertIsNotNone(enclosure)
        self.assertEqual(enclosure.attrib["length"], "25000000")

    def test_delete_and_cloud_actions(self):
        from github.feed_builder import tag_for, video_id_from_tag, build_release_body, parse_release_body

        tag = tag_for("abc_123")
        self.assertEqual(tag, "ep-abc_123")
        self.assertEqual(video_id_from_tag(tag), "abc_123")
        self.assertIsNone(video_id_from_tag("other-tag"))

        body = build_release_body({"id": "abc_123", "title": "Test Title", "duration": 300})
        meta = parse_release_body(body)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["id"], "abc_123")
        self.assertEqual(meta["title"], "Test Title")

        # Test local db skip (deletion from feed)
        self.db.upsert_discovered_episode("del_1", "Delete Me", "url", None, 100, "", "ready")
        ready_before = self.db.get_ready_episodes()
        self.assertTrue(any(e["id"] == "del_1" for e in ready_before))
        self.db.skip_episode("del_1", allow_ready=True)
        ready_after = self.db.get_ready_episodes()
        self.assertFalse(any(e["id"] == "del_1" for e in ready_after))

if __name__ == "__main__":
    unittest.main()
