"""
Publishing helpers for episodes downloaded on the Mac (CLI `publish` and the local dashboard).
Audio goes to R2; metadata goes to a GitHub Release, matching what the cloud builder does.
Needs the same R2_* / FEED_BASE_URL / AUDIO_TOKEN environment variables as the workflow.
"""
import logging
import os
import subprocess
from typing import Any, Dict

from github import r2
from github.feed_builder import build_release_body

logger = logging.getLogger(__name__)


def publish_episode(meta: Dict[str, Any], audio_path: str, repo: str) -> bool:
    """Uploads one local episode's audio to R2 and creates its metadata release."""
    if not r2.is_configured():
        logger.warning(f"R2 is not configured in this environment; skipping publish of {meta.get('id')}")
        return False

    ext = os.path.splitext(audio_path)[1].lstrip(".") or "m4a"
    content_type = "audio/mp4" if ext == "m4a" else "audio/mpeg"
    try:
        r2.upload_file(audio_path, r2.audio_key(meta["id"], ext), content_type)
    except Exception as e:
        logger.error(f"R2 upload failed for {meta['id']}: {e}")
        return False

    body = build_release_body(dict(meta, audio_ext=ext))
    res = subprocess.run(
        ["gh", "release", "create", f"ep-{meta['id']}", "--title", meta["title"], "--notes", body, "--repo", repo],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        logger.error(f"Failed to create release for {meta['id']}: {res.stderr.strip()}")
        return False
    return True


def remove_episode_audio(video_id: str):
    """Deletes an episode's audio from R2 (no-op if R2 isn't configured here)."""
    if not r2.is_configured():
        return
    for ext in ("m4a", "mp3"):
        try:
            r2.delete_key(r2.audio_key(video_id, ext))
        except Exception as e:
            logger.warning(f"Could not delete R2 audio for {video_id}: {e}")
