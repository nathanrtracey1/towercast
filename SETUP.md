# TowerCast: private feed + fast new-video watcher

What changes:
- Audio lives in Cloudflare R2 behind a Worker. The feed and dashboard need a password; audio URLs carry a secret token.
- The Worker checks YouTube's RSS every 10 minutes and starts the GitHub workflow only when there's something new. GitHub's own cron is now just a 3-hourly safety net.
- GitHub Releases still hold each episode's metadata (titles, descriptions, chapters), but no audio.
- Only the repo owner's issues can trigger the workflow.
- Shorts skipped, live streams + videos pulled, rolling feed of 50, and dashboard "Add URL" all work as before.

## Where each file goes

| Downloaded file      | Put it at                          | Status  |
|----------------------|------------------------------------|---------|
| index.js             | worker/src/index.js                | new     |
| wrangler.toml        | worker/wrangler.toml               | new     |
| r2.py                | github/r2.py                       | new     |
| migrate_to_r2.py     | github/migrate_to_r2.py            | new     |
| publish.py           | github/publish.py                  | new     |
| feed_builder.py      | github/feed_builder.py             | updated |
| github_api.py        | github/github_api.py               | updated |
| feed_generator.py    | engine/feed_generator.py           | updated |
| config.json          | config.json                        | updated |
| towercast.yml        | .github/workflows/towercast.yml    | updated |
| tower_cast.py        | tower_cast.py                      | updated |
| web.py               | server/web.py                      | updated |

From your repo folder, with everything in ~/Downloads:

```bash
D=~/Downloads
mkdir -p worker/src
mv $D/index.js worker/src/
mv $D/wrangler.toml worker/
mv $D/r2.py $D/migrate_to_r2.py $D/publish.py $D/feed_builder.py $D/github_api.py github/
mv $D/feed_generator.py engine/
mv $D/config.json $D/tower_cast.py .
mv $D/towercast.yml .github/workflows/
mv $D/web.py server/

# Dashboard: treat the Worker address as "cloud mode" (one-line edit)
sed -i '' "s/includes('github.io');/includes('github.io') || window.location.hostname.includes('workers.dev');/" server/templates/index.html
```

## Setup, in this order

1. **Cloudflare R2**: create a bucket named `towercast`, then an R2 API token with Object Read & Write on it. Note the Account ID, Access Key ID, and Secret Access Key.
2. **Secrets**: generate three values with `openssl rand -hex 24` (username, password, audio token).
3. **GitHub token for the Worker**: Settings → Developer settings → Fine-grained tokens. Only the `towercast` repo, **Actions: Read and write**. Note its expiry date.
4. **Worker**: in `worker/wrangler.toml`, set `YT_CHANNEL_ID`:
   `yt-dlp --print channel_id --playlist-items 1 "https://www.youtube.com/@TheDiceTower/videos"`
   Then from `worker/`:
   ```bash
   npx wrangler deploy
   npx wrangler secret put FEED_USER
   npx wrangler secret put FEED_PASS
   npx wrangler secret put AUDIO_TOKEN
   npx wrangler secret put GH_DISPATCH_TOKEN
   ```
   Note the URL it prints: `https://towercast.<you>.workers.dev`.
5. **GitHub repo secrets** (Settings → Secrets and variables → Actions): `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `FEED_BASE_URL` (the Worker URL), `AUDIO_TOKEN`, `FEED_USER`, `FEED_PASS`. Keep `YOUTUBE_COOKIES`.
6. **Commit and push** the files above.
7. **Copy existing audio**: Actions → TowerCast Sync → Run workflow → action `migrate_r2`.
8. **Build the new feed**: Run workflow → action `sync`.
9. **Test** from your Mac:
   ```bash
   curl -u USER:PASS https://towercast.<you>.workers.dev/feed.xml | head    # feed XML
   curl -I https://towercast.<you>.workers.dev/feed.xml                     # 401
   curl -I -r 0-99 "<an enclosure URL from the feed>"                       # 206
   ```
10. **Overcast** (on one device; it syncs): + → Add URL → `https://USER:PASS@towercast.<you>.workers.dev/feed.xml`. Check that an episode plays and seeks, then unsubscribe from the old github.io feed.
11. **Dashboard**: open `https://towercast.<you>.workers.dev/` on each device and log in. Re-enter your 1-tap GitHub token, since saved tokens belong to the old site.
12. **Remove the public copies**: Run workflow → action `purge_release_audio`. Then Settings → Pages → turn Pages off.

`npx wrangler tail` shows the watcher's checks live.

## Notes
- The repo can stay public. Releases only contain public YouTube metadata, secrets are masked in logs, and public repos get unlimited free Actions minutes.
- Upcoming live streams show in YouTube's RSS before they can be downloaded. The Worker retries those every 30 min, then every 2 hours, until they're in the feed.
- The Mac `publish` command and the local dashboard need the same R2_*, FEED_BASE_URL and AUDIO_TOKEN environment variables. Without them, they skip cloud publishing and leave it to GitHub Actions.
- If the GitHub token expires, the 3-hourly fallback keeps the feed running until you replace it.
