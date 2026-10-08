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
        self.ffmpeg_bin = shutil.which("ffmpeg") or ("/opt/homebrew/bin/ffmpeg" if os.path.exists("/opt/homebrew/bin/ffmpeg") else "/usr/bin/ffmpeg")
        self.ffprobe_bin = shutil.which("ffprobe") or ("/opt/homebrew/bin/ffprobe" if os.path.exists("/opt/homebrew/bin/ffprobe") else "/usr/bin/ffprobe")
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
            "--retries", "10",
            "--fragment-retries", "10",
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

        # Add trailing silence cushion (4 seconds) so Overcast / Smart Speed / streaming EOF never cuts off speech
        self._pad_audio_tail(audio_file, pad_seconds=4)

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

        # Parse publication date with exact second precision
        published_at = None
        ts = info_data.get("release_timestamp") or info_data.get("timestamp")
        if ts and isinstance(ts, (int, float)):
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                published_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        if not published_at:
            upload_date = info_data.get("upload_date")
            if upload_date and len(upload_date) == 8:
                try:
                    dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                    published_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

        if not published_at:
            published_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        exact_duration = self._get_exact_duration(audio_file)
        duration = exact_duration if exact_duration is not None else info_data.get("duration")
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

    def _pad_audio_tail(self, audio_path: str, pad_seconds: int = 4) -> bool:
        """
        Appends pad_seconds of trailing silence to the audio file.
        This provides a crucial audio cushion so podcast players like Overcast
        (and features like Smart Speed or streaming EOF) never clip the sign-off or outro.
        """
        if not os.path.exists(audio_path) or pad_seconds <= 0 or not self.ffmpeg_bin:
            return False

        temp_padded = audio_path + ".padded.m4a"
        try:
            cmd = [
                self.ffmpeg_bin, "-y",
                "-i", audio_path,
                "-af", f"apad=pad_dur={pad_seconds}",
                "-map", "0:a",
                "-map", "0:v?",
                "-map_metadata", "0",
                "-map_chapters", "0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", self.audio_quality if self.audio_quality else "192k",
                "-movflags", "+faststart",
                temp_padded
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and os.path.exists(temp_padded) and os.path.getsize(temp_padded) > 0:
                os.replace(temp_padded, audio_path)
                logger.info(f"Padded {pad_seconds}s trailing audio buffer to {os.path.basename(audio_path)}")
                return True
            else:
                logger.warning(f"Could not pad audio: {res.stderr}")
        except Exception as e:
            logger.warning(f"Audio padding skipped: {e}")
        finally:
            if os.path.exists(temp_padded):
                try:
                    os.remove(temp_padded)
                except Exception:
                    pass
        return False

    def _get_exact_duration(self, audio_path: str) -> Optional[int]:
        """Probes the actual audio file for its exact duration in seconds."""
        if not self.ffprobe_bin or not os.path.exists(audio_path):
            return None
        try:
            res = subprocess.run(
                [self.ffprobe_bin, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                capture_output=True, text=True, check=True
            )
            val = float(res.stdout.strip())
            return int(round(val))
        except Exception as e:
            logger.warning(f"Could not determine exact duration via ffprobe: {e}")
            return None

