#!/usr/bin/env python3
"""
get_upload_token.py - Generate YouTube Data API OAuth refresh_token (UPLOAD).

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

Scope yang diminta: youtube.upload (upload video ke channel kamu).
"""
import sys, json, urllib.parse, urllib.request, webbrowser

CLIENT_ID = ""      # <- isi, atau pass arg 1
CLIENT_SECRET = ""  # <- isi, atau pass arg 2
REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def build_auth_url():
    p = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
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
        sys.exit(1)
    url = build_auth_url()
    print("\nBuka URL ini di browser, login, lalu copy 'code':\n")
    print(url, "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    code = input("Paste code di sini: ").strip()
    tok = exchange(code)
    print("\n=== HASIL (masukkan ke GitHub Secrets) ===")
    print("YT_UPLOAD_CLIENT  =", CLIENT_ID)
    print("YT_UPLOAD_SECRET  =", CLIENT_SECRET)
    print("YT_UPLOAD_TOKEN   =", tok.get("refresh_token"))


if __name__ == "__main__":
    main()
