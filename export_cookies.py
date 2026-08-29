#!/usr/bin/env python3
"""
export_cookies.py - Auto-export YouTube cookies dari Camoufox (akun PEMILIK channel).

Alur:
  1. Buka YouTube di Camoufox (headless=False).
  2. KAMU yang login / klik "Allow" di browser.
  3. Setelah login, script otomatis:
     - ambil cookies via context.cookies()
     - convert ke Netscape txt
     - encode base64
     - print base64 + tulis ke yt_cookies_b64.txt (gitignored)
  4. Kamu copy base64 -> GitHub Secret YT_COOKIES_TXT.

Jalanin (Python311):
  python.exe export_cookies.py   (pakai interpreter Python311 di mesin ini)
"""
import sys, json, base64, pathlib

PY311 = r"C:\Users\Robby\AppData\Local\Programs\Python\Python311\python.exe"
WORKDIR = pathlib.Path(__file__).resolve().parent


def to_netscape(cookies):
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        # path bisa None
        path = c.get("path") or "/"
        # expires: pakai 0 kalau gak ada / -1 (session)
        exp = c.get("expires")
        try:
            exp = int(exp)
        except (TypeError, ValueError):
            exp = 0
        if exp < 0:
            exp = 0
        name = c.get("name", "")
        value = c.get("value", "")
        # Netscape butuh TAB pemisah (jgn spasi)
        lines.append("\t".join([domain, flag, path, "FALSE" if not c.get("secure") else "TRUE", str(exp), name, value]))
    return "\n".join(lines) + "\n"


def main():
    from camoufox.sync_api import Camoufox

    print("[*] Membuka YouTube di Camoufox (headless=False)...")
    with Camoufox(headless=False) as browser:
        page = browser.new_page()
        page.goto("https://www.youtube.com/")
        print("[*] Silakan LOGIN / klik ALLOW di browser yang muncul.")
        print("[*] Script otomatis detect login (ada cookie SID), lalu lanjut.")

        # Auto-detect: poll cookie SID tiap 3 detik, max 5 menit
        import time
        deadline = time.time() + 300
        sid = None
        while time.time() < deadline:
            cookies = browser.context.cookies()
            sid = next((c for c in cookies if c.get("name") == "SID"), None)
            if sid:
                break
            time.sleep(3)
        if not sid:
            raise SystemExit("[!] 5 menit tanpa login terdeteksi. Tutup & ulangi.")

        # ambil cookies dari context
        yt = [c for c in cookies if "youtube.com" in c.get("domain", "") or "google.com" in c.get("domain", "")]
        if not yt:
            yt = cookies
        print(f"[*] login terdeteksi, ambil {len(yt)} cookies")

        netscape = to_netscape(yt)
        # tulis txt (gitignored)
        txt_path = WORKDIR / "yt_cookies.txt"
        txt_path.write_text(netscape, encoding="utf-8")
        # base64
        b64 = base64.b64encode(netscape.encode("utf-8")).decode("ascii")
        b64_path = WORKDIR / "yt_cookies_b64.txt"
        b64_path.write_text(b64)

        print(f"[*] yt_cookies.txt ({len(netscape)} bytes) + yt_cookies_b64.txt tersimpan.")
        print("[*] BASE64 (copy 1 baris ini ke GitHub Secret YT_COOKIES_TXT):")
        print(b64)


if __name__ == "__main__":
    main()
