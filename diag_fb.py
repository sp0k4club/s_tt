"""
DIAGNOSA FB BOOTSTRAP - jalanin DI VPS buat liat kenapa fb_dtsg ga ke-scrape.
Usage: python diag_fb.py
Output: dump beberapa HTML + analisa di terminal.
"""
import json
import os
import re
import sys

import requests

COOKIES_FILE = "cookies.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def load_cookie_header(path):
    with open(path, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value") is not None]
    return "; ".join(parts), cookies


def main():
    if not os.path.exists(COOKIES_FILE):
        print(f"!! {COOKIES_FILE} ga ada")
        sys.exit(1)

    cookie_header, cookies = load_cookie_header(COOKIES_FILE)
    cu = next((c["value"] for c in cookies if c["name"] == "c_user"), None)
    iu = next((c["value"] for c in cookies if c["name"] == "i_user"), None)
    xs = next((c["value"] for c in cookies if c["name"] == "xs"), None)
    print(f"Cookie: c_user={cu}, i_user={iu}, xs={'ada' if xs else 'GA ADA'}")
    print(f"Total cookie: {len(cookies)}")
    print("=" * 60)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": UA,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_header,
        "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    })

    urls = [
        "https://www.facebook.com/",
        "https://www.facebook.com/me",
        "https://m.facebook.com/",
        "https://mbasic.facebook.com/",
    ]

    dtsg_patterns = [
        (r'"DTSGInitialData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"', "DTSGInitialData"),
        (r'"DTSGInitData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"', "DTSGInitData"),
        (r'name="fb_dtsg"\s+value="([^"]+)"', "input fb_dtsg"),
        (r'"fb_dtsg"\s*:\s*"([^"]+)"', "json fb_dtsg"),
        (r'"token":"([^"]{20,})","async_get_token"', "async_get_token"),
    ]

    for u in urls:
        try:
            r = sess.get(u, timeout=30, allow_redirects=True)
        except Exception as e:
            print(f"\n{u} -> ERROR: {e}")
            continue
        html = r.text or ""
        is_login = "login" in r.url.lower() or "checkpoint" in r.url.lower()
        low = html.lower()
        flags = []
        if is_login:
            flags.append("LOGIN/CHECKPOINT")
        if "captcha" in low:
            flags.append("CAPTCHA")
        if "checkpoint" in low:
            flags.append("checkpoint-text")
        if "/login" in low:
            flags.append("login-link")
        if "you must log in" in low or "log into facebook" in low:
            flags.append("must-login-text")

        print(f"\n{u}")
        print(f"  -> {r.status_code} | final={r.url}")
        print(f"  -> size={len(html)} bytes | flags={flags or 'none'}")

        # cari dtsg
        found = None
        for pat, label in dtsg_patterns:
            m = re.search(pat, html)
            if m:
                found = (label, m.group(1)[:25])
                break
        print(f"  -> fb_dtsg: {found if found else 'GA KETEMU'}")

        # cek kata kunci yg nunjukin halaman bener vs terbatas
        has_user = (cu and cu in html)
        print(f"  -> ada c_user di html: {has_user}")
        print(f"  -> 'DTSGInit' muncul: {'DTSGInit' in html}")
        print(f"  -> 'fb_dtsg' muncul: {'fb_dtsg' in html}")

        # dump HTML pertama yg ga login
        fname = "diag_" + u.replace("https://", "").replace("/", "_").strip("_") + ".html"
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  -> dump: {fname}")
        except Exception:
            pass

    print("\n" + "=" * 60)
    print("KESIMPULAN:")
    print("- Kalo SEMUA 'GA KETEMU' tapi size kecil (<500KB) & ga ada flag LOGIN:")
    print("  => FB kasih halaman terbatas (kemungkinan IP VPS dicurigai).")
    print("- Kalo ada flag LOGIN/CHECKPOINT/CAPTCHA:")
    print("  => cookie ditolak / butuh verifikasi.")
    print("- Kalo 'DTSGInit muncul: True' tapi pattern GA KETEMU:")
    print("  => format HTML beda, kirim file diag_*.html ke gua.")


if __name__ == "__main__":
    main()
