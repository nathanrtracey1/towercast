"""
YouTube scraper and classifier using yt-dlp.
Detects Shorts, applies auto-include keyword rules, and discovers videos.
Supports both auto_include_keywords (unlimited) and favorites.shows (with per-sync caps).
"""
import json
import logging
import re
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)


def parse_youtube_url(url_or_id: str) -> Optional[str]:
    """Extracts the 11-character YouTube video ID from a URL or raw ID."""
    if not url_or_id:
        return None
    raw = url_or_id.strip()
    if re.fullmatch(r"[a-zA-Z0-9_-]{11}", raw):
        return raw
    patterns = [
        r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/live\/|\/shorts\/)([a-zA-Z0-9_-]{11})",
        r"[?&]v=([a-zA-Z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, raw)
        if m:
            return m.group(1)
    return None


class YouTubeEngine:
    def __init__(
        self,
        channel_url: str,
        auto_keywords: List[str],
        exclude_shorts: bool = True,
        shorts_max_seconds: int = 60,
        favorites_config: Optional[Dict[str, Any]] = None,
        auto_download_all_new: bool = False,
    ):
        self.channel_url = channel_url
        self.auto_keywords = [k.strip().lower() for k in auto_keywords if k.strip()]
        self.exclude_shorts = exclude_shorts
        self.shorts_max_seconds = shorts_max_seconds
        self.favorites_config = favorites_config or {}
        self.auto_download_all_new = auto_download_all_new

        # Flatten all favorites keywords for fast lookup
        self._favorites_keywords: List[str] = []
        if self.favorites_config.get("enabled"):
            for show in self.favorites_config.get("shows", []):
                kw = show.get("keyword", "").strip().lower()
                if kw:
                    self._favorites_keywords.append(kw)

    def _get_scan_urls(self) -> List[str]:
        """Returns list of URLs to scan, ensuring both regular videos and live streams are fetched."""
        if isinstance(self.channel_url, list):
            return self.channel_url
        urls = [self.channel_url]
        if "/videos" in self.channel_url:
            streams_url = self.channel_url.replace("/videos", "/streams")
            if streams_url not in urls:
                urls.append(streams_url)
        elif self.channel_url.startswith("https://www.youtube.com/@") and not any(x in self.channel_url for x in ["/videos", "/streams", "/playlists"]):
            base = self.channel_url.rstrip("/")
            urls = [f"{base}/videos", f"{base}/streams"]
        return urls

    @staticmethod
    def is_scheduled_or_live(entry: Dict[str, Any]) -> bool:
        """
        Determines if a video entry is currently an upcoming scheduled live stream
        or an active in-progress live stream.
        """
        status = entry.get("live_status")
        if status in ("is_upcoming", "is_live", "post_live"):
            return True
        # If duration is missing and status indicates live or is not confirmed completed
        if entry.get("duration") is None and status != "was_live":
            title = (entry.get("title") or "").lower()
            if "live in " in title or "premieres in " in title or "scheduled for" in title:
                return True
            if status in ("is_upcoming", "is_live", "post_live"):
                return True
        return False

    def fetch_channel_entries(
        self, limit: int = 30
    ) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Extracts recent video entries from the channel (both uploads and live streams).
        Uses yt-dlp --flat-playlist for fast metadata retrieval.
        Filters out scheduled/active live streams and sorts all entries chronologically.
        Returns: (channel_metadata, list_of_video_entries)
        """
        urls = self._get_scan_urls()
        all_entries: List[Dict[str, Any]] = []
        seen_ids = set()
        channel_info = None

        for url in urls:
            cmd = [
                "yt-dlp",
                "--flat-playlist",
                "--dump-single-json",
                "--extractor-args", "youtube:approximate_date",
                "--playlist-end", str(limit),
                url,
            ]
            logger.info(f"Scanning source via yt-dlp: {url} (limit={limit})")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                data = json.loads(result.stdout)
                entries = data.get("entries", [])

                if not channel_info:
                    channel_info = {
                        "id": data.get("id"),
                        "title": data.get("title", "The Dice Tower"),
                        "channel": data.get("channel", data.get("uploader", "The Dice Tower")),
                        "description": data.get("description", ""),
                        "avatar": None,
                    }
                    for t in data.get("thumbnails", []):
                        if t.get("id") in ("avatar_uncropped", "7") or "avatar" in t.get("url", ""):
                            channel_info["avatar"] = t.get("url")
                            break
                    if not channel_info["avatar"] and data.get("thumbnails"):
                        channel_info["avatar"] = data["thumbnails"][-1].get("url")

                for e in entries:
                    eid = e.get("id")
                    if eid and eid not in seen_ids:
                        if self.is_scheduled_or_live(e):
                            logger.info(f"Skipping scheduled/active live stream: {eid} - {e.get('title')}")
                            continue
                        seen_ids.add(eid)
                        all_entries.append(e)
            except Exception as e:
                logger.warning(f"Error scanning source {url}: {e}")

        # Sort all entries in reverse chronological order (newest first)
        def _sort_key(entry: Dict[str, Any]) -> float:
            ts = entry.get("timestamp") or entry.get("release_timestamp")
            if ts and isinstance(ts, (int, float)):
                return float(ts)
            ud = entry.get("upload_date")
            if ud and len(str(ud)) == 8:
                try:
                    dt = datetime.strptime(str(ud), "%Y%m%d").replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
            return 0.0

        has_timestamps = any(_sort_key(e) > 0 for e in all_entries)
        if has_timestamps:
            all_entries.sort(key=_sort_key, reverse=True)

        if not channel_info:
            channel_info = {
                "id": "thedicetower",
                "title": "The Dice Tower",
                "channel": "The Dice Tower",
                "description": "",
                "avatar": None
            }

        return channel_info, all_entries

    def is_short(self, entry: Dict[str, Any]) -> bool:
        """Determines if a video entry is a YouTube Short."""
        if not self.exclude_shorts:
            return False

        duration = entry.get("duration")
        if duration is not None and duration <= self.shorts_max_seconds:
            return True

        title = (entry.get("title") or "").lower()
        url = (entry.get("url") or "")
        if "#shorts" in title or "/shorts/" in url:
            return True

        return False

    def match_auto_keyword(self, title: str) -> Optional[str]:
        """Checks if title matches any auto_include_keywords."""
        lower_title = title.lower()
        for kw in self.auto_keywords:
            if kw in lower_title:
                return kw
        return None

    def match_favorite_keyword(self, title: str) -> Optional[str]:
        """Checks if title matches any favorites.shows keyword."""
        lower_title = title.lower()
        for kw in self._favorites_keywords:
            if kw in lower_title:
                return kw
        return None

    def classify_entry(self, entry: Dict[str, Any], is_backlog: bool = False) -> Dict[str, Any]:
        """
        Classifies a video entry:
          target_status = 'queued'  → auto-download (all new videos or matched keyword)
          target_status = 'pending' → backlog manual pick
          target_status = 'skipped' → Short or scheduled stream
        """
        title = entry.get("title", "")
        is_sh = self.is_short(entry)
        is_sched_or_live = self.is_scheduled_or_live(entry)
        matched_kw = None
        is_favorite = False

        if not is_sh and not is_sched_or_live:
            matched_kw = self.match_auto_keyword(title)
            if matched_kw is None:
                fav_kw = self.match_favorite_keyword(title)
                if fav_kw:
                    matched_kw = fav_kw
                    is_favorite = True

        if is_sched_or_live or is_sh:
            target_status = "skipped"
        elif matched_kw:
            target_status = "queued"
        elif self.auto_download_all_new and not is_backlog:
            target_status = "queued"
            matched_kw = "New Episode"
        else:
            target_status = "pending"

        # Best thumbnail
        best_thumb = None
        for t in reversed(entry.get("thumbnails", [])):
            if t.get("url"):
                best_thumb = t.get("url")
                break

        # Calculate published_at timestamp if present
        published_at = None
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        if ts and isinstance(ts, (int, float)):
            try:
                published_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        if not published_at and entry.get("upload_date"):
            ud = str(entry.get("upload_date"))
            if len(ud) == 8:
                try:
                    published_at = datetime.strptime(ud, "%Y%m%d").strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

        return {
            "id": entry.get("id"),
            "title": title,
            "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
            "duration": entry.get("duration"),
            "thumbnail_url": best_thumb,
            "is_short": is_sh,
            "is_scheduled_or_live": is_sched_or_live,
            "published_at": published_at,
            "matched_keyword": matched_kw,
            "is_favorite": is_favorite,
            "target_status": target_status,
        }
