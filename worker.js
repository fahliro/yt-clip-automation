// worker.js
// WebSub hub callback untuk YouTube channel kamu sendiri.
// Peran:
//   1. Verifikasi hub.challenge (pas subscribe / tiap renew mingguan).
//   2. Terima POST notifikasi berisi <yt:videoId> -> panggil GitHub repository_dispatch.
//   3. Endpoint /meta-callback untuk Meta (Facebook) OAuth flow - serve HTML
//      page that extracts access_token dari URL fragment.
//   4. Cron Mingguan -> kirim mode=subscribe ulang ke pubsubhubbub.appspot.com.
//
// Semua secret diisi via: wrangler secret put GH_OWNER / GH_REPO / GH_PAT

const HUB = "https://pubsubhubbub.appspot.com/subscribe";

// HTML page for Meta OAuth callback. Client-side JS extracts access_token
// from URL fragment (#access_token=...) and displays it for copy-paste.
const META_CALLBACK_HTML = `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Meta OAuth - Token Captured</title>
<style>body{font-family:monospace;background:#0b0b0b;color:#0f0;padding:24px;line-height:1.5}
.box{background:#1a1a1a;border:1px solid #0f0;padding:16px;border-radius:4px;word-break:break-all;margin:12px 0}
.warn{color:#ff0;font-size:14px}label{display:block;color:#0ff;margin-top:16px;font-weight:bold}
button{background:#0f0;color:#000;padding:8px 16px;border:none;cursor:pointer;font-weight:bold;margin-top:8px}
input{width:100%;background:#000;color:#0f0;border:1px solid #0f0;padding:8px;font-family:monospace}
h1{color:#0f0;margin-top:0}
.err{color:#f55}</style></head>
<body>
<h1>Meta OAuth - Access Token Captured</h1>
<p class="warn">PENTING: Token ini sensitive. Jangan di-share ke siapa pun.</p>
<div id="status">Reading token from URL fragment...</div>
<label>Access Token:</label>
<textarea id="token" rows="6" readonly style="width:100%;background:#000;color:#0f0;border:1px solid #0f0;padding:8px;font-family:monospace"></textarea>
<button onclick="copyTok()">Copy Token</button>
<p class="warn">Cara pakai: copy token di atas, paste ke saya (Hermes) di chat ini.</p>
<p class="err" id="err"></p>
<script>
function copyTok(){const t=document.getElementById('token').value;navigator.clipboard.writeText(t).then(()=>{const s=document.getElementById('status');s.textContent='Copied!';s.style.color='#0f0'}).catch(e=>{document.getElementById('err').textContent='Copy failed: '+e})}
const hash=location.hash.slice(1);
if(!hash){document.getElementById('status').textContent='No token found in URL. Did you approve?';document.getElementById('status').style.color='#f55';document.getElementById('err').textContent='Fragment kosong. Pastikan kamu sudah klik "Continue" di halaman permission Facebook.'}
else{const params=new URLSearchParams(hash);const tok=params.get('access_token');const exp=params.get('expires_in');if(tok){document.getElementById('token').value=tok;const status=document.getElementById('status');status.textContent='Token loaded (expires in '+exp+' seconds / ~'+Math.round(exp/86400)+' days). Copy dan paste ke chat.';status.style.color='#0f0'}else{document.getElementById('status').textContent='No access_token in URL. Check fragment: '+hash.substring(0,200)}}
</script>
</body></html>`;

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

      // Meta OAuth redirect: serve HTML page that displays the access_token
      // from URL fragment (client-side JS extracts it). Lets user copy-paste
      // token without backend storage.
      if (url.pathname === "/meta-redirect" || url.pathname === "/meta-callback") {
        return new Response(META_CALLBACK_HTML, {
          status: 200,
          headers: { "Content-Type": "text/html; charset=utf-8" },
        });
      }

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
