#!/usr/bin/env python3
"""Convert Camoufox/Playwright cookies.json -> Netscape cookies.txt (yt-dlp format).
Usage: python convert_cookies.py INPUT.json OUTPUT.txt
"""
import sys, json

def to_netscape(cookies, out_path):
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        # flag TRUE kalau domain diawali '.' (wildcard), FALSE kalau host-only.
        # domain TIDAK dipaksa tambah '.' -> ikut asli biar cookiejar gak protes.
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        dom = domain
        path = c.get("path", "/") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires")
        # session cookie (expires<0 / None) -> 0 (Netscape invalid kalau -1)
        try:
            exp = int(float(expires)) if expires not in (None, "", "0") else 0
        except Exception:
            exp = 0
        if exp < 0:
            exp = 0
        expires = str(exp)
        name = c.get("name", "")
        value = str(c.get("value", "")).replace("\t", "")
        lines.append("\t".join([dom, flag, path, secure, expires, name, value]))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[convert] {len(cookies)} cookies -> {out_path}")

if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    data = json.load(open(src, encoding="utf-8"))
    # Playwright: list of dict. Camoufox sama.
    convert = data if isinstance(data, list) else data.get("cookies", [])
    to_netscape(convert, dst)
