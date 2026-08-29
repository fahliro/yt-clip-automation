// worker.js
// WebSub hub callback untuk YouTube channel kamu sendiri.
// Peran:
//   1. Verifikasi hub.challenge (pas subscribe / tiap renew mingguan).
//   2. Terima POST notifikasi berisi <yt:videoId> -> panggil GitHub repository_dispatch.
//   3. Cron Mingguan -> kirim mode=subscribe ulang ke pubsubhubbub.appspot.com.
//
// Semua secret diisi via: wrangler secret put GH_OWNER / GH_REPO / GH_PAT

const HUB = "https://pubsubhubbub.appspot.com/subscribe";

export default {
  // ---- HTTP handler: handshake + notifikasi ----
  async fetch(request, env) {
    const url = new URL(request.url);

    // (1) Verification handshake dari Google (GET)
    if (request.method === "GET") {
      const mode = url.searchParams.get("hub.mode");
      const challenge = url.searchParams.get("hub.challenge");
      const topic = url.searchParams.get("hub.topic");
      const lease = url.searchParams.get("hub.lease_seconds");

      // Google expect balikin challenge persis apa adanya (plain text, 200).
      if (mode && challenge) {
        console.log(`[webhook] verify ${mode} topic=${topic} lease=${lease}`);
        return new Response(challenge, {
          status: 200,
          headers: { "Content-Type": "text/plain" },
        });
      }
      return new Response("ok", { status: 200 });
    }

    // (2) Notifikasi push (POST) berisi Atom feed
    if (request.method === "POST") {
      const body = await request.text();
      const videoId = extractVideoId(body);
      if (videoId) {
        console.log(`[websub] video baru: ${videoId}`);
        await triggerGitHub(videoId, env);
        return new Response("accepted", { status: 202 });
      }
      return new Response("no videoId", { status: 200 });
    }

    return new Response("method not allowed", { status: 405 });
  },

  // ---- Cron Mingguan: renew subscription ----
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(renewSubscription(env));
  },
};

// Parse <yt:videoId> dari Atom XML feed YouTube.
function extractVideoId(xml) {
  const m = xml.match(/<yt:videoId>([^<]+)<\/yt:videoId>/);
  return m ? m[1].trim() : null;
}

// Kirim repository_dispatch ke GitHub Actions -> nyalain workflow clip.yml
async function triggerGitHub(videoId, env) {
  const api = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/dispatches`;
  const res = await fetch(api, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_PAT}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "yt-clip-worker",
    },
    body: JSON.stringify({
      event_type: "video_published",
      client_payload: { video_id: videoId },
    }),
  });
  if (!res.ok) {
    console.error(`[dispatch] gagal: ${res.status} ${await res.text()}`);
  }
}

// Daftar ulang langganan ke Google hub (biar gak kedaluwarsa).
// WORKER_URL = URL publik worker kamu, diisi via: wrangler secret put WORKER_URL
async function renewSubscription(env) {
  const callback = `${env.WORKER_URL}${env.HUB_CALLBACK_PATH || "/webhook"}`;
  const topic = `https://www.youtube.com/xml/feeds/videos.xml?channel_id=${env.CHANNEL_ID}`;
  const params = new URLSearchParams({
    "hub.mode": "subscribe",
    "hub.callback": callback,
    "hub.topic": topic,
    "hub.lease_seconds": "864000", // 10 hari
  });
  const res = await fetch(HUB, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: params.toString(),
  });
  console.log(`[renew] ${res.status} ${await res.text()}`);
}
