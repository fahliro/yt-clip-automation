#!/usr/bin/env python3
"""
get_upload_token.py - Generate YouTube Data API OAuth refresh_token.

Cara pakai (di mesin kamu, bukan CI):
  1. Google Cloud Console:
       - enable "YouTube Data API v3"
       - OAuth consent screen -> publish (atau add test user)
       - Credentials -> Create OAuth Client ID -> type "Desktop app"
       - copy Client ID + Client Secret
  2. Isi di bawah (atau via argumen), lalu:
       python get_upload_token.py
  3. Browser buka URL, login akun PEMILIK channel, klik Izinkan.
  4. Paste "code" yang muncul -> script print refresh_token.
  5. Masukkan 3 nilai ke GitHub Secrets:
       YT_UPLOAD_CLIENT = client_id
       YT_UPLOAD_SECRET = client_secret
       YT_UPLOAD_TOKEN  = refresh_token

SCOPE:
  Default = youtube.upload + youtube.force-ssl + youtube.readonly.
  - youtube.upload: upload video (POST /videos)
  - youtube.force-ssl: WAJIB untuk thumbnails.set (custom thumbnail).
                        Tanpa scope ini, API return 403.
  - youtube.readonly: dibutuhkan untuk poll_latest() yang pakai videos.list.
  Override via SCOPES env var (space-separated) kalau perlu.
"""
import sys, os, json, urllib.parse, urllib.request, webbrowser

CLIENT_ID = ""      # <- isi, atau pass arg 1
CLIENT_SECRET = ""  # <- isi, atau pass arg 2
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"

# Default scopes — upload + custom thumbnail + read.
# Custom thumbnail (set_thumbnail) butuh youtube.force-ssl.
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def get_scopes():
    env = os.environ.get("SCOPES", "").strip()
    if env:
        return env.split()
    return DEFAULT_SCOPES


def build_auth_url():
    p = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": " ".join(get_scopes()),
        "access_type": "offline",
        "prompt": "consent",  # penting: force generate refresh_token
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(p)


def exchange(code):
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT,
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req))


def main():
    global CLIENT_ID, CLIENT_SECRET
    if len(sys.argv) >= 3:
        CLIENT_ID, CLIENT_SECRET = sys.argv[1], sys.argv[2]
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: isi CLIENT_ID/CLIENT_SECRET di script atau pass arg.")
        print(f"Usage: python {sys.argv[0]} <CLIENT_ID> <CLIENT_SECRET>")
        sys.exit(1)
    scopes = get_scopes()
    print(f"=== Requesting scopes: {scopes}")
    print(f"=== Make sure Google Cloud Console OAuth consent screen includes all of these.")
    print()
    url = build_auth_url()
    print("Buka URL ini di browser, login akun PEMILIK channel, klik Izinkan:\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    code = input("Paste 'code' di sini: ").strip()
    if not code:
        print("ERROR: code kosong"); sys.exit(1)
    try:
        tok = exchange(code)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: token exchange gagal: HTTP {e.code}\n{body}")
        sys.exit(1)
    if "refresh_token" not in tok:
        print(f"ERROR: tidak ada refresh_token di response:\n{json.dumps(tok, indent=2)}")
        print("Hint: pastikan 'access_type=offline' dan 'prompt=consent' di URL auth.")
        sys.exit(1)
    print("\n=== HASIL (paste ke GitHub Secrets) ===")
    print(f"YT_UPLOAD_CLIENT  = {CLIENT_ID}")
    print(f"YT_UPLOAD_SECRET  = {CLIENT_SECRET}")
    print(f"YT_UPLOAD_TOKEN   = {tok['refresh_token']}")
    print(f"\nAccess token (untuk test, expired 1 jam): {tok.get('access_token', '')[:30]}...")
    print(f"Granted scopes: {tok.get('scope', '?')}")


if __name__ == "__main__":
    main()
