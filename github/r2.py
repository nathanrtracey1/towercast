"""
Cloudflare R2 (S3-compatible) storage helpers for TowerCast audio and feed files.
Configured entirely through environment variables:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (default "towercast"),
  FEED_BASE_URL (the Worker URL) and AUDIO_TOKEN (secret path segment for audio).
"""
import os
from typing import Optional

REQUIRED_ENV = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "FEED_BASE_URL", "AUDIO_TOKEN")


def is_configured() -> bool:
    return all(os.environ.get(k) for k in REQUIRED_ENV)


def _client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def bucket() -> str:
    return os.environ.get("R2_BUCKET", "towercast")


def audio_key(video_id: str, ext: str = "m4a") -> str:
    return f"audio/{video_id}.{ext}"


def audio_url(video_id: str, ext: str = "m4a") -> str:
    """Enclosure URL served by the Worker (secret token in the path)."""
    base = os.environ["FEED_BASE_URL"].rstrip("/")
    return f"{base}/audio/{os.environ['AUDIO_TOKEN']}/{video_id}.{ext}"


def upload_file(local_path: str, key: str, content_type: str):
    _client().upload_file(local_path, bucket(), key, ExtraArgs={"ContentType": content_type})


def upload_bytes(data: bytes, key: str, content_type: str, cache_control: Optional[str] = None):
    extra = {"CacheControl": cache_control} if cache_control else {}
    _client().put_object(Bucket=bucket(), Key=key, Body=data, ContentType=content_type, **extra)


def delete_key(key: str):
    _client().delete_object(Bucket=bucket(), Key=key)


def exists(key: str) -> bool:
    try:
        _client().head_object(Bucket=bucket(), Key=key)
        return True
    except Exception:
        return False
