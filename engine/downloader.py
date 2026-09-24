"""
Audio downloader and metadata/chapter tagger using yt-dlp and ffmpeg.
Extracts pristine audio, embeds Dice Tower cover art, metadata, and chapter markers.
"""
import json
import logging
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

class Downloader:
    def __init__(self, media_dir: str, audio_format: str = "m4a", audio_quality: str = "192k", cookies_file: Optional[str] = None):
        self.media_dir = os.path.abspath(media_dir)
        self.audio_format = audio_format
        self.audio_quality = audio_quality
        self.cookies_file = cookies_file
        os.makedirs(self.media_dir, exist_ok=True)

    def download_episode(self, video_id: str, video_url: str) -> Dict[str, Any]:
        """
        Downloads the video audio stream, embeds thumbnails and chapters,
        and saves episode artifacts in media_dir/<video_id>/.
        Returns metadata dict about the downloaded episode.
        """
        ep_dir = os.path.join(self.media_dir, video_id)
        os.makedirs(ep_dir, exist_ok=True)

        output_template = os.path.join(ep_dir, f"{video_id}.%(ext)s")

        # Discover ffmpeg binary directory dynamically (macOS Homebrew or Linux /usr/bin)
        ffmpeg_dir = None
        ffmpeg_bin = shutil.which("ffmpeg")
        if ffmpeg_bin:
            ffmpeg_dir = os.path.dirname(ffmpeg_bin)
        elif os.path.exists("/opt/homebrew/bin/ffmpeg"):
            ffmpeg_dir = "/opt/homebrew/bin"
        elif os.path.exists("/usr/bin/ffmpeg"):
            ffmpeg_dir = "/usr/bin"

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
            cmd.extend(["--ffmpeg-location", ffmpeg_dir])

        if self.cookies_file and os.path.exists(self.cookies_file):
            cmd.extend(["--cookies", self.cookies_file])
            cmd.extend(["--extractor-args", "youtube:player_client=android,ios,web"])
        else:
            # Fallback client configuration when no cookies are provided (visionos avoids bot challenge)
            cmd.extend(["--extractor-args", "youtube:player_client=visionos,web"])

        # Enable challenge solver and JS runtime
        cmd.extend(["--remote-components", "ejs:github"])
        if shutil.which("node") and not shutil.which("deno"):
            cmd.extend(["--js-runtimes", "node"])

        cmd.extend([
            "-o", output_template,
            video_url
        ])

        logger.info(f"Starting audio download for {video_id} ({video_url})")
        proc = subprocess.run(cmd, capture_output=True, text=True)

        # yt-dlp may exit non-zero due to non-fatal warnings (missing JS runtime,
        # webp thumbnail conversion quirks, etc.) but still produce a valid audio file.
        # We check for the output file first and only raise if nothing was produced.
        audio_file = self._find_audio_file(ep_dir, video_id)

        if not audio_file:
            # Genuine failure — nothing was written to disk
            logger.error(f"No output file produced for {video_id}.\nSTDERR: {proc.stderr}")
            raise RuntimeError(f"yt-dlp failed (no audio file produced): {proc.stderr}")

        if proc.returncode != 0:
            logger.warning(f"yt-dlp exited {proc.returncode} for {video_id} but audio file found — treating as success with warnings.")

        file_size = os.path.getsize(audio_file)

        # Locate thumbnail image written to disk
        thumb_file = None
        for img_ext in ("jpg", "jpeg", "webp", "png"):
            candidate = os.path.join(ep_dir, f"{video_id}.{img_ext}")
            if os.path.exists(candidate):
                thumb_file = candidate
                break

        # Read info.json for complete metadata
        info_json_path = os.path.join(ep_dir, f"{video_id}.info.json")
        info_data = {}
        if os.path.exists(info_json_path):
            try:
                with open(info_json_path, "r", encoding="utf-8") as f:
                    info_data = json.load(f)
            except Exception as e:
                logger.warning(f"Could not parse info.json: {e}")

        # Parse publication date
        published_at = None
        upload_date = info_data.get("upload_date")
        if upload_date and len(upload_date) == 8:
            try:
                dt = datetime.strptime(upload_date, "%Y%m%d")
                published_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if not published_at:
            published_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        duration = info_data.get("duration")
        description = info_data.get("description", "")
        title = info_data.get("title")
        chapters = info_data.get("chapters", [])

        # Relative audio filename for serving via HTTP
        rel_audio_path = f"{video_id}/{os.path.basename(audio_file)}"

        return {
            "id": video_id,
            "title": title,
            "audio_filename": rel_audio_path,
            "audio_full_path": audio_file,
            "file_size": file_size,
            "duration": duration,
            "description": description,
            "published_at": published_at,
            "thumbnail_path": thumb_file,
            "chapters": chapters
        }

    def _find_audio_file(self, ep_dir: str, video_id: str) -> Optional[str]:
        """Locates the downloaded audio file in ep_dir."""
        # Try exact expected names first
        for ext in (self.audio_format, "m4a", "mp3", "opus", "aac"):
            candidate = os.path.join(ep_dir, f"{video_id}.{ext}")
            if os.path.exists(candidate):
                return candidate

        # Fallback: scan for any audio file that isn't a partial download or metadata
        if not os.path.exists(ep_dir):
            return None
        audio_exts = {".m4a", ".mp3", ".opus", ".aac", ".ogg", ".wav"}
        candidates = [
            f for f in os.listdir(ep_dir)
            if os.path.splitext(f)[1].lower() in audio_exts
            and not f.endswith(".part")
            and not f.endswith(".temp.m4a")
        ]
        if candidates:
            return os.path.join(ep_dir, sorted(candidates)[0])
        return None
