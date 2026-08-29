#!/usr/bin/env python3
"""
preflight.py - Cek semua credential SEBELUM pipeline jalan.
Jalan di awal GitHub Actions (atau lokal) biar kalau ada yang invalid,
ketahuan di awal, bukan error aneh di tengah.

Cek:
  - YT_COOKIES_TXT : base64-decode, Netscape valid, ada SID, belum expired
  - GROQ_API_KEY   : GET /v1/models -> 200
  - LLM_*:         : POST /chat/completions -> 200 (atau 401=key salah)
  - YT_UPLOAD_*:   : tukar refresh_token -> access_token
  - YT_CHANNEL_ID  : akses token bisa baca channel tsb

Keluar code 1 kalau ada credential KRITIS invalid (fail-fast).
"""
import os, base64, sys, time

try:
    import requests
except ImportError:
    requests = None

def log(m): print(f"[preflight] {m}", flush=True)

results = []
def check(name, ok, detail=""):
    results.append((name, ok))
    log(f"{'✅' if ok else '❌'} {name}: {detail}")
    return ok


# ---------------------------------------------------------------- 1. COOKIES
def check_cookies():
    name = "YT_COOKIES_TXT"
    b64 = os.environ.get("YT_COOKIES_TXT", "").strip()
    if not b64:
        return check(name, False, "KOSONG -> set secret (base64 yt_cookies.txt)")
    try:
        raw = base64.b64decode(b64).decode("utf-8")
    except Exception:
        raw = b64
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [l for l in raw.split("\n") if l.strip()]
    if not lines or not lines[0].startswith("# Netscape HTTP Cookie File"):
        return check(name, False, "bukan format Netscape (header salah)")
    if not any("\t" in l for l in lines[1:]):
        return check(name, False, "gak ada TAB (Notepad ubah TAB->spasi?)")
    # cek SID ada
    has_sid = any(l.split("\t")[5:6] == ["SID"] if len(l.split("\t")) > 5 else False for l in lines[1:])
    if not has_sid:
        # fallback: cari substring SID
        has_sid = any("SID" in l for l in lines[1:])
    # cek expired
    now = int(time.time())
    exp_lines = [l for l in lines[1:] if len(l.split("\t")) > 4 and l.split("\t")[4].isdigit()]
    any_expired = any(0 < int(l.split("\t")[4]) < now for l in exp_lines)
    if not has_sid:
        return check(name, False, "Netscape OK tapi gak ada cookie SID (belum login?)")
    if any_expired:
        return check(name, False, "ada cookie EXPIRED (session di-rotate Google)")
    return check(name, True, f"Netscape OK, SID ada, {len(exp_lines)} cookie belum expired")


# ---------------------------------------------------------------- 2. GROQ
def check_groq():
    name = "GROQ_API_KEY"
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return check(name, False, "KOSONG")
    if requests is None:
        return check(name, True, "skip (requests gak ada, di CI ada)")
    r = requests.get("https://api.groq.com/openai/v1/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=20)
    if r.status_code == 200:
        return check(name, True, "200 OK")
    if r.status_code == 401:
        return check(name, False, "401 -> key SALAH/expired")
    return check(name, False, f"HTTP {r.status_code}: {r.text[:120]}")


# ---------------------------------------------------------------- 3. LLM
def check_llm():
    name = "LLM (kilo.ai)"
    base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    if not (base and key and model):
        return check(name, False, "base_url/api_key/model ada yang KOSONG")
    if requests is None:
        return check(name, True, "skip (requests gak ada, di CI ada)")
    r = requests.post(f"{base}/chat/completions",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"model": model, "messages": [{"role": "user", "content": "balas: OK"}]},
                      timeout=30)
    if r.status_code == 200:
        return check(name, True, f"200 OK (model={model})")
    if r.status_code == 401:
        return check(name, False, "401 -> LLM_API_KEY SALAH")
    if r.status_code == 404:
        # kilo.ai: 404 = model_not_found (key benar, model salah)
        return check(name, False, f"404 -> MODEL '{model}' gak ditemukan di provider ini")
    return check(name, False, f"HTTP {r.status_code}: {r.text[:160]}")


# ---------------------------------------------------------------- 4. YT UPLOAD TOKEN
def check_youtube():
    name = "YT_UPLOAD (OAuth)"
    cid = os.environ.get("YT_UPLOAD_CLIENT", "").strip()
    sec = os.environ.get("YT_UPLOAD_SECRET", "").strip()
    tok = os.environ.get("YT_UPLOAD_TOKEN", "").strip()
    ch = os.environ.get("YT_CHANNEL_ID", "").strip()
    if not (cid and sec and tok):
        return check(name, False, "client/secret/token ada yang KOSONG")
    if requests is None:
        return check(name, True, "skip (requests gak ada, di CI ada)")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": sec, "refresh_token": tok,
        "grant_type": "refresh_token"}, timeout=20)
    if r.status_code != 200:
        return check(name, False, f"token exchange gagal HTTP {r.status_code}: {r.text[:120]}")
    access = r.json().get("access_token")
    if not access:
        return check(name, False, "gak dapat access_token")
    # cek channel id valid
    if ch:
        cr = requests.get("https://www.googleapis.com/youtube/v3/channels",
                          params={"id": ch, "part": "id", "mine": "true"},
                          headers={"Authorization": f"Bearer {access}"}, timeout=20)
        if cr.status_code == 200 and cr.json().get("items"):
            return check(name, True, f"token OK, channel {ch} bisa diakses")
        return check(name, False, f"channel {ch} gak bisa diakses (punya token?): {cr.text[:120]}")
    return check(name, True, "token OK (YT_CHANNEL_ID kosong, skip cek channel)")


# ---------------------------------------------------------------- MAIN
def main():
    global results
    results = []  # reset tiap run (aman kalau dipanggil ulang)
    log("=== PREFLIGHT CHECK ALL CREDENTIALS ===")
    c1 = check_cookies()
    c2 = check_groq()
    c3 = check_llm()
    c4 = check_youtube()
    log("=== RINGKASAN ===")
    failed = [n for n, ok in results if not ok]
    if failed:
        log(f"GAGAL: {', '.join(failed)}")
        log("Perbaiki secret di GitHub -> re-run. Pipeline dihentikan (fail-fast).")
        sys.exit(1)
    log("SEMUA CREDENTIAL VALID ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
