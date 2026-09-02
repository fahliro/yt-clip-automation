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
import os, json, subprocess, sys, tempfile, re, pathlib, datetime
import requests

WORKDIR = pathlib.Path(tempfile.gettempdir()) / "yt_clip"
WORKDIR.mkdir(parents=True, exist_ok=True)

SILENCE_GAP = 0.4      # detik; gap antar kata > ini = dianggap silence, dibuang
DEFAULT_FILLERS = []   # kosong: kalau LLM gak specify fillers, jangan filter apa-apa
                       # LLM lebih paham mana filler natural vs substantive per konteks.
MAX_CLIPS = 100        # cap videos.insert per hari
# ID test (hardcode buat debug lokal/CI manual). Prod: override via env VIDEO_ID / WebSub / poll.
TEST_VIDEO_ID = "YVLYNuhKZpc"

def log(m): print(f"[clip] {m}", flush=True)

def run(cmd, log_stderr=False):
    log(" ".join(str(c) for c in cmd[:3]) + " ...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if log_stderr or r.returncode != 0:
        # Selalu log stderr kalau ffmpeg: membantu debug subtitle silent-skip.
        # Print FULL stderr, bukan tail 500, supaya bisa lihat baris
        # 'Parsed_subtitles_4' / 'fontselect' yang biasanya di awal log.
        stderr_full = r.stderr or "(empty)"
        if "subtitles" in str(cmd) or log_stderr:
            log(f"stderr_full ({len(stderr_full)} chars):\n{stderr_full}")
        else:
            log(f"stderr_tail: {stderr_full[-500:]}")
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
    b64 = os.environ.get("YT_COOKIES_TXT", "").strip()
    if not b64:
        raise SystemExit("YT_COOKIES_TXT kosong -> set secret (base64 dari yt_cookies.txt)")
    try:
        raw = base64.b64decode(b64).decode("utf-8")
        decoded_base64 = True
    except Exception:
        # fallback: anggap sudah plaintext (strip CR saja)
        raw = b64.replace("\r\n", "\n").replace("\r", "\n")
        decoded_base64 = False
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    cookies.write_text(raw, newline="\n", encoding="utf-8")

    # ---- DIAGNOSTIK: validasi sebelum yt-dlp biar error jelas di CI ----
    lines = [l for l in raw.split("\n") if l.strip()]
    has_tab = any("\t" in l for l in lines[1:])
    first = lines[0] if lines else "(kosong)"
    log(f"[cookies] base64_decode={decoded_base64} baris={len(lines)} "
        f"ada_tab={has_tab} header='{first[:40]}'")
    if not first.startswith("# Netscape HTTP Cookie File"):
        raise SystemExit("COOKIES SALAH: header bukan '# Netscape HTTP Cookie File'. "
                         "Pastikan YT_COOKIES_TXT = base64 dari yt_cookies.txt (bukan isi mentah).")
    if not has_tab:
        raise SystemExit("COOKIES SALAH: tidak ada TAB (field dipisah spasi?). "
                         "Notepad ubah TAB->spasi. Pakai base64, atau copy via editor lain.")

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
                          data={"model": "whisper-large-v3",
                                "response_format": "verbose_json",
                                "timestamp_granularities[]": "word"})
        if not r.ok:
            # tangkap body error biar jelas di CI (groq kasih pesan 400-nya)
            log(f"[groq] HTTP {r.status_code}: {r.text[:500]}")
            r.raise_for_status()
    data = r.json()
    # Groq/OpenAI verbose_json: "words" bersarang di dalam "segments", BUKAN top-level.
    # Pipeline butuh list words flat (buat caption + LLM) -> flatten biar caption kebakar.
    if not data.get("words"):
        flat = []
        for seg in data.get("segments", []):
            for w in seg.get("words", []):
                flat.append({"word": w.get("word", ""),
                             "start": float(w.get("start", 0)),
                             "end": float(w.get("end", 0))})
        data["words"] = flat
        log(f"[groq] flatten words dari segments: {len(flat)} kata")
    return data


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
        "menarik untuk YouTube Shorts.\n"
        "DURASI: setiap segmen HARUS 25-45 detik. Kalau momen lucu <25 detik, gabungkan "
        "dengan konteks sebelum/sesudahnya sampai 25-45 detik. Kalau >45 detik, pilih 25-45 "
        "detik PALING menarik (skip intro/outro, mulai dari hook).\n"
        "FILTERS: untuk TIAP segmen WAJIB sertakan 'fillers' = array kata yang bisa "
        "dibuang (pengisi/filler murni: 'um', 'eh', 'anu', 'apa tuh', dll). JANGAN masukkan "
        "kata substantif/konten ke fillers. Kosongkan [] kalau tidak ada filler.\n"
        "FORMAT: JSON array [{start,end,score,reason,fillers}]. start/end = detik absolut "
        "dari raw video. score virality 1-10. reason singkat (1 kalimat). "
        "Jawab HANYA JSON (no markdown).\n"
        + "\n".join(chunks)[:14000]
    )

    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    try:
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                     "Content-Type": "application/json"},
            json={"model": os.environ["LLM_MODEL"], "messages": [
                {"role": "system", "content": "Output JSON saja tanpa markdown."},
                {"role": "user", "content": transcript_for_llm}]},
            timeout=120,
        )
        if not r.ok:
            log(f"[llm] HTTP {r.status_code} url={base}/chat/completions body={r.text[:300]}")
            r.raise_for_status()
    except Exception as e:
        log(f"[llm] error: {e} -> fallback potong 45s")
        return [{"start": i, "end": min(i + 45, words[-1]["end"]), "score": 5,
                 "reason": "fallback-llm-error", "fillers": DEFAULT_FILLERS}
                for i in range(0, int(words[-1]["end"]), 45)]
    content = re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip().strip("`")
    try:
        segs = json.loads(content)
    except Exception:
        log(f"LLM gagal parse: {content[:400]}")
        segs = [{"start": i, "end": min(i + 45, words[-1]["end"]), "score": 5,
                 "reason": "fallback", "fillers": DEFAULT_FILLERS}
                for i in range(0, int(words[-1]["end"]), 45)]
    for s in segs:
        s.setdefault("fillers", DEFAULT_FILLERS)
    segs.sort(key=lambda s: s.get("score", 0), reverse=True)
    log(f"LLM pilih {len(segs)} segmen")
    return segs


# ---------------------------------------------------------------- 4a. CAPTION
def build_ass(words, out_path):
    # Font: pakai DejaVu Sans (built-in di Ubuntu runner GitHub Actions).
    # Arial sering gak ada di Linux -> ffmpeg skip render tanpa error, subtitle kosong.
    # Position: bottom area, aman dari UI platform (TikTok/Reels/Shorts).
    #   - 20% dari bawah -> MarginV=384 (1920-1536=384)
    #     Subtitle di y=1536-1632, di atas tombol like/comment/share/nama akun
    #   - FontSize 115 = 6% dari tinggi frame 1920 (range target 5-8%)
    #     -> readable, gak nutupin subjek video
    #   - Outline=4, Shadow=1 untuk kontras di atas background blur apapun
    # warna: &H00FFFFFF = putih; outline &H00000000 = hitam; Bold=1
    # Format fields (22): Name,Font,Size,PCol,SCol,OCol,B,I,U,S,SX,SY,Sp,Ang,
    #   BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
    style = ("Style: Default,DejaVu Sans,115,&H00FFFFFF,&H000000FF,&H00000000,"
             "1,0,0,0,100,100,0,0,1,4,1,2,20,20,384,1")
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
        # Offset ASS timestamps ke 0 (relative ke clip).
        # Whisper kasih absolute timestamp dari raw video; setelah ffmpeg -ss
        # seek + -t, output video mulai 0 tapi ASS masih pakai timestamp
        # absolut -> subtitle di luar range, gak ke-render. Offset di sini.
        clip_words = [{"word": w["word"], "start": w["start"] - s, "end": w["end"] - s}
                      for w in span_words]
        parts.append(cut_span(raw_path, s, e, clip_words, idx, i))
    final = concat_parts(parts, idx)
    return final


def cut_span(raw_path, s, e, words, idx, i):
    dur = max(0.5, e - s)
    out = WORKDIR / f"part_{idx:02d}_{i:02d}.mp4"
    ass = WORKDIR / f"cap_{idx:02d}_{i:02d}.ass"
    if words:
        build_ass(words, ass)
        # Log: pastikan ASS punya Dialogue entries, bukan cuma header
        n_dlg = sum(1 for ln in ass.read_text(encoding="utf-8").splitlines()
                    if ln.startswith("Dialogue:"))
        log(f"[caption] {ass.name}: {n_dlg} dialogue lines")
        # ffmpeg subtitles filter di Linux parse 'path' sbg 'option:value' kalau ada colon.
        # Path Windows 'C:\...' atau bahkan POSIX path dengan colon bisa bikin dia kira
        # colon adalah opsi (mis. original_size=WxH). Solusi: normalkan ke POSIX style.
        # Pakai forward-slash + escape colon di drive letter (C\:/Users/...) + escape backslash.
        ass_path = str(ass).replace("\\", "/").replace(":", "\\:")
        # Portrait 9:16: canvas 1080x1920, blur bg = scale raw ke 1080x1920
        # (stretch) + boxblur. Foreground = scale raw fit-width (1080 wide)
        # lalu pad vertikal ke 1080x1920 hitam. Overlay fg center.
        # PENTING: split input agar dipakai 2x di filter_complex (kalau tdk,
        # ffmpeg auto-prune dan output landscape).
        fc = (f"[0:v]split=2[vbg][vfg];"
              f"[vbg]scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920,boxblur=20[bg];"
              f"[vfg]scale=1080:-1[fg_scaled];"
              f"[fg_scaled]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[fg_padded];"
              f"[bg][fg_padded]overlay=(W-w)/2:(H-h)/2[ov];"
              f"[ov]subtitles='{ass_path}',format=yuv420p[v]")
    else:
        log(f"[caption] part_{idx:02d}_{i:02d}: kosong (no words in span)")
        # Portrait 9:16 juga untuk non-subtitle branch
        fc = (f"[0:v]split=2[vbg][vfg];"
              f"[vbg]scale=1080:1920:force_original_aspect_ratio=increase,"
              f"crop=1080:1920,boxblur=20[bg];"
              f"[vfg]scale=1080:-1[fg_scaled];"
              f"[fg_scaled]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[fg_padded];"
              f"[bg][fg_padded]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    run(["ffmpeg", "-y", "-ss", f"{s:.2f}", "-i", str(raw_path), "-t", f"{dur:.2f}",
         "-filter_complex", fc, "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", str(out)], log_stderr=True)
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


# ---------------------------------------------------------------- 4c. TITLE/DESC
# Generate title + description engaging (POV viewers, pake emoticon,
# bahasa sesuai video). Pake LLM yang sama dengan pick_segments.
# Returns: (title, description) — title max 100 char, desc max 4900 char.
import re as _re
_LANG_NAME = {
    "id": "Indonesian", "en": "English", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "ar": "Arabic", "ru": "Russian", "hi": "Hindi",
    "th": "Thai", "vi": "Vietnamese", "ms": "Malay", "tl": "Filipino",
}
def _llm_call_json(prompt, max_retries=1):
    """Call LLM and return parsed JSON dict (or None on fail).
    Retry up to max_retries times if JSON parsing fails.
    Tidak validasi bahasa di sini — itu urusan caller (gen_title_desc).
    """
    import requests
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                         "Content-Type": "application/json"},
                json={"model": os.environ["LLM_MODEL"], "messages": [
                    {"role": "system", "content": "Output HANYA JSON valid, tanpa markdown."},
                    {"role": "user", "content": prompt}]},
                timeout=60,
            )
            if not r.ok:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log(f"[llm] attempt {attempt+1} HTTP fail: {last_err}")
                continue
            content = _re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip().strip("`")
            try:
                return json.loads(content)
            except Exception as pe:
                last_err = f"JSON parse fail: {pe}; raw={content[:150]}"
                log(f"[llm] attempt {attempt+1} {last_err}")
        except Exception as e:
            last_err = f"network: {e}"
            log(f"[llm] attempt {attempt+1} {last_err}")
    return None


def gen_title_desc(seg, transcript, lang):
    """Generate engaging title + description via LLM.
    - title: catchy, max 100 char, pake 1-2 emoticon, bahasa = lang
    - desc: 2-4 kalimat POV viewers, pake emoticon, bahasa = lang
    Simple approach: prompt tegas (100% bahasa target) + fallback kalau LLM gagal.
    """
    lang_name = _LANG_NAME.get(lang, "English")
    seg_text = seg.get("text") or seg.get("reason", "")
    if not seg_text and "words" in transcript:
        s, e = float(seg["start"]), float(seg["end"])
        ws = [w for w in transcript["words"] if s <= w["start"] < e]
        seg_text = " ".join(w["word"] for w in ws)[:800]

    # Optional: override bahasa via env (untuk testing/rebrand)
    lang_override = os.environ.get("META_LANG_OVERRIDE", "").strip().lower()
    if lang_override in _LANG_NAME:
        lang = lang_override
        lang_name = _LANG_NAME[lang]

    prompt = (
        f"Buat title + description untuk YouTube Shorts (max 60 detik).\n"
        f"\n"
        f"=== ATURAN BAHASA (WAJIB, tanpa exception) ===\n"
        f"Bahasa target = {lang_name} ({lang}).\n"
        f"SELURUH title dan description HARUS 100% {lang_name} dari awal sampai akhir.\n"
        f"JANGAN pakai kata dari bahasa lain.\n"
        f"Kalau konteks mengandung istilah asing, TRANSLATE atau PARAPHRASE ke {lang_name}.\n"
        f"\n"
        f"=== FORMAT ===\n"
        f"- title: 1 kalimat catchy, max 100 char, 1-2 emoticon\n"
        f"- desc: 2-4 kalimat engaging + 1-2 hashtag relevan + CTA (like/comment/share)\n"
        f"- POV: sudut pandang VIEWER (yang nonton & react), bukan uploader.\n"
        f"- Output JSON valid: {{\"title\": str, \"desc\": str}}\n"
        f"\n"
        f"=== KONTEKS SCRIPT ===\n"
        f"{seg_text}\n"
        f"\n"
        f"=== ALASAN SEGMENT INI MENARIK ===\n"
        f"{seg.get('reason', '')}\n"
    )

    data = _llm_call_json(prompt)
    if data is not None:
        title = (data.get("title") or "").strip()[:100]
        desc = (data.get("desc") or data.get("description") or "").strip()[:4900]
        if title and desc:
            log(f"[title-desc] {lang} -> '{title[:60]}'")
            return title, desc

    # Fallback template kalau LLM gagal
    log(f"[title-desc] LLM gagal -> fallback template ({lang})")
    score = seg.get("score", "?")
    if lang == "id":
        title = f"Kamu gak bakal nyangka! 😱 Skor {score} 🔥"
        desc = (seg.get("reason") or "Wajib tonton!") + "\n\nLike ya! 💯 #shorts"
    elif lang == "en":
        title = f"You won't believe this! 😱 Score {score} 🔥"
        desc = (seg.get("reason") or "Must watch!") + "\n\nLike & share! 💯 #shorts"
    else:
        title = f"{lang_name} highlight! 😱 Skor {score} 🔥"
        desc = (seg.get("reason") or f"Watch this {lang_name} clip!") + f"\n\nLike ya! 💯 #shorts"
    return title[:100], desc[:4900]





# ---------------------------------------------------------------- 4d. THUMBNAIL
# Extract frame terbaik dari clip + tambah hook text + emoticon.
# Returns: path ke thumbnail JPG (1080x1920 portrait), atau None kalau gagal.
# Vision flow:
#   1. Extract 3 frame candidate (t=0.3s, t=2s, t=tengah)
#   2. Encode base64 + kirim ke LLM vision (kalau support)
#   3. LLM pilih frame terbaik + kasih hook text
#   4. Fallback: pakai frame tengah + hook default
import base64 as _b64
def _gen_thumbnail_style(seg, transcript, lang, workdir):
    """Generate hook text + visual style untuk thumbnail dari konteks video.
    Context: transcript segment (~500 char), reason, lang.
    Return dict: {hook_text, font_family, font_size, text_color, emoji_size, vertical_position}
    Falls back ke default Bebas Neue/kuning kalau LLM error.
    """
    import requests as _req
    # Ambil transcript segment untuk konteks naratif
    seg_text = ""
    if "words" in transcript:
        s, e = float(seg["start"]), float(seg["end"])
        ws = [w for w in transcript["words"] if s <= w["start"] < e]
        seg_text = " ".join(w["word"] for w in ws)[:800]
    # Default fallback
    default = {
        "hook_text": seg.get("reason", "TONTON!")[:60] or "TONTON!",
        "font_family": "Bebas Neue",
        "font_size": 134,
        "text_color": "#FFE600",
        "emoji_size": 144,
        "vertical_position": "center",
    }
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    if not base or "kilo" in (os.environ.get("LLM_MODEL", "").lower()):
        return default  # skip kalau model gak support (heuristic: kilo*)
    lang_name = _LANG_NAME.get(lang, "English")
    prompt = (
            f"Generate thumbnail spec untuk YouTube Shorts.\n"
            f"BAHASA: {lang_name} ({lang}). SEMUA output WAJIB bahasa {lang_name}.\n"
            f"\n"
            f"=== KONTEKS SCRIPT (transcript segment) ===\n{seg_text}\n"
            f"===\n"
            f"\n"
            f"TUGAS: Baca script di atas, lalu buat hook text yang:\n"
            f"  1. INFORMATIF — mengandung konteks SPESIFIK dari script (plot twist, punchline, info kunci)\n"
            f"  2. PENASARAN (teaser) — bikin penasaran klik video, tapi BUKAN kalimat nggantung kosong\n"
            f"  3. POV viewer (bukan uploader)\n"
            f"  4. Max 7 kata, 1-2 emoticon\n"
            f"  5. JANGAN deskripsi visual/gambar/frame\n"
            f"  6. JANGAN generic ('Wajib tonton!', 'Video ini...')\n"
            f"  7. JANGAN kalimat nggantung tanpa konteks ('Mau tau caranya?', 'Cek selengkapnya!')\n"
            f"\n"
            f"OUTPUT JSON (no markdown):\n"
            f"{{\n"
            f'  "hook_text": str (WAJIB refer ke konten script),\\n'
            f'  "font_family": str (salah satu: "Bebas Neue" | "Anton" | "Inter" | "Roboto" | "Poppins"),\\n'
            f'  "font_size": int (90-200, default 134),\\n'
            f'  "text_color": hex color string (mis. "#FFE600" kuning, "#FFFFFF" putih, "#FF1744" merah),\\n'
            f'  "emoji_size": int (80-200, default 144),\\n'
            f'  "vertical_position": salah satu "top" | "center" | "bottom"\\n'
            f"}}\\n"
            f"\n"
            f"=== CONTOH (GOOD vs BAD) ===\n"
            f"\n"
            f"Script: 'Ternyata suara itu AI, bukan manusia asli!'\n"
            f"  GOOD hook: 'AI ngaku manusia! 🤯' (informatif + penasaran, ada konteks)\n"
            f"  GOOD hook: 'Suara itu AI?! 😱' (teaser, spesifik ke script)\n"
            f"  BAD hook: 'Muka terkejut' (deskripsi visual, JANGAN)\n"
            f"  BAD hook: 'Wajib tonton!' (generic, JANGAN)\n"
            f"  BAD hook: 'Mau tau caranya?' (nggantung tanpa konteks, JANGAN)\n"
            f"\n"
            f"Script: 'Plot twist: ternyata dia adiknya sendiri!'\n"
            f"  GOOD hook: 'Plot twist gila! 😱' (informatif + penasaran)\n"
            f"  GOOD hook: 'Mereka bersaudara!?' (teaser, spesifik)\n"
            f"  BAD hook: 'Muka sedih' (visual, JANGAN)\n"
            f"  BAD hook: 'Cek selengkapnya!' (nggantung kosong, JANGAN)\n"
            f"\n"
            f"Script: '5 tips supaya tidur nyenyak. Tips 3: matikan HP 1 jam sebelum tidur.'\n"
            f"  GOOD hook: 'Tips 3 paling penting! 💡' (informatif + penasaran)\n"
            f"  GOOD hook: 'Matikan HP sebelum tidur? 📱' (teaser, spesifik)\n"
            f"  BAD hook: 'Orang ngorok' (visual, JANGAN)\n"
            f"  BAD hook: 'Gimana caranya?' (nggantung kosong, JANGAN)\n"
            f"\n"
            f"=== INGAT: hook_text HARUS tentang SCRIPT, bukan GAMBAR ==="
        )
    try:
        r = _req.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
                     "Content-Type": "application/json"},
            json={"model": os.environ["LLM_MODEL"], "messages": [
                {"role": "system", "content": "Output JSON saja tanpa markdown."},
                {"role": "user", "content": prompt}]},
            timeout=60,
        )
        if r.ok:
            content = _re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip().strip("`")
            data = json.loads(content)
            # Trust prompt: kalau LLM kasih hook_text, pakai langsung.
            # Bahasa compliance sudah dijaga prompt ("SEMUA output WAJIB bahasa X").
            full_text = (data.get("hook_text") or "").strip()
            if full_text:
                default["hook_text"] = full_text[:60]
                default["font_family"] = data.get("font_family", default["font_family"])
                try: default["font_size"] = int(data.get("font_size", 134))
                except Exception: pass
                if re.match(r"^#[0-9A-Fa-f]{6}$", str(data.get("text_color", ""))):
                    default["text_color"] = data["text_color"]
                try: default["emoji_size"] = int(data.get("emoji_size", 144))
                except Exception: pass
                if data.get("vertical_position") in ("top", "center", "bottom"):
                    default["vertical_position"] = data["vertical_position"]
                log(f"[thumb-style] {lang} font={default['font_family']} "
                    f"color={default['text_color']} hook='{default['hook_text'][:50]}'")
                return default
            else:
                log(f"[thumb-style] LLM output kosong -> default")
    except Exception as e:
        log(f"[thumb-style] LLM error: {e} -> default")
    return default


def _extract_frames(clip_path, out_dir, n=3, raw_path=None, start_offset=0.0):
    """Extract n frame dari clip di t=0.3, t=2, t=mid. Return list of paths.
    Kalau raw_path + start_offset dikasih, extract dari raw video
    (supaya tidak ada subtitle terbakar di frame) DAN resize ke portrait 1080x1920
    (sama style dengan cut_span: blur bg + foreground center).
    Kalau tanpa raw_path, extract dari clip_path (sudah 1080x1920 dari cut_span).
    """
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = raw_path if raw_path else clip_path
    offset = start_offset if raw_path else 0.0
    # Probe durasi (dari source)
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except Exception:
        dur = 5.0
    # Timestamp absolute di source (raw), bukan relatif clip
    timestamps = [min(0.3, dur*0.05), min(2.0, dur*0.4), dur*0.5]
    timestamps = [t + offset for t in timestamps]
    # Kalau extract dari raw (landscape source), resize ke portrait 1080x1920
    # dengan style sama seperti cut_span (blur bg + fg center) — biar
    # thumbnail_PIL gak kepotong/landscape.
    if raw_path:
        fc = ("[0:v]split=2[vbg][vfg];"
              "[vbg]scale=1080:1920:force_original_aspect_ratio=increase,"
              "crop=1080:1920,boxblur=20[bg];"
              "[vfg]scale=1080:-1[fg_scaled];"
              "[fg_scaled]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[fg_padded];"
              "[bg][fg_padded]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]")
    else:
        fc = None  # clip sudah 1080x1920, gak perlu filter
    frames = []
    for i, t in enumerate(timestamps):
        fp = out_dir / f"frame_{i}.jpg"
        cmd = ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(source)]
        if fc:
            cmd += ["-filter_complex", fc, "-map", "[v]", "-frames:v", "1", "-q:v", "3", str(fp)]
        else:
            cmd += ["-frames:v", "1", "-q:v", "3", str(fp)]
        subprocess.run(cmd, capture_output=True)
        if fp.exists() and fp.stat().st_size > 1000:
            frames.append(fp)
    return frames

def _add_hook_text(frame_path, hook_text, out_path):
    """Tambah hook text + emoticon ke frame pakai PIL.
    Hook text: bold, besar, warna mencolok (kuning), outline hitam.
    Emoticon: render dengan font emoji berwarna kalau tersedia
    (Noto Color Emoji di Linux, Segoe UI Emoji di Windows). Kalau tidak ada,
    fallback ke monokrom (kuning) — tetep terbaca.
    """
    import re as _re
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    # Font text utama (bold sans)
    font_size = int(h * 0.06)
    font = None
    text_font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for fp in text_font_paths:
        if pathlib.Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()
    # Font emoji berwarna (Apple/Google/Microsoft style)
    emoji_font = None
    emoji_font_paths = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",  # Linux
        "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",  # Linux alt
        "/usr/share/fonts/NotoColorEmoji.ttf",  # macOS
        "C:\\Windows\\Fonts\\seguiemj.ttf",  # Windows Segoe UI Emoji
        "C:\\Windows\\Fonts\\seguisb.ttf",  # Windows Segoe UI Symbol
    ]
    for fp in emoji_font_paths:
        if pathlib.Path(fp).exists():
            try:
                # Render size 2x biar emoji lebih besar & jelas di thumbnail
                emoji_font = ImageFont.truetype(fp, int(font_size * 1.0))
                break
            except Exception:
                pass
    # Split text jadi: karakter teks + karakter emoji
    # Range emoji Unicode: 0x1F300-0x1FAFF (majority), 0x2600-0x27BF (symbols)
    # Plus variation selectors (0xFE0F) dan ZWJ (0x200D)
    emoji_pattern = _re.compile(
        r'([\U0001F300-\U0001FAFF\U00002600-\U000027BF\u200D\uFE0F])'
    )
    # Wrap per baris (15 char per line max)
    words = hook_text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] > w * 0.85 and cur:
            lines.append(cur); cur = word
        else:
            cur = test
    if cur: lines.append(cur)
    line_h = int(h * 0.08)
    y_start = int(h * 0.15)
    # Warna hook text — dinamis via env HOOK_TEXT_COLOR (default kuning #FFE600)
    # Format: 'R,G,B' mis. '255,230,0' atau '255,255,255' (putih) atau '255,0,0' (merah).
    # Outline selalu hitam (high contrast).
    hook_color_str = os.environ.get("HOOK_TEXT_COLOR", "255,230,0")
    try:
        r, g, b = [int(c.strip()) for c in hook_color_str.split(",")][:3]
        hook_color = (r, g, b)
    except Exception:
        hook_color = (255, 230, 0)
    for i, line in enumerate(lines):
        # Hitung total width per line (sum of token widths + spacing 4px)
        tokens = [t for t in emoji_pattern.split(line) if t]
        token_widths = []
        for tok in tokens:
            is_e = bool(emoji_pattern.match(tok))
            tf = emoji_font if (is_e and emoji_font) else font
            tb = draw.textbbox((0, 0), tok, font=tf)
            token_widths.append(tb[2] - tb[0])
        # Total = sum widths + 4px spacing per gap
        total_w = sum(token_widths) + max(0, len(tokens) - 1) * 4
        x = (w - total_w) // 2  # center per line
        y = y_start + i * line_h
        # Render per-token
        cursor_x = x
        for idx, tok in enumerate(tokens):
            is_emoji = bool(emoji_pattern.match(tok))
            tok_font = emoji_font if (is_emoji and emoji_font) else font
            # Outline hitam 8-arah
            for dx, dy in [(-3, -3), (-3, 3), (3, -3), (3, 3), (-3, 0), (3, 0), (0, -3), (0, 3)]:
                draw.text((cursor_x+dx, y+dy), tok, font=tok_font, fill=(0, 0, 0))
            # Teks dengan warna (kuning default) — JANGAN override kalau emoji font
            if is_emoji and emoji_font:
                # font emoji berwarna — biar natural
                draw.text((cursor_x, y), tok, font=tok_font)
            else:
                draw.text((cursor_x, y), tok, font=tok_font, fill=hook_color)
            cursor_x += token_widths[idx] + 4
    img.save(out_path, "JPEG", quality=92)
    return out_path

def gen_thumbnail(clip_path, seg, lang, workdir, raw_path=None, start_offset=0.0, transcript=None):
    """Generate thumbnail untuk clip. Returns path to JPG or None.
    1. Pre-step: LLM generate hook text + style (font, color, size, position)
       dari transcript segment. Pisah dari vision step karena text-style butuh
       konteks NARASI, vision butuh konteks VISUAL.
    2. Extract 3 frame dari raw_path (kalau ada) supaya tidak ada subtitle terbakar,
       fallback ke clip_path kalau raw_path tidak dikasih
    3. Try LLM vision (kalau model support image input) -> pilih frame terbaik
    4. Render HTML+CSS via Playwright + Twemoji inline + Google Fonts (default)
       Fallback ke PIL kalau Playwright error / THUMBNAIL_HTML=0
    """
    workdir = pathlib.Path(workdir)
    frames = _extract_frames(clip_path, workdir / "frames",
                              raw_path=raw_path, start_offset=start_offset)
    if not frames:
        log("[thumb] gagal extract frame"); return None
    chosen = frames[len(frames) // 2]  # default: frame tengah
    # Step 1: Generate hook text + style dari LLM dengan konteks video (transcript)
    # Pisah dari vision: text-style butuh konteks NARASI, vision butuh konteks VISUAL
    style = _gen_thumbnail_style(seg, transcript, lang, workdir)
    hook_text = style["hook_text"]
    # Step 2: Try LLM vision (best-effort, kalau model support -> pilih frame terbaik)
    try:
        import requests as _req
        base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        model = os.environ.get("LLM_MODEL", "")
        # Encode frames as data URL
        content_parts = [{
            "type": "text",
            "text": (f"Pilih 1 frame PALING menarik untuk thumbnail YouTube Shorts. "
                    f"Jawab HANYA JSON: {{\"frame_index\": 0|1|2, \"hook_text\": str (max 5 kata, "
                    f"POV viewer, ada 1 emoticon, bahasa {lang})}}")
        }]
        for fp in frames:
            data = _b64.b64encode(fp.read_bytes()).decode()
            content_parts.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}})
        r = _req.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', '')}",
                     "Content-Type": "application/json"},
            json={"model": model, "messages": [
                {"role": "system", "content": "Output JSON saja tanpa markdown."},
                {"role": "user", "content": content_parts}]},
            timeout=60,
        )
        if r.ok:
            content = _re.sub(r"```(?:json)?", "", r.json()["choices"][0]["message"]["content"]).strip().strip("`")
            data = json.loads(content)
            idx = int(data.get("frame_index", 1))
            if 0 <= idx < len(frames):
                chosen = frames[idx]
            hook = (data.get("hook_text") or hook_text).strip()[:60]
            if hook:
                hook_text = hook
                log(f"[thumb] LLM vision: frame {idx} hook='{hook_text}'")
    except Exception as e:
        log(f"[thumb] LLM vision skip: {e} (fallback ke frame tengah)")
    # Generate thumbnail. Default: HTML+CSS via Playwright (Twemoji + Google Fonts).
    # Set THUMBNAIL_HTML=0 untuk fallback ke PIL (gak butuh Chromium).
    use_html = os.environ.get("THUMBNAIL_HTML", "1") != "0"
    out = workdir / f"thumb_{pathlib.Path(clip_path).stem}.jpg"
    if use_html:
        try:
            import thumbnail as _thumb
            _thumb.gen_thumbnail_html(
                str(chosen), hook_text, str(out),
                font_family=style["font_family"],
                font_size=style["font_size"],
                text_color=style["text_color"],
                emoji_size=style["emoji_size"],
                vertical_position=style["vertical_position"],
            )
            log(f"[thumb] HTML+CSS saved: {out} ({out.stat().st_size}B) "
                f"font={style['font_family']} color={style['text_color']}")
        except Exception as e:
            log(f"[thumb] HTML+CSS gagal ({e}) -> fallback PIL")
            _add_hook_text(chosen, hook_text, out)
            log(f"[thumb] PIL saved: {out} ({out.stat().st_size}B)")
    else:
        _add_hook_text(chosen, hook_text, out)
        log(f"[thumb] PIL saved: {out} ({out.stat().st_size}B)")
    return out if out.exists() else None


def set_thumbnail(video_id, thumb_path):
    """Upload custom thumbnail via YouTube Data API thumbnails.set."""
    import requests as _req
    tok = _req.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_UPLOAD_CLIENT"],
        "client_secret": os.environ["YT_UPLOAD_SECRET"],
        "refresh_token": os.environ["YT_UPLOAD_TOKEN"],
        "grant_type": "refresh_token",
    }).json()
    access = tok.get("access_token")
    if not access:
        log("[thumb-set] gagal dapat access token"); return False
    with open(thumb_path, "rb") as f:
        r = _req.post(
            f"https://www.googleapis.com/youtube/v3/thumbnails/set?videoId={video_id}",
            headers={"Authorization": f"Bearer {access}"},
            files={"media": (pathlib.Path(thumb_path).name, f, "image/jpeg")},
            timeout=60,
        )
    if r.ok:
        log(f"[thumb-set] uploaded for {video_id}")
        return True
    log(f"[thumb-set] gagal: HTTP {r.status_code} body={r.text[:200]}")
    return False


# ---------------------------------------------------------------- 5. UPLOAD
def upload_video(path, title, description):
    import requests, time
    tok = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YT_UPLOAD_CLIENT"],
        "client_secret": os.environ["YT_UPLOAD_SECRET"],
        "refresh_token": os.environ["YT_UPLOAD_TOKEN"],
        "grant_type": "refresh_token",
    }).json()
    access = tok["access_token"]
    meta = json.dumps({
        "snippet": {"title": title[:100], "description": (description or "")[:4900],
                    "channelId": os.environ["YT_CHANNEL_ID"]},
        "status": {"privacyStatus": "public"},
    }).encode()
    last_err = None
    for attempt in range(3):
        try:
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
        except requests.HTTPError as e:
            last_err = e
            body = ""
            try: body = e.response.text[:400]
            except Exception: pass
            log(f"[upload] attempt {attempt+1} gagal HTTP {e.response.status_code if e.response else '?'} body={body}")
            if e.response and e.response.status_code == 400:
                # 400 sering transient (rate/processing) -> retry dgn jeda
                time.sleep(20 * (attempt + 1)); continue
            raise
    raise last_err or RuntimeError("upload gagal")


# ---------------------------------------------------------------- STATE
def already_done(video_id):
    sf = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
    if not sf.exists():
        return False
    return video_id in set(json.loads(sf.read_text()).get("done", []))


def mark_done(video_id):
    sf = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
    data = json.loads(sf.read_text()) if sf.exists() else {"done": [], "uploaded": {}}
    if video_id not in data["done"]:
        data["done"].append(video_id)
    sf.write_text(json.dumps(data, indent=2))


def save_uploaded(youtube_id, raw_video_id, title, thumb_path):
    """Track uploaded Shorts di state.json biar bisa di-retry kalau ada error."""
    sf = pathlib.Path(os.environ.get("STATE_FILE", "state.json"))
    data = json.loads(sf.read_text()) if sf.exists() else {"done": [], "uploaded": {}}
    data.setdefault("uploaded", {})[youtube_id] = {
        "raw": raw_video_id, "title": title, "thumb": str(thumb_path) if thumb_path else None,
        "ts": datetime.datetime.now().isoformat()}
    sf.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------- 5b. META UPLOAD
# Cross-post Reels ke Instagram + Facebook via Meta Graph API (official).
# - Facebook: /<page-id>/videos (page access token, pages_manage_posts)
# - Instagram: /<ig-user-id>/media (container) -> publish (instagram_content_publish)
#
# ENV yang dibutuhkan (GitHub Secrets):
#   META_ACCESS_TOKEN     Long-lived Page access token (60 hari, refresh manual)
#   META_FB_PAGE_ID       Facebook Page ID tempat upload
#   META_IG_USER_ID       Instagram Business User ID
#   META_IG_HASHTAG       Default hashtag (mis. "#shorts" + niche)
#
# Setup lengkap: https://developers.facebook.com/docs/video-api/guides/reels/
def _meta_get_long_token():
    """Ambil long-lived Page access token dari env (atau refresh kalau ada)."""
    tok = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not tok:
        log("[meta] META_ACCESS_TOKEN kosong -> skip"); return None
    return tok


def upload_to_facebook_reels(clip_path, title, description):
    """Upload Reels ke Facebook Page via Graph API /{page-id}/videos.
    Returns: video_id (FB) atau None kalau gagal/skip.
    """
    tok = _meta_get_long_token()
    page_id = os.environ.get("META_FB_PAGE_ID", "").strip()
    if not tok or not page_id:
        log("[meta-fb] skip: token/page_id kosong"); return None
    try:
        with open(clip_path, "rb") as f:
            r = requests.post(
                f"https://graph-video.facebook.com/v22.0/{page_id}/videos",
                params={"access_token": tok, "title": title[:255],
                        "description": (description or "")[:5000],
                        "published": "true"},
                files={"source": (pathlib.Path(clip_path).name, f, "video/mp4")},
                timeout=600,
            )
        if r.ok:
            vid = r.json().get("id")
            log(f"[meta-fb] uploaded: https://facebook.com/{page_id}/videos/{vid}")
            return vid
        log(f"[meta-fb] gagal: HTTP {r.status_code} body={r.text[:300]}")
    except Exception as e:
        log(f"[meta-fb] error: {e}")
    return None


def upload_to_instagram_reels(clip_path, title, description):
    """Upload Reels ke Instagram Business via Graph API 2-step container pattern.
    Returns: media_id (IG) atau None kalau gagal/skip.
    Flow:
      1. POST /{ig-user-id}/media (video_url, caption, media_type=REELS)
      2. POST /{ig-user-id}/media_publish (creation_id)
    Note: media harus sudah di-host di URL publik (IG tidak terima upload multipart).
    Workaround: pakai staged upload via Facebook dulu (DONE), lalu set IG
    source_url ke URL FB (Graph API support ini, since v18+).
    """
    tok = _meta_get_long_token()
    ig_user_id = os.environ.get("META_IG_USER_ID", "").strip()
    fb_vid = os.environ.get("META_LAST_FB_VIDEO_ID", "").strip()  # di-set sebelumnya
    if not tok or not ig_user_id or not fb_vid:
        log("[meta-ig] skip: token/ig_user_id/fb_video_id kosong"); return None
    # Step 1: container
    try:
        hashtag = os.environ.get("META_IG_HASHTAG", "#shorts #reels")
        caption = (description or title) + "\n\n" + hashtag
        r = requests.post(
            f"https://graph.facebook.com/v22.0/{ig_user_id}/media",
            params={"access_token": tok, "media_type": "REELS",
                    "video_url": f"https://facebook.com/{os.environ['META_FB_PAGE_ID']}/videos/{fb_vid}",
                    "caption": caption[:2200], "share_to_feed": "true"},
            timeout=120,
        )
        if not r.ok:
            log(f"[meta-ig] container gagal: HTTP {r.status_code} body={r.text[:300]}")
            return None
        creation_id = r.json().get("id")
        if not creation_id:
            log(f"[meta-ig] no creation_id: {r.text[:200]}"); return None
        log(f"[meta-ig] container created: {creation_id}")
        # Step 2: publish
        r2 = requests.post(
            f"https://graph.facebook.com/v22.0/{ig_user_id}/media_publish",
            params={"access_token": tok, "creation_id": creation_id},
            timeout=120,
        )
        if r2.ok:
            mid = r2.json().get("id")
            log(f"[meta-ig] published: {mid}")
            return mid
        log(f"[meta-ig] publish gagal: HTTP {r2.status_code} body={r2.text[:300]}")
    except Exception as e:
        log(f"[meta-ig] error: {e}")
    return None


def cross_post_meta(clip_path, title, description):
    """Orchestrator: upload ke FB Reels dulu, kalau sukses upload ke IG Reels
    (pakai FB video_id sebagai source untuk IG container)."""
    fb_vid = upload_to_facebook_reels(clip_path, title, description)
    if not fb_vid:
        return None
    # Set env var sementara biar upload_to_instagram_reels bisa baca
    os.environ["META_LAST_FB_VIDEO_ID"] = fb_vid
    ig_vid = upload_to_instagram_reels(clip_path, title, description)
    return {"fb": fb_vid, "ig": ig_vid}


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
def _log_trace(e, prefix=""):
    """Print full traceback to log so CI Actions shows exactly which step failed."""
    import traceback
    log(f"{prefix}ERROR: {type(e).__name__}: {e}")
    log(f"{prefix}traceback: {traceback.format_exc().replace(chr(10), ' | ')}")


def main():
    log("=" * 60)
    log("PHASE 0: START yt-clip pipeline")
    log("=" * 60)
    video_id = os.environ.get("VIDEO_ID")
    if not video_id:
        log("VIDEO_ID env kosong -> cek video terbaru via API (poll_latest)")
        video_id = poll_latest()
    if not video_id:
        video_id = TEST_VIDEO_ID
        log(f"VIDEO_ID tetap kosong -> pakai TEST_VIDEO_ID={video_id}")
    else:
        log(f"VIDEO_ID = {video_id}")

    if already_done(video_id):
        log("PHASE 0: video sudah pernah di-clip (state.json match) -> SKIP")
        return

    # PHASE 1: Download raw
    log("=" * 60)
    log("PHASE 1/6: DOWNLOAD raw video")
    log("=" * 60)
    try:
        raw = download_raw(video_id)
        log(f"PHASE 1: OK raw={raw}")
    except Exception as e:
        _log_trace(e, "[phase1-download] ")
        raise

    # PHASE 2: Transcribe
    log("=" * 60)
    log("PHASE 2/6: TRANSCRIBE (Groq Whisper)")
    log("=" * 60)
    try:
        tr = transcribe(raw)
        lang = tr.get("language", "en")
        words = tr.get("words", [])
        log(f"PHASE 2: OK lang={lang} words={len(words)} durasi~={tr.get('duration', '?')}s")
    except Exception as e:
        _log_trace(e, "[phase2-transcribe] ")
        raise

    # PHASE 3: Pick segments
    log("=" * 60)
    log("PHASE 3/6: PICK SEGMENTS")
    log("=" * 60)
    try:
        segs = pick_segments(tr)
        log(f"PHASE 3: OK dapat {len(segs)} segment(s) (max MAX_CLIPS={MAX_CLIPS if 'MAX_CLIPS' in dir() else '?'})")
        for j, s in enumerate(segs):
            log(f"  seg[{j}]: start={float(s.get('start',0)):.1f}s end={float(s.get('end',0)):.1f}s "
                f"score={s.get('score','?')} reason='{(s.get('reason','') or '')[:60]}'")
    except Exception as e:
        _log_trace(e, "[phase3-pick] ")
        raise

    upload_errors = 0
    total_segs = len(segs[:MAX_CLIPS]) if 'MAX_CLIPS' in dir() else len(segs)
    log(f"PHASE 4-6: loop {total_segs} segment(s)")

    for i, seg in enumerate(segs[:MAX_CLIPS]):
        log("-" * 60)
        log(f"SEGMENT {i+1}/{total_segs}: start={float(seg['start']):.1f}s end={float(seg['end']):.1f}s")
        log("-" * 60)

        # PHASE 4a: Cut clip
        log(f"[seg{i}] PHASE 4a/6: CUT clip (ffmpeg)")
        try:
            clip = clip_segment(raw, seg, words, i)
            log(f"[seg{i}] PHASE 4a: OK clip={clip}")
        except Exception as e:
            _log_trace(e, f"[seg{i}-cut] ")
            continue  # skip segment ini, lanjut ke berikutnya

        # PHASE 4b: Title/desc
        log(f"[seg{i}] PHASE 4b/6: GEN title+desc via LLM (lang={lang})")
        try:
            title, desc = gen_title_desc(seg, tr, lang)
            log(f"[seg{i}] PHASE 4b: OK title='{title[:60]}' desc_len={len(desc)}")
        except Exception as e:
            _log_trace(e, f"[seg{i}-title] ")
            log(f"[seg{i}] PHASE 4b: GAGAL -> fallback template")
            title, desc = ("Kamu gak bakal nyangka! 😱", "Wajib tonton!\n\nLike ya! 💯 #shorts")

        # Save meta
        try:
            meta_file = WORKDIR / f"meta_{pathlib.Path(str(clip)).stem}.json"
            meta_file.write_text(json.dumps({
                "seg_index": i, "title": title, "desc": desc, "lang": lang,
                "score": seg.get("score"), "reason": seg.get("reason", ""),
                "start": float(seg["start"]), "end": float(seg["end"]),
                "fillers": seg.get("fillers", []),
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            log(f"[seg{i}] meta saved: {meta_file}")
        except Exception as e:
            _log_trace(e, f"[seg{i}-meta-save] ")

        # PHASE 4c: Thumbnail
        log(f"[seg{i}] PHASE 4c/6: GEN thumbnail (frame + LLM hook + PIL/Playwright)")
        thumb = None
        try:
            thumb = gen_thumbnail(clip, seg, lang, WORKDIR,
                                  raw_path=raw, start_offset=float(seg["start"]),
                                  transcript=tr)
            log(f"[seg{i}] PHASE 4c: OK thumb={thumb}")
        except Exception as e:
            _log_trace(e, f"[seg{i}-thumb] ")
            log(f"[seg{i}] PHASE 4c: SKIP thumbnail (lanjut upload tanpa thumb)")

        # Update meta with thumb path
        if thumb:
            try:
                meta_data = json.loads(meta_file.read_text(encoding="utf-8"))
                meta_data["thumb"] = str(thumb)
                meta_file.write_text(json.dumps(meta_data, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception as e:
                _log_trace(e, f"[seg{i}-meta-update] ")

        # PHASE 5: Upload YouTube
        log(f"[seg{i}] PHASE 5/6: UPLOAD to YouTube")
        video_id_yt = None
        try:
            video_id_yt = upload_video(clip, title, desc)
            log(f"[seg{i}] PHASE 5: OK uploaded video_id_yt={video_id_yt}")
            try:
                save_uploaded(video_id_yt, video_id, title, str(thumb) if thumb else None)
            except Exception as e:
                _log_trace(e, f"[seg{i}-save-uploaded] ")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            body = ""
            try: body = e.response.text[:200] if e.response else ""
            except Exception: pass
            _log_trace(e, f"[seg{i}-upload] ")
            if "uploadLimitExceeded" in body or code in (400, 429):
                log(f"[seg{i}] PHASE 5: SKIP (limit/quota) — artifact clip tetap di WORKDIR")
                upload_errors += 1
            else:
                raise  # error lain -> stop pipeline
        except Exception as e:
            _log_trace(e, f"[seg{i}-upload] ")
            log(f"[seg{i}] PHASE 5: error (non-HTTP) — artifact clip tetap di WORKDIR")
            upload_errors += 1

        # Set thumbnail on YouTube
        if thumb and video_id_yt:
            log(f"[seg{i}] PHASE 5b: SET thumbnail on YouTube")
            try:
                set_thumbnail(video_id_yt, thumb)
                log(f"[seg{i}] PHASE 5b: OK thumb set")
            except Exception as e:
                _log_trace(e, f"[seg{i}-thumb-set] ")

        # PHASE 6: Cross-post Meta
        log(f"[seg{i}] PHASE 6/6: CROSS-POST Meta (FB Reels + IG Reels)")
        try:
            cross_post_meta(clip, title, desc)
            log(f"[seg{i}] PHASE 6: OK cross-post")
        except Exception as e:
            _log_trace(e, f"[seg{i}-meta-cross] ")
            log(f"[seg{i}] PHASE 6: skip (lanjut)")

    # Final
    log("=" * 60)
    log(f"FINAL: {total_segs} segment diproses, {upload_errors} upload error")
    log("=" * 60)
    mark_done(video_id)
    if upload_errors and upload_errors == total_segs:
        log(f"SELESAI (semua upload di-skip, artifact siap di-upload manual)")
    else:
        log("SELESAI")


if __name__ == "__main__":
    main()
