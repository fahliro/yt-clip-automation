# yt-clip-automation (FREE STACK)

Otomatisasi: upload raw (PRIVATE) ke YouTube channel kamu sendiri -> WebSub trigger
-> Cloudflare Worker -> GitHub Actions -> yt-dlp download -> Groq Whisper -> LLM pilih
segmen -> ffmpeg clip 9:16 -> upload Shorts (PUBLIC). Tanpa biaya (free tier).

## Komponen
- **Cloudflare Worker** (`worker.js`): WebSub handshake + forward videoId ke GitHub,
  renew subscription tiap Minggu.
- **GitHub Actions** (`clip.yml`): jalanin `clip.py`. Public repo = unlimited minutes.
- **clip.py**: orchestrator (download -> whisper -> LLM -> ffmpeg -> upload).

## Secret yang harus diisi
### Cloudflare Worker (wrangler secret put)
- `GH_OWNER`, `GH_REPO`, `GH_PAT` (PAT dengan repo:write)
- `WORKER_URL` (URL worker, mis https://yt-clip-webhook.xxx.workers.dev)
- `CHANNEL_ID` (bisa di wrangler.toml [vars], bukan rahasia)
- `HUB_CALLBACK_PATH` = /webhook

### GitHub Actions Secrets
- `YT_CHANNEL_ID`            - ID channel kamu
- `YT_COOKIES_TXT`           - Netscape cookies (buat DOWNLOAD video private) - lihat bawah
- `YTDLP_JS_RUNTIME`         - isi "node" (wajib solve signature challenge YT)
- `YT_UPLOAD_CLIENT`         - Google Cloud OAuth client_id (upload API)
- `YT_UPLOAD_SECRET`         - client_secret
- `YT_UPLOAD_TOKEN`          - refresh_token
- `GROQ_API_KEY`             - Whisper transkrip (gratis)
- `LLM_API_KEY`              - key LLM kamu (hook + virality)
- `LLM_BASE_URL`             - mis https://api.openai.com/v1
- `LLM_MODEL`                - mis gpt-4o-mini

## Generate YT_COOKIES_TXT (download, cara cookies)
Cookies = login akun PEMILIK channel (wajib buat video private).
```
# di laptop kamu (atau di mesin ini): pakai Camoufox/Playwright export cookies.json
# lalu convert ke Netscape (script sudah disediakan):
python convert_cookies.py /path/yt_cookies.json yt_cookies.txt
# isi yt_cookies.txt -> GitHub Secret YT_COOKIES_TXT
```
Catatan: cookies expire (mingguan). Kalau download gagal "confirm you're not a bot"
-> export ulang cookies.json dari browser login, convert, update secret.

yt-dlp butuh node + remote component buat solve signature challenge YouTube:
flag `--js-runtimes node --remote-components ejs:github` (sudah di clip.py).
Di runner ubuntu, `node` sudah ada; set secret `YTDLP_JS_RUNTIME=node`.

## Generate YT Upload OAuth (API)
1. Google Cloud Console -> enable YouTube Data API v3.
2. OAuth consent screen -> publish.
3. Credentials -> OAuth client (Desktop).
4. Pakai script exchange code -> refresh_token (simpan 3 nilai ke secret).

## Deploy Worker
```
npm i -g wrangler
wrangler login
wrangler secret put GH_OWNER ...   # untuk tiap secret
wrangler deploy
# subscribe manual pertama kali:
curl -X POST https://pubsubhubbub.appspot.com/subscribe \
  -d hub.mode=subscribe -d hub.callback=https://<worker>/webhook \
  -d hub.topic=https://www.youtube.com/xml/feeds/videos.xml?channel_id=<CHANNEL_ID> \
  -d hub.lease_seconds=864000
```

## Catatan
- Cap upload 100/hari (videos.insert). channel baru/belum verified bisa lebih ketat.
- yt-dlp di-PIN (requirements.txt). Kalau YouTube break -> update versi, commit.
- Raw yang didownload sudah di-re-encode YouTube -> kualitas 2 generasi di bawah master.
- state.json cegah double-clip kalau WebSub kirim notifikasi ganda.
- ffmpeg: resize 9:16 = fit-width + blur background (screen tutorial, no face).
  silence/filler removal + burned caption = pass 2 (TODO di clip.py).
