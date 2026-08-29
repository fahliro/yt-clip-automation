#!/usr/bin/env python3
"""
clip.py - Orchestrator clipping otomatis (free stack).

Flow:
  1. Download raw (PRIVATE) dari YouTube channel sendiri via yt-dlp (OAuth, pin versi).
  2. Transkrip audio -> teks + timestamp kata via Groq Whisper API.
  3. LLM (key kamu) baca teks -> pilih segmen + virality score + DAFTAR FILLER per video.
  4. ffmpeg:
       - resize 9:16 (fit-width + blur bg, screen tutorial tanpa wajah)
       - buang filler-word (pakai list dari LLM, bukan hardcode)
       - buang silence (gap antar kata > SILENCE_GAP)
       - burn caption (ASS dari timestamp whisper)
  5. Upload tiap klip ke YouTube (PUBLIC) via YouTube Data API (OAuth upload).

ENV wajib (GitHub Secrets / Actions variables):
  VIDEO_ID, YT_CHANNEL_ID, YT_DL_OAUTH_JSON, YT_UPLOAD_CLIENT, YT_UPLOAD_SECRET,
  YT_UPLOAD_TOKEN, GROQ_API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, STATE_FILE
"""
import os, json, subprocess, sys, tempfile, re, pathlib

WORKDIR = pathlib.Path(tempfile.gettempdir()) / "yt_clip"
WORKDIR.mkdir(parents=True, exist_ok=True)

SILENCE_GAP = 0.4      # detik; gap antar kata > ini = dianggap silence, dibuang
DEFAULT_FILLERS = ["um", "yah", "gitu", "eh", "ya", "wah", "nih", "kan", "loh"]
MAX_CLIPS = 100        # cap videos.insert per hari

def log(m): print(f"[clip] {m}", flush=True)

def run(cmd):
    log(" ".join(str(c) for c in cmd[:3]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"ERROR: {r.stderr[-2000:]}")
        raise SystemExit(f"command failed: {cmd[0]}")
    return r.stdout


# ---------------------------------------------------------------- 1. DOWNLOAD
def download_raw(video_id):
    out = WORKDIR / f"{video_id}.mp4"
    cookies = WORKDIR / "cookies.txt"
    # Secret disimpan sbg BASE64 (1 baris) biar gak rusak saat di-copy dari Notepad
    # (Notepad sering ubah TAB jadi spasi / CRLF jadi aneh). Decode -> tulis LF murni.
    import base64
    b64 = os.environ["YT_COOKIES_TXT"].strip()
    try:
        raw = base64.b64decode(b64).decode("utf-8")
    except Exception:
        # fallback: anggap sudah plaintext (strip CR saja)
        raw = os.environ["YT_COOKIES_TXT"].replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cookies.write_text(raw, newline="\n", encoding="utf-8")
    # Pin versi (requirements.txt). Cookies = login akun pemilik channel (video private).
    # node + remote component wajib buat solve YouTube signature challenge (2024+).
    cmd = [
        "yt-dlp", "--cookies", str(cookies),
        "--js-runtimes", os.environ.get("YTDLP_JS_RUNTIME", "node"),
        "--remote-components", "ejs:github",
        "-f", "best[height<=1080]", "-o", str(out),
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    run(cmd)
    if not out.exists():
        raise SystemExit("download gagal")
    log(f"raw downloaded: {out}")
    return out


# ---------------------------------------------------------------- 2. WHISPER
def transcribe(path):
    import requests
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    with open(path, "rb") as f:
        r = requests.post(url, headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
                          files={"file": f},
                          data={"model": "whisper-large-v3", "response_format": "verbose_json",
                                "timestamp_granularities": "word"})
        r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- 3. LLM PILIH
def pick_segments(transcript):
    import requests
    words = transcript.get("words", [])
    if not words:
        # fallback: potong tiap 45 detik (pakai duration kalau ada, else 0)
        dur = float(transcript.get("duration") or 0)
        if dur <= 0:
            # tanpa words & tanpa duration: anggap 1 segmen penuh 0..0 (caller skip)
            return [{"start": 0, "end": 0, "score": 5,
                     "reason": "fallback-no-words", "fillers": DEFAULT_FILLERS}]
        return [{"start": i, "end": min(i + 45, dur), "score": 5,
                 "reason": "fallback", "fillers": DEFAULT_FILLERS}
                for i in range(0, int(dur), 45)]

    chunks, t, buf = [], 0.0, []
    for w in words:
        buf.append(w["word"])
        if w["end"] - t >= 5:
            chunks.append(f"[{t:.0f}s] " + " ".join(buf))
            t, buf = w["end"], []
    transcript_for_llm = (
        "Kamu editor video short. Dari transkrip ber-timestamp berikut, pilih 3-8 segmen "
        "menarik untuk YouTube Shorts (30-60 detik). Untuk TIAP segmen berikan: "
        "start (detik), end (detik), score virality (1-10), reason singkat, dan "
        "fillers = array kata pengisi/pembuka tidak penting dalam segmen itu "
        "(mis: 'yah','gitu','eh','ya'). Jawab HANYA JSON array: "
        "[{\"start\":float,\"end\":float,\"score\":int,\"reason\":str,\"fillers\":[str]}].\n"
        + "\n".join(chunks)[:14000]
    )

    r = requests.post(
        f"{os.environ['LLM_BASE_URL']}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": os.environ["LLM_MODEL"], "messages": [
            {"role": "system", "content": "Output JSON saja tanpa markdown."},
            {"role": "user", "content": transcript_for_llm}]},
        timeout=120,
    )
    r.raise_for_status()
    content = re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip().strip("`")
    try:
        segs = json.loads(content)
    except Exception:
        log(f"LLM gagal parse: {content[:400]}")
        segs = [{"start": i, "end": min(i+45, words[-1]["end"]), "score": 5,
                 "reason": "fallback", "fillers": DEFAULT_FILLERS}
                for i in range(0, int(words[-1]["end"]), 45)]
    for s in segs:
        s.setdefault("fillers", DEFAULT_FILLERS)
    segs.sort(key=lambda s: s.get("score", 0), reverse=True)
    log(f"LLM pilih {len(segs)} segmen")
    return segs


# ---------------------------------------------------------------- 4a. CAPTION
def build_ass(words, out_path):
    # warna: &H00FFFFFF = putih; outline &H00000000 = hitam; Bold=1; Align=2 bawah-tengah
    style = ("Style: Default,Arial,48,&H00FFFFFF,&H000000FF,&H00000000,1,0,0,0,100,100,0,0,2,20,20,20,1")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        style, "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for w in words:
        s = fmt_time(w["start"]); e = fmt_time(w["end"])
        txt = w["word"].strip().replace(",", " ")
        lines.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{txt}")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def fmt_time(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


# ---------------------------------------------------------------- 4b. CLIP
def clip_segment(raw_path, seg, all_words, idx):
    start, end = float(seg["start"]), float(seg["end"])
    fillers = set(w.lower() for w in seg.get("fillers", DEFAULT_FILLERS))
    seg_words = [w for w in all_words if start <= w["start"] < end]
    kept = [w for w in seg_words if w["word"].strip().lower() not in fillers]

    # gabungkan kata jadi "keep spans"; gap > SILENCE_GAP = silence dibuang
    spans, cur = [], None
    for w in kept:
        if cur is None:
            cur = [w["start"], w["end"]]
        elif w["start"] - cur[1] <= SILENCE_GAP:
            cur[1] = w["end"]
        else:
            spans.append(cur); cur = [w["start"], w["end"]]
    if cur:
        spans.append(cur)
    if not spans:                      # semua filler -> pakai segmen utuh
        spans = [[start, end]]
        seg_words = [w for w in all_words if start <= w["start"] < end]

    parts = []
    for i, (s, e) in enumerate(spans):
        span_words = [w for w in seg_words if s <= w["start"] < e]
        parts.append(cut_span(raw_path, s, e, span_words, idx, i))
    final = concat_parts(parts, idx)
    return final


def cut_span(raw_path, s, e, words, idx, i):
    dur = max(0.5, e - s)
    out = WORKDIR / f"part_{idx:02d}_{i:02d}.mp4"
    ass = WORKDIR / f"cap_{idx:02d}_{i:02d}.ass"
    if words:
        build_ass(words, ass)
        sub = f",subtitles={ass}"
    else:
        sub = ""
    # 9:16 fit-width + blur background (screen tutorial, tanpa wajah)
    vf = (
        f"[0:v]scale=1080:-1,boxblur=20[bg];"
        f"[0:v]scale=1080:-1[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{sub},format=yuv420p"
    )
    run(["ffmpeg", "-y", "-ss", f"{s:.2f}", "-i", str(raw_path), "-t", f"{dur:.2f}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", str(out)])
    return out


def concat_parts(parts, idx):
    final = WORKDIR / f"clip_{idx:02d}.mp4"
    if len(parts) == 1:
        parts[0].rename(final)
        return final
    lst = WORKDIR / f"concat_{idx:02d}.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", str(final)])
    return final


# ---------------------------------------------------------------- 5. UPLOAD
def upload_video(path, title, description):
    import requests
    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_UPLOAD_CLIENT"],
        "client_secret": os.environ["YT_UPLOAD_SECRET"],
        "refresh_token": os.environ["YT_UPLOAD_TOKEN"],
        "grant_type": "refresh_token",
    }).json()
    access = tok["access_token"]
    meta = json.dumps({
        "snippet": {"title": title, "description": description,
                    "channelId": os.environ["YT_CHANNEL_ID"]},
        "status": {"privacyStatus": "public"},
    }).encode()
    with open(path, "rb") as f:
        r = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos?part=snippet,status&uploadType=multipart",
            headers={"Authorization": f"Bearer {access}"},
            files={"metadata": ("meta", meta, "application/json; charset=UTF-8"),
                   "media": (path.name, f, "video/*")},
            timeout=600,
        )
        r.raise_for_status()
    vid = r.json()["id"]
    log(f"uploaded: https://youtu.be/{vid}")
    return vid


# ---------------------------------------------------------------- STATE
def already_done(video_id):
    sf = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
    if not sf.exists():
        return False
    return video_id in set(json.loads(sf.read_text()).get("done", []))


def mark_done(video_id):
    sf = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
    data = json.loads(sf.read_text()) if sf.exists() else {"done": []}
    data["done"].append(video_id)
    sf.write_text(json.dumps(data))


# ---------------------------------------------------------------- POLL (cron fallback)
def poll_latest():
    """Cek video terbaru di channel via videos.list (butuh YT_READ_TOKEN,
    scope youtube.readonly). Return video_id pertama, atau None."""
    import requests
    tok = os.environ.get("YT_READ_TOKEN")
    if not tok:
        log("[poll] YT_READ_TOKEN kosong -> skip (pakai WebSub atau manual input)")
        return None
    # dapat access token dari refresh token (pakai client upload yg sama)
    t = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_UPLOAD_CLIENT"],
        "client_secret": os.environ["YT_UPLOAD_SECRET"],
        "refresh_token": tok,
        "grant_type": "refresh_token",
    }).json()
    access = t.get("access_token")
    if not access:
        log("[poll] gagal token baca"); return None
    r = requests.get("https://www.googleapis.com/youtube/v3/search",
                     params={"channelId": os.environ["YT_CHANNEL_ID"],
                             "part": "id", "order": "date", "maxResults": 1,
                             "type": "video"},
                     headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        return None
    return items[0]["id"]["videoId"]


# ---------------------------------------------------------------- MAIN
def main():
    video_id = os.environ.get("VIDEO_ID")
    if not video_id:
        # cron fallback: cek video terbaru
        video_id = poll_latest()
    if not video_id:
        log("VIDEO_ID kosong (gak ada WebSub/manual/poll) -> keluar bersih")
        return
    if already_done(video_id):
        log("sudah di-clip, skip"); return
    raw = download_raw(video_id)
    tr = transcribe(raw)
    words = tr.get("words", [])
    segs = pick_segments(tr)
    for i, seg in enumerate(segs[:MAX_CLIPS]):
        clip = clip_segment(raw, seg, words, i)
        title = f"Clip #{i+1} - score {seg.get('score','?')}"
        desc = seg.get("reason", "")
        upload_video(clip, title, desc)
    mark_done(video_id)
    log("SELESAI")


if __name__ == "__main__":
    main()
