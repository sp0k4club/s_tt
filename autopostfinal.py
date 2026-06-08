"""
============================================================
  TIKTOK -> FACEBOOK REELS AUTO POSTER  (final)
============================================================
Flow:
  - Sumber video: file urls.txt / scrape username / grab username->urls.txt
  - Download via ssstik (video & photo carousel -> MP4)
  - Caption asli dari TikTok oEmbed
  - Upload + publish ke FB Reels fanspage (cookie-only, no token)
  - Urut terlama -> terbaru, skip yg udah di-post, hapus file setelah post

Pakai:
  python autopostfinal.py                       (menu interaktif)
  python autopostfinal.py urls [file.txt]       (dari file)
  python autopostfinal.py username <name>       (scrape profil)
  python autopostfinal.py grab <name> [file]    (grab -> file -> proses)
"""

import os
import re
import sys
import json
import time
import uuid
import hashlib
import secrets
from pathlib import Path
from typing import Optional

import requests

# cloudscraper opsional - buat bypass Cloudflare di ssstik
try:
    import cloudscraper

    _SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
except ImportError:
    _SCRAPER = None

# Playwright - cuma dibutuhin buat mode username/grab (scrape profil)
try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

# =========================
#  KONFIGURASI
# =========================
DELAY_MENIT = 3            # delay antar post video baru (menit). Aman: 15-30
MAX_VIDEO = 0             # 0 = semua, isi angka buat limit
COOKIES_FILE = "cookies.json"
DOWNLOAD_DIR = "downloads"
PAGE_ID = ""              # kosongin = auto dari cookie i_user
POSTED_FILE = "posted.txt"   # catatan video yg udah di-post (jangan dihapus!)
DELETE_AFTER_POST = True     # hapus MP4 setelah sukses post (hemat disk VPS)
URLS_FILE = "urls.txt"
PLAYWRIGHT_HEADLESS = False  # False = browser keliatan (bisa solve captcha manual)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
COMMON_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tikwm.com/",
}


# =========================
#  WARNA & TAMPILAN (ANSI)
# =========================
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    GRAY = "\033[90m"


# Aktifin ANSI + paksa UTF-8 di Windows (biar emoji & box-drawing ga error)
if os.name == "nt":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _c(text, color):
    return f"{color}{text}{C.RESET}"


def banner():
    art = r"""
   ______ ____     ____  ___  ____  ______
  /_  __// __ \   / __/ / _ )/ __ \/ __/ /
   / /  / /_/ /  / _/  / _  / /_/ /\ \/_/
  /_/   \____/  /_/   /____/\____/___/(_)
"""
    print(_c(art, C.CYAN + C.BOLD))
    print(_c("  ╔═══════════════════════════════════════════════╗", C.MAGENTA))
    print(_c("  ║", C.MAGENTA) + _c("   TIKTOK → FACEBOOK REELS  AUTO POSTER", C.WHITE + C.BOLD) + _c("       ║", C.MAGENTA))
    print(_c("  ║", C.MAGENTA) + _c("   reels engine  •  edition 2026", C.GRAY) + _c("                ║", C.MAGENTA))
    print(_c("  ╚═══════════════════════════════════════════════╝", C.MAGENTA))
    print()


def hr():
    print(_c("  " + "─" * 50, C.GRAY))


def log_info(msg):
    print(f"  {_c('ℹ', C.BLUE)}  {msg}")


def log_ok(msg):
    print(f"  {_c('✓', C.GREEN + C.BOLD)}  {_c(msg, C.GREEN)}")


def log_warn(msg):
    print(f"  {_c('!', C.YELLOW + C.BOLD)}  {_c(msg, C.YELLOW)}")


def log_err(msg):
    print(f"  {_c('✗', C.RED + C.BOLD)}  {_c(msg, C.RED)}")


def log_step(tag, msg):
    print(f"  {_c('▸', C.CYAN)} {_c(f'[{tag}]', C.CYAN + C.BOLD)} {msg}")


def log_dim(msg):
    print(f"     {_c(msg, C.GRAY)}")


def header_video(i, total, vid_id):
    bar = _c("━" * 50, C.MAGENTA)
    print()
    print(f"  {bar}")
    print(f"  {_c('🎬', C.WHITE)} {_c(f'[{i}/{total}]', C.YELLOW + C.BOLD)} {_c(vid_id, C.WHITE + C.BOLD)}")
    print(f"  {bar}")


# =========================
#  TIKTOK CAPTION (oEmbed)
# =========================
def fetch_caption(video_url: str) -> str:
    """Caption asli TikTok via oEmbed (zero browser, no login)."""
    try:
        clean = video_url.split("?")[0].replace("/photo/", "/video/")
        r = requests.get(
            "https://www.tiktok.com/oembed",
            params={"url": clean},
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        if r.status_code != 200:
            return ""
        return (r.json().get("title") or "").strip()
    except Exception:
        return ""


# =========================
#  SUMBER 1: FILE urls.txt
# =========================
def get_videos_from_file(urls_file: str, skip_ids: set = None):
    """Baca URL TikTok dari file (1 per baris). Skip yg udah di-post."""
    if not os.path.exists(urls_file):
        raise RuntimeError(f"File {urls_file} ga ada")

    skip_ids = skip_ids or set()
    items, seen = [], set()
    already = 0
    with open(urls_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.search(r"/(video|photo)/(\d{10,25})", line)
            if not m:
                m2 = re.search(r"(\d{15,25})", line)
                if not m2:
                    continue
                vid_id = m2.group(1)
                full_url = line if line.startswith("http") else f"https://www.tiktok.com/@x/video/{vid_id}"
            else:
                vid_id = m.group(2)
                full_url = line.split("?")[0]
            if vid_id in seen:
                continue
            seen.add(vid_id)
            if vid_id in skip_ids:
                already += 1
                continue
            items.append({"id": vid_id, "title": "",
                          "url_no_wm": f"SSSTIK:{full_url}", "_page_url": full_url})

    if already:
        log_dim(f"{already} video udah pernah di-post → di-skip")
    return items


# =========================
#  SUMBER 2: SCRAPE PROFIL (Playwright)
# =========================
def _clean_alt_caption(alt: str) -> str:
    if not alt:
        return ""
    return re.sub(r"\s+created by\b.*$", "", alt, flags=re.IGNORECASE | re.DOTALL).strip()


def _build_caption_map(html: str) -> dict:
    """Mapping {video_id: caption_desc} dari JSON hydration TikTok."""
    cap = {}
    m = re.search(r'<script id="SIGI_STATE"[^>]*>(.+?)</script>', html, re.DOTALL)
    if m:
        try:
            st = json.loads(m.group(1))
            for vid, it in (st.get("ItemModule") or {}).items():
                if it.get("desc"):
                    cap[str(vid)] = it["desc"]
        except Exception:
            pass
    if not cap:
        for m in re.finditer(r'"id":"(\d{15,25})"[^{]*?"desc":"((?:[^"\\]|\\.)*)"', html):
            try:
                cap[m.group(1)] = json.loads('"' + m.group(2) + '"')
            except Exception:
                continue
    return cap


def _inject_tiktok_cookies(context):
    """Inject cookie TikTok (tiktok_cookies.txt) -> kurangi captcha."""
    path = "tiktok_cookies.txt"
    if not os.path.exists(path):
        return
    try:
        cookies = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif not line or line.startswith("#"):
                    continue
                p = line.split("\t")
                if len(p) < 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = p[:7]
                cookies.append({"name": name, "value": value, "domain": domain,
                                "path": cpath or "/", "secure": secure.upper() == "TRUE",
                                "expires": int(expiry) if expiry.isdigit() else -1})
        if cookies:
            context.add_cookies(cookies)
            log_dim(f"inject {len(cookies)} cookie TikTok (anti-captcha)")
    except Exception as e:
        log_dim(f"gagal inject cookie TikTok: {e}")


def _wait_if_captcha(page):
    try:
        html = page.content().lower()
    except Exception:
        return
    if "captcha" not in html and "/verify" not in page.url.lower():
        return
    if PLAYWRIGHT_HEADLESS:
        log_warn("KENA CAPTCHA (headless). Set PLAYWRIGHT_HEADLESS=False / pakai mode urls.")
        return
    log_warn("KENA CAPTCHA! Selesaiin di browser, lalu tekan ENTER di sini ...")
    try:
        input()
    except Exception:
        page.wait_for_timeout(15000)


def scrape_profile(username: str, max_video: int = 0):
    """Scrape semua URL video/photo dari profil TikTok pakai Playwright."""
    if not _PLAYWRIGHT_OK:
        raise RuntimeError("Playwright ga ada. pip install playwright && playwright install chromium")

    username = username.lstrip("@").strip()
    url = f"https://www.tiktok.com/@{username}"
    log_step("scrape", f"buka browser → {url}")

    seen, items = set(), []
    profile_dir = Path(".pw_profile").absolute()
    profile_dir.mkdir(exist_ok=True)
    stealth = (
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
        "window.chrome={runtime:{}};"
    )

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=PLAYWRIGHT_HEADLESS,
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage"],
        )
        ctx.add_init_script(stealth)
        _inject_tiktok_cookies(ctx)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            ctx.close()
            raise RuntimeError(f"goto fail: {e}")

        _wait_if_captcha(page)
        try:
            page.wait_for_selector('a[href*="/video/"], a[href*="/photo/"]', timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        scroll_target = max(max_video * 2, 30) if max_video else 1000
        last_count, stale = 0, 0
        for i in range(scroll_target):
            anchors = page.query_selector_all(
                f'a[href*="/@{username}/video/"], a[href*="/@{username}/photo/"]'
            )
            for a in anchors:
                href = a.get_attribute("href") or ""
                m = re.search(rf"/@{re.escape(username)}/(video|photo)/(\d{{10,25}})",
                              href, re.IGNORECASE)
                if not m:
                    continue
                path, vid_id = m.group(1), m.group(2)
                if vid_id in seen:
                    continue
                seen.add(vid_id)
                caption = ""
                try:
                    img = a.query_selector("img[alt]")
                    if img:
                        caption = _clean_alt_caption((img.get_attribute("alt") or "").strip())
                except Exception:
                    pass
                items.append({"id": vid_id, "title": caption,
                              "url_no_wm": f"SSSTIK:https://www.tiktok.com/@{username}/{path}/{vid_id}"})
            if max_video and len(items) >= max_video:
                break
            page.mouse.wheel(0, 5000)
            page.wait_for_timeout(2500)
            if len(items) == last_count:
                stale += 1
                log_dim(f"scroll {i+1}: {len(items)} video (ga nambah {stale}/5)")
                if stale >= 5:
                    log_dim(f"mentok di {len(items)} video, stop scroll")
                    break
            else:
                stale = 0
                log_dim(f"scroll {i+1}: {len(items)} video")
            last_count = len(items)

        try:
            cap_map = _build_caption_map(page.content())
            for it in items:
                if cap_map.get(it["id"]):
                    it["title"] = cap_map[it["id"]]
        except Exception:
            pass
        ctx.close()

    if not items:
        raise RuntimeError("ga ketemu video (profil private/kosong/captcha)")
    if max_video:
        items = items[:max_video]
    return items


def grab_to_file(username: str, urls_file: str, max_video: int = 0) -> int:
    """Grab URL dari profil -> append ke urls_file (skip yg udah ada)."""
    username = username.lstrip("@").strip()
    log_step("grab", f"ambil URL dari @{username}")
    items = scrape_profile(username, max_video)

    existing = set()
    if os.path.exists(urls_file):
        with open(urls_file, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r"/(?:video|photo)/(\d{10,25})", line)
                if m:
                    existing.add(m.group(1))

    new_lines = []
    for it in items:
        if it["id"] in existing:
            continue
        new_lines.append(it["url_no_wm"].replace("SSSTIK:", "", 1))
        existing.add(it["id"])

    if new_lines:
        with open(urls_file, "a", encoding="utf-8") as f:
            if os.path.exists(urls_file) and os.path.getsize(urls_file) > 0:
                f.write("\n")
            f.write("\n".join(new_lines) + "\n")
    log_ok(f"{len(new_lines)} URL baru → {urls_file} (total grab: {len(items)})")
    return len(new_lines)


# =========================
#  DOWNLOAD via ssstik
# =========================
def ssstik_resolve(video_url: str):
    """Resolve URL TikTok -> MP4 URL via ssstik. Return ('url', mp4_url) / None."""
    sess = _SCRAPER if _SCRAPER is not None else requests.Session()
    try:
        home = sess.get("https://ssstik.io/", headers=COMMON_HEADERS, timeout=20).text
        m = re.search(r"s_tt\s*=\s*['\"]([A-Za-z0-9+/=_-]+)['\"]", home)
        tt = m.group(1) if m else "SzVUZTk3"

        r = sess.post(
            "https://ssstik.io/abc?url=dl",
            data={"id": video_url, "locale": "en", "tt": tt},
            headers={**COMMON_HEADERS, "HX-Request": "true",
                     "HX-Current-URL": "https://ssstik.io/", "HX-Target": "target",
                     "HX-Trigger": "_gcaptcha_pt", "Origin": "https://ssstik.io",
                     "Referer": "https://ssstik.io/",
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if r.status_code != 200 or len(r.text) < 50:
            return None
        html = r.text

        # Photo carousel -> POST slides_data, ambil MP4 dari header hx-redirect
        m_slides = re.search(r'name=["\']slides_data["\']\s+value=["\']([^"\']+)["\']', html)
        if not m_slides:
            m_slides = re.search(r'slides_data\s*=\s*["\']([^"\']+)["\']', html)
        if m_slides:
            log_dim("photo carousel → compile video di server ssstik ...")
            r2 = sess.post(
                "https://r.ssstik.top/b/index.sh",
                data={"slides_data": m_slides.group(1)},
                headers={**COMMON_HEADERS, "HX-Request": "true",
                         "HX-Current-URL": "https://ssstik.io/",
                         "HX-Target": "slides_generate", "HX-Trigger": "slides_generate",
                         "Origin": "https://ssstik.io", "Referer": "https://ssstik.io/",
                         "Content-Type": "application/x-www-form-urlencoded"},
                timeout=180, allow_redirects=False,
            )
            mp4 = (r2.headers.get("hx-redirect") or r2.headers.get("HX-Redirect")
                   or r2.headers.get("Location"))
            if mp4:
                return ("url", mp4)
            return None

        # Video biasa -> href tikcdn MP4
        m = re.search(r'href="(https://tikcdn\.io/ssstik/[^"]+)"', html)
        if m:
            return ("url", m.group(1))
        return None
    except Exception as e:
        log_dim(f"ssstik error: {e}")
        return None


def safe_filename(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen] or "video").strip()


def download_video(video: dict, outdir: Path) -> Optional[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    fname = f"{video['id']}_{safe_filename(video.get('title', ''))}.mp4"
    path = outdir / fname
    if path.exists() and path.stat().st_size > 0:
        log_dim(f"udah ada, skip download: {fname}")
        return path

    webpage_url = video["url_no_wm"].replace("SSSTIK:", "", 1)
    result = ssstik_resolve(webpage_url)
    if not result and "/video/" in webpage_url:
        result = ssstik_resolve(webpage_url.replace("/video/", "/photo/"))
    if not result:
        return None

    mp4_url = result[1]
    try:
        with requests.get(mp4_url, stream=True, timeout=180,
                          headers={"User-Agent": USER_AGENT,
                                   "Referer": "https://ssstik.io/"}) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        log_dim(f"download OK ({path.stat().st_size // 1024} KB)")
        return path
    except Exception as e:
        log_dim(f"download gagal: {e}")
        path.unlink(missing_ok=True)
        return None


# =========================
#  FACEBOOK CLIENT
# =========================
class FBClient:
    def __init__(self, cookies_path: str):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "image/avif,image/webp,image/apng,*/*;q=0.8"),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",  # JANGAN tambah 'br' (VPS sering ga bisa decode)
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
        self._load_cookies(cookies_path)
        self.user_id = self.i_user = self.fb_dtsg = None
        self.jazoest = self.lsd = self.spin_r = self.spin_t = self.hsi = None

    def _load_cookies(self, path: str):
        if not os.path.exists(path):
            log_err(f"File cookie ga ada: {path}")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        parts = [f"{c['name']}={c['value']}" for c in cookies
                 if c.get("name") and c.get("value") is not None]
        self.session.headers["Cookie"] = "; ".join(parts)
        for c in cookies:
            try:
                self.session.cookies.set(c["name"], c["value"],
                                         domain=c.get("domain", ".facebook.com"),
                                         path=c.get("path", "/"))
            except Exception:
                pass

    def bootstrap(self):
        log_step("FB", "bootstrap - scrape token ...")
        cu = self.session.cookies.get("c_user")
        if not cu:
            log_err("Cookie c_user ga ada - login gagal.")
            sys.exit(1)
        self.user_id = cu
        self.i_user = self.session.cookies.get("i_user") or None

        candidates = [
            "https://business.facebook.com/latest/home",
            "https://www.facebook.com/",
            "https://www.facebook.com/me",
            "https://m.facebook.com/",
        ]
        html = ""
        for u in candidates:
            try:
                r = self.session.get(u, timeout=30, allow_redirects=True)
                is_login = "login" in r.url.lower() or "checkpoint" in r.url.lower()
                has_dtsg = bool(r.text and ("DTSGInit" in r.text or "fb_dtsg" in r.text))
                log_dim(f"GET {u.split('//')[1][:35]} → {r.status_code} "
                        f"({len(r.text)}b) dtsg={'Y' if has_dtsg else 'N'}"
                        f"{' [LOGIN]' if is_login else ''}")
                if is_login:
                    continue
                if r.text and len(r.text) > 5000 and has_dtsg:
                    html = r.text
                    break
            except Exception as e:
                log_dim(f"{u} err: {e}")
                continue

        if not html:
            log_err("Semua URL FB gagal / kena login.")
            log_dim("Cookie expired? Export ulang dari Chrome (login + switch fanspage).")
            log_dim(f"c_user={self.user_id}, "
                    f"xs={'ada' if self.session.cookies.get('xs') else 'GA ADA'}, "
                    f"i_user={self.i_user or 'ga ada'}")
            sys.exit(1)

        self.fb_dtsg = self._extract(html, [
            r'"DTSGInitialData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
            r'"DTSGInitData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
            r'name="fb_dtsg"\s+value="([^"]+)"',
            r'"fb_dtsg"\s*:\s*"([^"]+)"',
            r'"token":"([^"]+)","async_get_token"',
        ])
        self.jazoest = self._extract(html, [r'name="jazoest"\s+value="([^"]+)"',
                                            r'"jazoest"\s*:\s*"([^"]+)"', r"&jazoest=(\d+)"])
        self.lsd = self._extract(html, [
            r'"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
            r'name="lsd"\s+value="([^"]+)"', r'"lsd"\s*:\s*"([^"]+)"'])
        self.spin_r = self._extract(html, [r'"__spin_r"\s*:\s*(\d+)'])
        self.spin_t = self._extract(html, [r'"__spin_t"\s*:\s*(\d+)'])
        self.hsi = self._extract(html, [r'"hsi"\s*:\s*"(\d+)"'])

        if not self.fb_dtsg:
            Path("fb_debug.html").write_text(html, encoding="utf-8")
            log_err("Gagal ambil fb_dtsg (HTML di-dump ke fb_debug.html).")
            sys.exit(1)
        log_ok(f"login OK - user={self.user_id}  fb_dtsg=ok  "
               f"lsd={'ok' if self.lsd else '-'}  jazoest={'ok' if self.jazoest else '-'}")

    def _extract(self, html, patterns):
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)
        return None

    def _common(self, req_num: int, actor_id: str) -> dict:
        return {
            "__user": str(actor_id or self.user_id or "0"), "__a": "1",
            "__req": str(req_num), "__hs": "20611.HYP:comet_pkg.2.1...0", "dpr": "1",
            "__ccg": "EXCELLENT", "__rev": self.spin_r or "", "__s": secrets.token_hex(4),
            "__hsi": self.hsi or "", "__comet_req": "15", "fb_dtsg": self.fb_dtsg,
            "jazoest": self.jazoest or "", "lsd": self.lsd or "",
            "__spin_r": self.spin_r or "", "__spin_b": "trunk", "__spin_t": self.spin_t or "",
            "__crn": "comet.fbweb.CometHomeRoute", "qpl_active_flow_ids": "884152905",
        }

    def _ajax_headers(self, friendly=None) -> dict:
        h = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
             "Content-Type": "application/x-www-form-urlencoded",
             "Origin": "https://www.facebook.com", "Referer": "https://www.facebook.com/",
             "X-FB-LSD": self.lsd or "", "Sec-Fetch-Dest": "empty",
             "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
        if friendly:
            h["X-FB-Friendly-Name"] = friendly
        return h

    def upload_video(self, video_path: Path, page_id: str) -> str:
        size = video_path.stat().st_size
        wf = str(uuid.uuid4())

        # Step 1: register
        log_step("FB", f"[1/3] register upload ({size/1024/1024:.2f} MB)")
        r = self.session.post(
            f"https://vupload-edge.facebook.com/ajax/video/upload/requests/start/?av={page_id}&__a=1",
            data={"waterfall_id": wf, "target_id": page_id, "source": "reel_composer",
                  "composer_entry_point_ref": "comet_ap_plus_reel_composer_feed_sprout",
                  "supports_chunking": "true", "supports_file_api": "true",
                  "file_size": str(size), "file_extension": "mp4",
                  "partition_start_offset": "0", "partition_end_offset": str(size),
                  "has_file_been_replaced": "false", "__aaid": "0",
                  **self._common(14, page_id)},
            headers={**self._ajax_headers(), "x_fb_video_waterfall_id": wf,
                     "Sec-Fetch-Site": "same-site"}, timeout=60,
        )
        resp = self._json(r, "start")
        payload = resp.get("payload") or resp
        video_id = str(payload.get("video_id") or payload.get("videoID") or "")
        upload_url = payload.get("upload_url")
        if not video_id:
            raise RuntimeError(f"/start/ ga return video_id: {str(resp)[:300]}")
        if not upload_url:
            md5 = self._md5(video_path)
            upload_url = f"https://rupload-cgk1-1.up.facebook.com/fb_video/{md5}-0-{size}"

        # Step 2: upload binary
        log_step("FB", "[2/3] upload binary")
        sep = "&" if "?" in upload_url else "?"
        qs = {"__aaid": "0", **self._common(44, page_id), "fb_dtsg_ag": self.fb_dtsg}
        upload_url += sep + "&".join(
            f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in qs.items())
        data = video_path.read_bytes()
        r = self.session.post(upload_url, data=data, timeout=600, headers={
            "User-Agent": USER_AGENT, "Accept": "*/*", "Content-Type": "application/octet-stream",
            "Offset": "0", "X-Entity-Length": str(size),
            "X-Entity-Name": f"{video_id}_{int(time.time())}.mp4", "X-Entity-Type": "video/mp4",
            "Origin": "https://www.facebook.com", "Referer": "https://www.facebook.com/",
            "Sec-Fetch-Site": "same-site"})
        if r.status_code != 200:
            raise RuntimeError(f"binary upload HTTP {r.status_code}: {r.text[:300]}")
        try:
            bresp = r.json()
        except Exception:
            bresp = {}

        # Step 3: finalize
        log_step("FB", "[3/3] finalize")
        chunk = bresp.get("h") or bresp.get("fbuploader_video_file_chunk") or ""
        if not chunk:
            fn = self._b64url(f"{video_id}_{video_path.stem[:50]}.mp4")
            chunk = f"1:{fn}:video/mp4:{video_id}"
        r = self.session.post(
            f"https://vupload-edge.facebook.com/ajax/video/upload/requests/receive/?av={page_id}&__a=1",
            data={"waterfall_id": wf, "target_id": page_id, "video_id": video_id,
                  "source": "reel_composer",
                  "composer_entry_point_ref": "comet_ap_plus_reel_composer_feed_sprout",
                  "supports_chunking": "true", "supports_upload_service": "true",
                  "partition_start_offset": "0", "partition_end_offset": str(size),
                  "start_offset": "0", "end_offset": str(size), "upload_speed": "2000000",
                  "fbuploader_video_file_chunk": chunk, "has_file_been_replaced": "false",
                  "__aaid": "0", **self._common(46, page_id)},
            headers={**self._ajax_headers(), "x_fb_video_waterfall_id": wf,
                     "Sec-Fetch-Site": "same-site"}, timeout=120,
        )
        rresp = self._json(r, "receive")
        if rresp.get("error"):
            raise RuntimeError(f"/receive/ error: {rresp.get('errorSummary') or rresp}")
        return video_id

    def publish_reel(self, video_id: str, caption: str, page_id: str) -> dict:
        log_step("FB", "publish reels")
        idem = str(uuid.uuid4())
        variables = {
            "input": {
                "composer_entry_point": "comet_ap_plus_reel_composer_feed_sprout",
                "composer_source_surface": "short_form_video",
                "idempotence_token": f"{idem}_FEED", "source": "WWW",
                "attachments": [{"video": {
                    "audio_descriptions": None, "id": str(video_id),
                    "additional_video_metadata": {
                        "translatedAudioMetadata": [],
                        "autoGenCaptionsSettings": {"autogenerate_captions_enabled": True,
                                                    "should_review_all_captions": False}},
                    "notify_when_processed": True, "transcriptions": None,
                    "was_created_via_unified_video_flow": {"was_created_via_unified_video_flow": True},
                    "story_media_audio_data": {"raw_media_type": "VIDEO"},
                    "video_media_metadata": {
                        "audio": {"audio_type": "original_audio", "start_time_s": 0, "volume_level": 1},
                        "is_audio_muted": False, "length_in_sec": 30}}}],
                "fb_shorts": {"has_overridden_video_format": True, "is_fb_short": False,
                              "remix_status": "DISABLED"},
                "post_publish_story_data": {"reshare_post_as_sticker": "DISABLED"},
                "message": {"ranges": [], "text": caption or ""},
                "audience": {"privacy": {"allow": [], "base_state": "EVERYONE", "deny": [],
                                         "tag_expansion_state": "UNSPECIFIED"}},
                "with_tags_ids": None, "reels_remix": {"remix_status": "ENABLED"},
                "stars_receivable": {"is_receiving_stars_disabled": False},
                "logging": {"composer_session_id": idem},
                "navigation_data": {"attribution_id_v2": ""}, "tracking": [None],
                "event_share_metadata": {"surface": "timeline"}, "actor_id": str(page_id),
                "client_mutation_id": str(secrets.randbelow(100))},
            "feedLocation": "NEWSFEED", "feedbackSource": 1, "focusCommentID": None,
            "gridMediaWidth": None, "groupID": None, "scale": 1,
            "privacySelectorRenderLocation": "COMET_STREAM",
            "checkPhotosToReelsUpsellEligibility": False, "referringStoryRenderLocation": None,
            "renderLocation": "homepage_stream", "useDefaultActor": False,
            "inviteShortLinkKey": None, "isFeed": True, "isFundraiser": False,
            "isFunFactPost": False, "isGroup": False, "isEvent": False, "isTimeline": False,
            "isSocialLearning": False, "isPageNewsFeed": False, "isProfileReviews": False,
            "isWorkSharedDraft": False,
            "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
            "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
            "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
            "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
            "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
            "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
            "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": False,
            "__relay_internal__pv__IsWorkUserrelayprovider": False,
            "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
            "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
            "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
            "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
            "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,
            "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
            "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": True,
            "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
            "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
            "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
            "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
            "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
            "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
            "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
            "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": True,
            "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": True,
            "__relay_internal__pv__GroupsCometGYSJFeedItemHeightrelayprovider": 206,
            "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
            "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": False,
            "__relay_internal__pv__groups_comet_use_glvrelayprovider": False,
            "__relay_internal__pv__GHLShouldChangeSponsoredAuctionDistanceFieldNamerelayprovider": False,
            "__relay_internal__pv__GHLShouldUseSponsoredAuctionLabelFieldNameV1relayprovider": False,
            "__relay_internal__pv__GHLShouldUseSponsoredAuctionLabelFieldNameV2relayprovider": False,
        }
        form = {"av": page_id, "__aaid": "0", **self._common(63, page_id),
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "ComposerStoryCreateMutation",
                "variables": json.dumps(variables), "server_timestamps": "true",
                "doc_id": "26950268144643228"}
        headers = self._ajax_headers("ComposerStoryCreateMutation")
        headers["Referer"] = f"https://www.facebook.com/profile.php?id={page_id}"
        r = self.session.post("https://www.facebook.com/api/graphql/", data=form,
                              headers=headers, timeout=120)
        resp = self._json(r, "publish")
        if resp.get("errors"):
            raise RuntimeError(f"Publish error: {resp['errors']}")
        if resp.get("error"):
            raise RuntimeError(f"Publish error: {resp.get('errorSummary') or resp}")
        return resp

    def _json(self, r, label):
        if r.status_code != 200:
            raise RuntimeError(f"/{label}/ HTTP {r.status_code}: {r.text[:300]}")
        text = r.text.strip()
        if text.startswith("for (;;);"):
            text = text[len("for (;;);"):]
        try:
            return json.loads(text)
        except Exception:
            pass
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except Exception:
                    continue
        raise RuntimeError(f"/{label}/ bukan JSON: {text[:300]}")

    @staticmethod
    def _md5(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _b64url(s: str) -> str:
        import base64
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# =========================
#  POSTED TRACKING
# =========================
def load_posted() -> set:
    posted = set()
    if os.path.exists(POSTED_FILE):
        with open(POSTED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                vid = line.strip().split("\t")[0].strip()
                if vid and not vid.startswith("#"):
                    posted.add(vid)
    return posted


def mark_posted(video_id: str, caption: str = ""):
    with open(POSTED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{video_id}\t{(caption or '')[:50]}\n")


# =========================
#  MAIN
# =========================
def main():
    banner()

    # --- Pilih mode (CLI args atau menu interaktif) ---
    args = sys.argv[1:]
    urls_file, grab_username, mode, username = URLS_FILE, None, None, "from_urls"
    if args:
        if args[0] == "urls":
            mode = "urls"
            urls_file = args[1] if len(args) > 1 else URLS_FILE
        elif args[0] == "username" and len(args) > 1:
            mode, username = "username", args[1].lstrip("@")
        elif args[0] == "grab" and len(args) > 1:
            mode, grab_username = "urls", args[1].lstrip("@")
            urls_file = args[2] if len(args) > 2 else URLS_FILE
        else:
            log_err("Usage: python autopostfinal.py [urls <file> | username <name> | grab <name> [file]]")
            sys.exit(1)
    else:
        print(_c("  Pilih sumber video TikTok:", C.WHITE + C.BOLD))
        print(f"    {_c('1', C.CYAN + C.BOLD)}  File urls.txt   {_c('(zero browser, cocok VPS)', C.GRAY)}")
        print(f"    {_c('2', C.CYAN + C.BOLD)}  Username        {_c('(scrape profil, pakai browser)', C.GRAY)}")
        print(f"    {_c('3', C.CYAN + C.BOLD)}  Grab → urls.txt {_c('(grab dulu, terus proses)', C.GRAY)}")
        pilih = input(f"\n  {_c('➜', C.MAGENTA)} pilihan [1/2/3] (default 1): ").strip() or "1"
        if pilih == "2":
            mode = "username"
            username = input(f"  {_c('➜', C.MAGENTA)} username TikTok (tanpa @): ").strip()
            if not username:
                log_err("username kosong"); sys.exit(1)
        elif pilih == "3":
            mode = "urls"
            grab_username = input(f"  {_c('➜', C.MAGENTA)} username buat grab (tanpa @): ").strip()
            if not grab_username:
                log_err("username kosong"); sys.exit(1)
            urls_file = input(f"  {_c('➜', C.MAGENTA)} simpan ke (default {URLS_FILE}): ").strip() or URLS_FILE
        else:
            mode = "urls"
            urls_file = input(f"  {_c('➜', C.MAGENTA)} file URL (default {URLS_FILE}): ").strip() or URLS_FILE
    print()
    hr()

    # --- Grab dulu (mode 3) ---
    if grab_username:
        try:
            grab_to_file(grab_username, urls_file, MAX_VIDEO)
        except Exception as e:
            log_warn(f"grab gagal: {e} - lanjut proses file yg ada ...")
        hr()

    # --- Bootstrap FB ---
    fb = FBClient(COOKIES_FILE)
    fb.bootstrap()
    page_id = PAGE_ID.strip() if PAGE_ID else fb.i_user
    if not page_id:
        log_err("Ga ada PAGE_ID & cookie i_user. Switch ke fanspage dulu, export cookie ulang.")
        sys.exit(1)
    if page_id == fb.user_id:
        log_warn("page_id == user_id - belum switch fanspage? Bakal post ke profil pribadi.")
    log_info(f"target fanspage: {_c(page_id, C.WHITE + C.BOLD)}")
    hr()

    # --- Ambil daftar video ---
    posted = load_posted()
    if mode == "urls":
        videos = get_videos_from_file(urls_file, skip_ids=posted)
    else:
        videos = scrape_profile(username, MAX_VIDEO)
        if posted:
            before = len(videos)
            videos = [v for v in videos if v["id"] not in posted]
            if before - len(videos):
                log_dim(f"{before - len(videos)} video udah di-post → di-skip")
    if not videos:
        log_ok("Ga ada video baru. Semua udah di-post.")
        sys.exit(0)

    # Urut terlama -> terbaru (ID kecil = lama)
    try:
        videos.sort(key=lambda v: int(v["id"]))
    except Exception:
        pass
    if MAX_VIDEO:
        videos = videos[:MAX_VIDEO]

    total = len(videos)
    outdir = Path(DOWNLOAD_DIR) / username
    log_info(f"{_c(str(total), C.GREEN + C.BOLD)} video baru, urut {_c('terlama → terbaru', C.YELLOW)}")
    log_info(f"delay antar post: {_c(str(DELAY_MENIT) + ' menit', C.YELLOW)}")
    hr()

    # --- Loop: download -> upload -> publish -> delay ---
    success = failed = consecutive_fail = 0
    for i, v in enumerate(videos, start=1):
        header_video(i, total, v["id"])

        # caption (oEmbed) - fetch per-video, hindari rate-limit
        if not v.get("title") and v.get("_page_url"):
            cap = fetch_caption(v["_page_url"])
            if cap:
                v["title"] = cap
        cap_show = v.get("title") or "(kosong)"
        log_step("caption", _c(cap_show[:65], C.WHITE))

        # download
        vpath = download_video(v, outdir)
        if not vpath:
            log_err(f"gagal download (URL salah/dihapus/private) → skip")
            failed += 1
            continue

        # upload + publish
        try:
            vid = fb.upload_video(vpath, page_id)
            fb.publish_reel(vid, v.get("title", ""), page_id)
            log_ok(f"POSTED → {vpath.name}")
            success += 1
            consecutive_fail = 0
            mark_posted(v["id"], v.get("title", ""))
            if DELETE_AFTER_POST:
                vpath.unlink(missing_ok=True)
                log_dim("file dihapus (hemat disk)")
        except Exception as e:
            log_err(f"gagal posting: {e}")
            failed += 1
            consecutive_fail += 1
            if consecutive_fail >= 3:
                hr()
                log_err(f"STOP - gagal 3x berturut (cookie FB expired / API berubah)")
                log_info(f"berhasil: {success}  gagal: {failed}  sisa: {total - i}")
                sys.exit(1)
            continue

        # delay cuma setelah sukses post video baru
        if i < total:
            log_dim(f"tunggu {DELAY_MENIT} menit ...")
            time.sleep(DELAY_MENIT * 60)

    hr()
    print()
    print(f"  {_c('★ SELESAI ★', C.GREEN + C.BOLD)}   "
          f"{_c('berhasil: ' + str(success), C.GREEN)}   "
          f"{_c('gagal: ' + str(failed), C.RED if failed else C.GRAY)}   "
          f"{_c('total: ' + str(total), C.WHITE)}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_warn("dihentikan user (Ctrl+C)")
