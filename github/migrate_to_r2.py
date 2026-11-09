#!/usr/bin/env python3
"""
One-time migration of existing episodes from GitHub Release assets to R2.

  Actions → TowerCast Sync → Run workflow → action = migrate_r2
      Copies every release's audio file into R2 (safe to re-run).

  Actions → TowerCast Sync → Run workflow → action = purge_release_audio
      Deletes the public audio files from Releases, but only for episodes
      R2 already has. Run this after confirming the new feed plays in Overcast.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from github.github_api import GitHubAPI
from github.feed_builder import video_id_from_tag
from github import r2


def main():
    purge = "--purge" in sys.argv
    repo = os.environ["GITHUB_REPOSITORY"]
    api = GitHubAPI(token=os.environ["GITHUB_TOKEN"], repo=repo)
    moved = skipped = purged = 0

    for rel in api.list_all_releases():
        vid = video_id_from_tag(rel.get("tag_name", ""))
        assets = [a for a in rel.get("assets", []) if a.get("name", "").endswith((".m4a", ".mp3"))]
        if not vid or not assets:
            continue

        asset = assets[0]
        name = asset["name"]
        ext = name.rsplit(".", 1)[1]
        key = r2.audio_key(vid, ext)

        if purge:
            # Only delete the public copy once R2 definitely has it
            if r2.exists(key):
                api.delete_asset(asset["id"])
                purged += 1
                print(f"  🗑 removed public copy of {vid}")
            else:
                print(f"  ⚠ {vid} is not in R2 yet; leaving its release audio in place")
            continue

        if r2.exists(key):
            skipped += 1
            continue

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                ["gh", "release", "download", rel["tag_name"], "--repo", repo, "--pattern", name, "--dir", tmp],
                check=True,
            )
            r2.upload_file(os.path.join(tmp, name), key, "audio/mp4" if ext == "m4a" else "audio/mpeg")
        moved += 1
        print(f"  ✓ {vid} → R2")

    if purge:
        print(f"Removed {purged} public audio file(s) from Releases.")
    else:
        print(f"Migrated {moved} episode(s); {skipped} already in R2.")


if __name__ == "__main__":
    main()
