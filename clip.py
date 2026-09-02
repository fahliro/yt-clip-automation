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
def gen_title_desc(seg, transcript, lang):
    """Generate engaging title + description via LLM.
    - title: catchy, max 100 char, pake 1-2 emoticon, bahasa = lang
    - desc: 2-4 kalimat POV viewers, pake emoticon, bahasa = lang
    Falls back ke simple title kalau LLM gagal.
    """
    import requests
    lang_name = _LANG_NAME.get(lang, "English")
    seg_text = seg.get("text") or seg.get("reason", "")
    if not seg_text and "words" in transcript:
        # Ambil transkrip di range segment ini
        s, e = float(seg["start"]), float(seg["end"])
        ws = [w for w in transcript["words"] if s <= w["start"] < e]
        seg_text = " ".join(w["word"] for w in ws)[:800]

    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    prompt = (
        f"Buat title + description untuk YouTube Shorts (max 60 detik).\n"
        f"BAHASA: pakai {lang_name} ({lang}). Kalau video bhs Indonesia -> bhs Indonesia.\n"
        f"EMOTICON: pakai 1-2 emoticon yang relevan di title, 3-5 di description.\n"
        f"POV: tulis dari sudut pandang VIEWER (yang nonton & reaction), bukan uploader.\n"
        f"  Mis. bukan 'Saya cerita tentang X' tapi 'Kamu gak bakal percaya X! 😱'\n"
        f"  Hindari kata 'video ini', 'clip ini', 'konten ini'.\n"
        f"FORMAT:\n"
        f"  title: 1 kalimat catchy, max 100 char, ada 1-2 emoticon\n"
        f"  desc: 2-4 kalimat engaging + 1-2 hashtag relevan + CTA (like/comment/share)\n"
        f"  JSON: {{\"title\": str, \"desc\": str}}\n"
        f"CONTEXT: {seg_text}\n"
        f"REASON: {seg.get('reason', '')}\n"
        f"Jawab HANYA JSON (no markdown)."
    )
    try:
        r = requests.post(
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
            title = (data.get("title") or "").strip()[:100]
            desc = (data.get("desc") or data.get("description") or "").strip()[:4900]
            if title:
                log(f"[title-desc] {lang} -> '{title[:60]}'")
                return title, desc
    except Exception as e:
        log(f"[title-desc] LLM error: {e} -> fallback")
    # Fallback kalau LLM gagal
    score = seg.get("score", "?")
    title = f"Clip #{seg.get('idx', '')} - score {score} {('🔥' if lang == 'id' else '🔥')}"
    desc = seg.get("reason", "")
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
def _extract_frames(clip_path, out_dir, n=3):
    """Extract n frame dari clip di t=0.3, t=2, t=mid. Return list of paths."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Probe durasi
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
                       capture_output=True, text=True)
    try:
        dur = float(r.stdout.strip())
    except Exception:
        dur = 5.0
    timestamps = [min(0.3, dur*0.05), min(2.0, dur*0.4), dur*0.5]
    frames = []
    for i, t in enumerate(timestamps):
        fp = out_dir / f"frame_{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(clip_path),
                        "-frames:v", "1", "-q:v", "3", str(fp)],
                       capture_output=True)
        if fp.exists() and fp.stat().st_size > 1000:
            frames.append(fp)
    return frames

def _add_hook_text(frame_path, hook_text, out_path):
    """Tambah hook text + emoticon ke frame pakai PIL.
    Hook text: bold, besar, warna mencolok (kuning/merah), outline hitam.
    """
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(frame_path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    # Cari font bold yang available
    font = None
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux CI
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",  # Windows
        "C:\\Windows\\Fonts\\segoeui.ttf",
    ]
    for fp in font_paths:
        if pathlib.Path(fp).exists():
            try:
                # Font size ~6% dari tinggi (untuk thumbnail 1920 -> 115)
                font = ImageFont.truetype(fp, int(h * 0.06))
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()
    # Wrap text max ~20 char per line
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
    # Render text multi-line, center, di y=15% dari atas (safe area)
    line_h = int(h * 0.08)
    total_h = line_h * len(lines)
    y_start = int(h * 0.15)
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]; th = bbox[3] - bbox[1]
        x = (w - tw) // 2
        y = y_start + i * line_h
        # Outline hitam (8 arah)
        for dx, dy in [(-3, -3), (-3, 3), (3, -3), (3, 3), (-3, 0), (3, 0), (0, -3), (0, 3)]:
            draw.text((x+dx, y+dy), line, font=font, fill=(0, 0, 0))
        # Teks kuning tebal (warna mencolok)
        draw.text((x, y), line, font=font, fill=(255, 230, 0))  # kuning
    img.save(out_path, "JPEG", quality=92)
    return out_path

def gen_thumbnail(clip_path, seg, lang, workdir):
    """Generate thumbnail untuk clip. Returns path to JPG or None.
    1. Extract 3 frame
    2. Try LLM vision (kalau model support image input)
    3. Fallback: pakai frame tengah + hook default dari seg.reason
    """
    workdir = pathlib.Path(workdir)
    frames = _extract_frames(clip_path, workdir / "frames")
    if not frames:
        log("[thumb] gagal extract frame"); return None
    chosen = frames[len(frames) // 2]  # default: frame tengah
    hook_text = seg.get("reason", "TONTON!")[:60] or "TONTON!"
    # Try LLM vision (best-effort, kalau model gak support -> fallback)
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
    # Generate thumbnail
    out = workdir / f"thumb_{pathlib.Path(clip_path).stem}.jpg"
    _add_hook_text(chosen, hook_text, out)
    log(f"[thumb] saved: {out} ({out.stat().st_size}B)")
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
        # debug: hardcode ID test biar gampang jalanin tanpa set env
        video_id = TEST_VIDEO_ID
        log(f"VIDEO_ID kosong -> pakai TEST_VIDEO_ID={video_id}")
    if already_done(video_id):
        log("sudah di-clip, skip"); return
    raw = download_raw(video_id)
    tr = transcribe(raw)
    words = tr.get("words", [])
    segs = pick_segments(tr)
    upload_errors = 0
    for i, seg in enumerate(segs[:MAX_CLIPS]):
        clip = clip_segment(raw, seg, words, i)
        # Detect bahasa: pakai 'language' dari Whisper response, fallback 'en'
        lang = tr.get("language", "en")
        # Generate title/desc engaging via LLM (bhs sesuai video + emoticon)
        title, desc = gen_title_desc(seg, tr, lang)
        # Upload ke YouTube. Kalau limit/gagal, skip tapi jangan stop pipeline
        # (artifact clip + thumbnail sudah ke-render, bisa di-upload manual nanti).
        video_id_yt = None
        try:
            video_id_yt = upload_video(clip, title, desc)
            save_uploaded(video_id_yt, video_id, title, None)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response else 0
            body = ""
            try: body = e.response.text[:200] if e.response else ""
            except Exception: pass
            if "uploadLimitExceeded" in body or code in (400, 429):
                log(f"[upload] SKIP (limit/quota) — artifact clip tetap di WORKDIR, "
                    f"bisa di-upload manual. raw={video_id} seg={i}")
                upload_errors += 1
            else:
                raise  # error lain -> stop pipeline
        except Exception as e:
            log(f"[upload] error: {e} — artifact clip tetap di WORKDIR")
            upload_errors += 1
        # Generate thumbnail dari clip + set di YouTube
        try:
            thumb = gen_thumbnail(clip, seg, lang, WORKDIR)
            if thumb and video_id_yt:
                set_thumbnail(video_id_yt, thumb)
        except Exception as e:
            log(f"[thumb] skip: {e}")
    mark_done(video_id)
    if upload_errors and upload_errors == len(segs[:MAX_CLIPS]):
        log(f"SELESAI (semua upload di-skip, artifact siap di-upload manual)")
    else:
        log("SELESAI")


if __name__ == "__main__":
    main()
