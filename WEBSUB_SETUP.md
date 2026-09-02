# WEBSUB SETUP GUIDE
# Panduan deploy Cloudflare Worker untuk auto-trigger YouTube Shorts
# 
# Dibuat: 2026-09-02
# Repo: fahliro/yt-clip-automation

---

## PRASYARAT

1. **Node.js** (>= 18) — download dari https://nodejs.org (LTS)
2. **Git** — sudah ada di laptop kamu
3. **Akun Cloudflare** — daftar gratis di https://dash.cloudflare.com/sign-up
4. **GitHub PAT** — sudah punya (yang barusan kamu kasih ke Hermes)
5. **YouTube Channel ID** — bisa dilihat di YouTube Studio > Settings > Channel > Basic Info

---

## LANGKAH 1: Install wrangler

Node.js SUDAH terinstall di laptop (v24.18.0 di `C:\Program Files\nodejs\`). Tinggal install wrangler:

Buka PowerShell atau Git Bash, jalankan:

```bash
# Tambah Node.js ke PATH
export PATH="/c/Program Files/nodejs:/c/Users/Robby/AppData/Roaming/npm:$PATH"

# Install wrangler globally
npm install -g wrangler

# Verify (pakai full path kalau 'wrangler' gak dikenali)
wrangler.cmd --version
# atau: /c/Users/Robby/AppData/Roaming/npm/wrangler.cmd --version
# expect: 4.128.0 (atau lebih baru)
```

Kalau `node` atau `npm` masih belum dikenali:
```bash
node --version   # harusnya v24.18.0
npm --version    # harusnya 11.16.0
```
Kalau error "stdin is not a tty" di Git Bash, pakai PowerShell langsung.

---

## LANGKAH 2: Login ke Cloudflare

```bash
cd /c/Users/Robby/yt-clip-automation
wrangler login
```

Browser akan terbuka. Klik **"Allow"** untuk otorisasi wrangler ke akun Cloudflare kamu.

---

## LANGKAH 3: Edit wrangler.toml (CHANNEL_ID)

Buka file `wrangler.toml` di repo, cari baris:

```
CHANNEL_ID = "GANTI_DENGAN_CHANNEL_ID_KAMU"
```

Ganti dengan channel ID kamu, contoh:

```
CHANNEL_ID = "UCxxxxxxxxxxxxxxxxxxxxxx"
```

**Cari Channel ID:**
1. Buka https://studio.youtube.com
2. Klik Settings (gear icon) > Channel > Basic Info
3. Copy "Channel ID" (format: UC...)

Setelah edit, **commit & push**:

```bash
git add wrangler.toml
git commit -m "config: set CHANNEL_ID"
git push
```

---

## LANGKAH 4: Set Secrets di Worker

Secrets ini TIDAK akan ter-commit (aman). Jalankan satu per satu:

```bash
wrangler secret put GH_OWNER
# Ketik: fahliro (Enter)

wrangler secret put GH_REPO
# Ketik: yt-clip-automation (Enter)

wrangler secret put GH_PAT
# Paste PAT kamu (yang barusan, scope 'actions:write' atau classic 'repo')
# Ctrl+V lalu Enter
```

Verify (lihat list secret, bukan value):

```bash
wrangler secret list
```

Output harusnya:
```
🌀 Creating the secrets for the Worker "yt-clip-webhook"
- GH_OWNER: private data
- GH_REPO: private data
- GH_PAT: private data
```

---

## LANGKAH 5: Deploy Worker

```bash
wrangler deploy
```

Output:
```
Published yt-clip-webhook (X.XX sec)
  https://yt-clip-webhook.<subdomain>.workers.dev
  Current Version ID: xxxxxxxx
```

**CATAT URL** itu (mis. `https://yt-clip-webhook.abc123.workers.dev`).

---

## LANGKAH 6: Test Worker Hidup

```bash
curl -i https://yt-clip-webhook.<subdomain>.workers.dev/
```

Expect: `HTTP 200`, body: `ok`

Kalau 404/500, cek:
- `wrangler deploy` sukses?
- URL benar?

---

## LANGKAH 7: Subscribe ke YouTube PubSubHubbub

Ini **sekali saja** (lease 10 hari). Worker auto-renew tiap Minggu via cron.

```bash
curl -X POST https://pubsubhubbub.appspot.com/subscribe \
  -d "hub.mode=subscribe" \
  -d "hub.callback=https://yt-clip-webhook.<subdomain>.workers.dev/webhook" \
  -d "hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=<CHANNEL_ID>" \
  -d "hub.lease_seconds=864000"
```

Ganti:
- `<subdomain>` = subdomain dari URL wrangler deploy
- `<CHANNEL_ID>` = channel ID kamu (format: UC...)

Expect: `HTTP 204` (atau `202` dengan body `hub.mode=subscribe`)

---

## LANGKAH 8: Verifikasi End-to-End

1. Buka **YouTube Studio** > **Content**
2. **Upload 1 video PRIVATE** (penting: PRIVATE, biar tidak publish ke publik)
3. Tunggu **10-30 detik** (PubSubHubbub push latency)
4. Buka **GitHub** > repo `yt-clip-automation` > **Actions**
5. Cek: run baru dengan event `repository_dispatch` harusnya muncul
6. Tunggu pipeline selesai (5-10 menit)
7. Cek channel kamu: video Shorts baru harusnya **PUBLIC**

---

## TROUBLESHOOTING

### Q: Run baru TIDAK muncul di GitHub Actions?
**A:** Cek Worker logs:
1. Buka https://dash.cloudflare.com
2. Klik **Workers & Pages** > **yt-clip-webhook**
3. Klik **Logs** > **Start log streaming**
4. Upload video PRIVATE lagi
5. Lihat apakah ada log `[webhook] verify` atau `[websub] video baru`

Kalau tidak ada log:
- Subscription gak aktif → ulangi LANGKAH 7
- CHANNEL_ID salah → cek lagi di YouTube Studio

### Q: Worker log show "GH_PAT invalid"?
**A:** PAT di Worker salah/expired. Update:
```bash
wrangler secret put GH_PAT
# Paste PAT baru
```

### Q: Pipeline jalan tapi video tidak muncul di channel?
**A:** Cek secrets GitHub:
- `YT_UPLOAD_TOKEN` valid?
- `YT_UPLOAD_CLIENT` & `YT_UPLOAD_SECRET` benar?
- Cek log run di Actions untuk error upload

### Q: WebSub tidak fire setelah 10 hari?
**A:** Subscription expired. Worker auto-renew via cron Minggu, tapi kalau gagal:
```bash
# Re-subscribe manual
curl -X POST https://pubsubhubbub.appspot.com/subscribe \
  -d "hub.mode=subscribe" \
  -d "hub.callback=https://<URL>/webhook" \
  -d "hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=<ID>" \
  -d "hub.lease_seconds=864000"
```

---

## BACKUP: Cron Fallback

Workflow `clip.yml` punya fallback `schedule: "0 * * * *"`:
- Poll channel tiap jam
- Detect video terbaru via `YT_READ_TOKEN`
- Trigger clip.py dengan video_id terbaru

Walaupun WebSub gak jalan, ini backup. Tapi latency bisa **1 jam** vs WebSub yang **<30 detik**.

---

## SELESAI!

Setelah setup selesai, setiap kali kamu upload video ke channel:
1. YouTube push notifikasi ke PubSubHubbub
2. PubSubHubbub forward ke Cloudflare Worker
3. Worker panggil GitHub `repository_dispatch`
4. GitHub Actions jalan → download → whisper → LLM → ffmpeg → upload Shorts
5. Shorts muncul di channel kamu (PUBLIC)

**Fully automated.** 🎉
