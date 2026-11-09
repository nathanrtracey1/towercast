// TowerCast gateway: a Cloudflare Worker in front of the R2 bucket.
//
//   /feed.xml and /            -> HTTP Basic Auth (FEED_USER / FEED_PASS)
//   /audio/<AUDIO_TOKEN>/<id>  -> secret token in the path (podcast apps don't
//                                 reliably send credentials for episode audio)
//
// On a cron schedule it also watches the channel's recent uploads and starts
// the GitHub workflow only when a video TowerCast hasn't handled shows up.

const AUDIO_NAME = /^[A-Za-z0-9_-]{11}\.(m4a|mp3)$/;

// ── Auth helpers ─────────────────────────────────────────────────────────

async function safeEqual(a, b) {
  const enc = new TextEncoder();
  const [ha, hb] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a || "")),
    crypto.subtle.digest("SHA-256", enc.encode(b || "")),
  ]);
  return crypto.subtle.timingSafeEqual(ha, hb);
}

async function hasBasicAuth(request, env) {
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Basic ")) return false;
  let decoded;
  try {
    decoded = atob(header.slice(6));
  } catch {
    return false;
  }
  const i = decoded.indexOf(":");
  if (i < 0) return false;
  const userOk = await safeEqual(decoded.slice(0, i), env.FEED_USER);
  const passOk = await safeEqual(decoded.slice(i + 1), env.FEED_PASS);
  return userOk && passOk;
}

function unauthorized() {
  return new Response("Unauthorized", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="TowerCast"' },
  });
}

// ── R2 file serving (with byte ranges so Overcast can stream and seek) ───

async function serveObject(request, env, key, extraHeaders) {
  if (request.method === "HEAD") {
    const head = await env.BUCKET.head(key);
    if (!head) return new Response(null, { status: 404 });
    const headers = new Headers(extraHeaders);
    head.writeHttpMetadata(headers);
    headers.set("etag", head.httpEtag);
    headers.set("accept-ranges", "bytes");
    headers.set("content-length", String(head.size));
    return new Response(null, { headers });
  }

  const object = await env.BUCKET.get(key, { range: request.headers, onlyIf: request.headers });
  if (object === null) return new Response("Not found", { status: 404 });

  const headers = new Headers(extraHeaders);
  object.writeHttpMetadata(headers);
  headers.set("etag", object.httpEtag);
  headers.set("accept-ranges", "bytes");
  if (!object.body) return new Response(null, { status: 304, headers });

  const r = object.range;
  if (request.headers.has("range") && r) {
    let start, end;
    if ("suffix" in r) {
      start = object.size - r.suffix;
      end = object.size - 1;
    } else {
      start = r.offset ?? 0;
      end = r.length !== undefined ? start + r.length - 1 : object.size - 1;
    }
    headers.set("content-range", `bytes ${start}-${end}/${object.size}`);
    headers.set("content-length", String(end - start + 1));
    return new Response(object.body, { status: 206, headers });
  }

  headers.set("content-length", String(object.size));
  return new Response(object.body, { headers });
}

// ── New-video watcher ────────────────────────────────────────────────────

async function readJson(env, key) {
  const obj = await env.BUCKET.get(key);
  if (!obj) return null;
  try {
    return await obj.json();
  } catch {
    return null;
  }
}

async function dispatchWorkflow(env) {
  const res = await fetch(
    `https://api.github.com/repos/${env.GH_REPO}/actions/workflows/towercast.yml/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "TowerCast-Worker",
      },
      body: JSON.stringify({ ref: "main", inputs: { action: "sync", scan_limit: "15" } }),
    },
  );
  if (!res.ok) console.log(`Dispatch failed: ${res.status} ${await res.text()}`);
  return res.ok;
}

// YouTube's RSS has gone 404 for hours at a time since late 2025, sometimes
// for one channel and not others. Try the channel feed, then the uploads
// playlist feed, then (if YT_API_KEY is set) the Data API. Returns null if
// every source failed.
async function fetchRecentIds(env) {
  const uploads = "UU" + env.YT_CHANNEL_ID.slice(2);
  for (const q of [`channel_id=${env.YT_CHANNEL_ID}`, `playlist_id=${uploads}`]) {
    const res = await fetch(`https://www.youtube.com/feeds/videos.xml?${q}`, { cf: { cacheTtl: 0 } });
    if (!res.ok) {
      console.log(`YouTube RSS (${q.split("=")[0]}) returned ${res.status}`);
      continue;
    }
    const xml = await res.text();
    // Shorts are skipped here so they never wake the workflow
    const ids = [];
    for (const entry of xml.split("<entry>").slice(1)) {
      const m = entry.match(/<yt:videoId>([A-Za-z0-9_-]{11})<\/yt:videoId>/);
      if (m && !entry.includes("/shorts/")) ids.push(m[1]);
    }
    return ids;
  }

  if (env.YT_API_KEY) {
    // 1 quota unit per call; the free daily quota is 10,000
    const res = await fetch(
      `https://www.googleapis.com/youtube/v3/playlistItems?part=contentDetails&maxResults=15&playlistId=${uploads}&key=${env.YT_API_KEY}`,
    );
    if (res.ok) {
      const data = await res.json();
      // The API can't tell Shorts apart; the workflow skips them and marks them known
      return (data.items || []).map((i) => i.contentDetails.videoId);
    }
    console.log(`YouTube Data API returned ${res.status}`);
  }
  return null;
}

// With no way to see the channel, let the workflow's own yt-dlp scan do the
// detection, at most once an hour.
async function blindDispatch(env) {
  const last = (await readJson(env, "state/blind_dispatch.json")) || {};
  if (Date.now() - (last.at || 0) < 60 * 60_000) return;
  console.log("All YouTube sources failed; running the workflow's own scan");
  if (await dispatchWorkflow(env)) {
    await env.BUCKET.put("state/blind_dispatch.json", JSON.stringify({ at: Date.now() }));
  }
}

async function checkForNewVideos(env) {
  const ids = await fetchRecentIds(env);
  if (ids === null) return blindDispatch(env);

  const known = new Set((await readJson(env, "state/known_ids.json"))?.ids || []);
  const fresh = ids.filter((id) => !known.has(id)).sort();
  if (fresh.length === 0) return;

  // Same unhandled set as last time (e.g. an upcoming live stream or a failed
  // download): retry every 30 min for the first few tries, then every 2 hours.
  const last = (await readJson(env, "state/last_dispatch.json")) || {};
  const sameSet = JSON.stringify(last.ids) === JSON.stringify(fresh);
  const attempts = sameSet ? last.attempts || 0 : 0;
  const waitMs = (attempts < 6 ? 30 : 120) * 60_000;
  if (sameSet && Date.now() - (last.at || 0) < waitMs) return;

  console.log(`New/unhandled videos: ${fresh.join(", ")} (attempt ${attempts + 1})`);
  if (await dispatchWorkflow(env)) {
    await env.BUCKET.put(
      "state/last_dispatch.json",
      JSON.stringify({ ids: fresh, at: Date.now(), attempts: attempts + 1 }),
    );
  }
}

// ── Entry points ─────────────────────────────────────────────────────────

export default {
  async fetch(request, env) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    if (parts[0] === "audio" && parts.length === 3) {
      const tokenOk = await safeEqual(parts[1], env.AUDIO_TOKEN);
      if (!tokenOk || !AUDIO_NAME.test(parts[2])) return new Response("Not found", { status: 404 });
      return serveObject(request, env, `audio/${parts[2]}`, { "cache-control": "private, max-age=86400" });
    }

    if (url.pathname === "/feed.xml" || url.pathname === "/") {
      if (!(await hasBasicAuth(request, env))) return unauthorized();
      const key = url.pathname === "/" ? "index.html" : "feed.xml";
      return serveObject(request, env, key, { "cache-control": "no-cache", "x-robots-tag": "noindex" });
    }

    return new Response("Not found", { status: 404 });
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(checkForNewVideos(env));
  },
};
