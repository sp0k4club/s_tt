"""
AUTO POST TIKTOK -> FACEBOOK FANSPAGE (REELS) - via Internal API
==================================================================
Flow:
1. Input username TikTok
2. Ambil semua video dari user itu via TikWM API (otomatis no-watermark)
3. Download semua video ke folder ./downloads/
4. Login ke Facebook pakai cookie (cookies.json) - scrape fb_dtsg token
5. Upload video pake endpoint rupload.facebook.com (chunked)
6. Publish ke Reels fanspage via GraphQL mutation, post pake delay (default 15 menit)

Cara pakai:
    pip install -r requirements.txt
    python autopost.py
"""

import json
import os
import re
import sys
import time
import uuid
import base64
import shutil
import hashlib
import tempfile
import subprocess
import mimetypes
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

# Playwright wajib - buat scrape TikTok profile
try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_OK = True
except ImportError:
    _PLAYWRIGHT_OK = False

# =========================
# KONFIGURASI (edit di sini)
# =========================
DELAY_MENIT = 3  # delay antar post (menit) - ganti ke 30 kalau mau lebih aman
MAX_VIDEO = 0  # 0 = ambil semua, isi angka kalau mau limit (contoh: 10)
COOKIES_FILE = "cookies.json"
DOWNLOAD_DIR = "downloads"
PAGE_ID = ""  # KOSONGIN AJA - otomatis dibaca dari cookie 'i_user'
# (cookie 'i_user' = ID fanspage yg lu switch sebelum export cookie)
# Cuma isi manual kalo lu mau force ke page lain.

# File catatan video yang UDAH di-post (biar ga dobel post pas re-run)
POSTED_FILE = "posted.txt"
# Hapus file MP4 setelah berhasil di-post (hemat disk, penting buat VPS)
DELETE_AFTER_POST = True

# File daftar URL TikTok (dipakai kalo pilih mode "urls")
URLS_FILE = "urls.txt"

# Playwright: False = browser KELIATAN (lebih susah ke-detect bot, bisa selesaiin captcha manual)
#             True  = headless (lebih cepet tapi gampang ke-block TikTok)
PLAYWRIGHT_HEADLESS = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# =========================
# 1) TIKTOK SCRAPER (multi-source dgn fallback)
# =========================
TIKWM_BASE = "https://www.tikwm.com/api"
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tikwm.com/",
}


def _clean_alt_caption(alt: str) -> str:
    """
    Bersihin alt-text TikTok: buang embel-embel auto-generate di belakang.
    Contoh alt: '#manhwa #fyp created by AMV Ft Helhella with Connor's Bleed'
    -> '#manhwa #fyp'
    """
    if not alt:
        return ""
    # Buang ' created by ... ' sampai akhir (info author + musik yg ditambah TikTok)
    alt = re.sub(r'\s+created by\b.*$', '', alt, flags=re.IGNORECASE | re.DOTALL)
    return alt.strip()


def _build_caption_map(html: str) -> dict:
    """
    Parse JSON hydration TikTok (__UNIVERSAL_DATA__ / SIGI_STATE) dari HTML profil,
    return mapping {video_id: caption_desc_asli}.
    """
    cap_map = {}

    def _walk_itemlist(items):
        for it in items or []:
            vid = str(it.get("id") or it.get("itemId") or "")
            desc = it.get("desc")
            if vid and desc:
                cap_map[vid] = desc

    # Format 1: SIGI_STATE -> ItemModule
    m = re.search(r'<script id="SIGI_STATE"[^>]*>(.+?)</script>', html, re.DOTALL)
    if m:
        try:
            state = json.loads(m.group(1))
            for vid, it in (state.get("ItemModule") or {}).items():
                desc = it.get("desc")
                if desc:
                    cap_map[str(vid)] = desc
        except Exception:
            pass

    # Format 2: __UNIVERSAL_DATA_FOR_REHYDRATION__
    m = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.+?)</script>',
        html, re.DOTALL,
    )
    if m:
        try:
            state = json.loads(m.group(1))
            scope = state.get("__DEFAULT_SCOPE__", {})
            for key in ("webapp.user-detail", "webapp.video-detail"):
                node = scope.get(key, {})
                _walk_itemlist(node.get("itemList"))
                # kadang nested di userInfo / itemInfo
                item = node.get("itemInfo", {}).get("itemStruct")
                if item and item.get("id") and item.get("desc"):
                    cap_map[str(item["id"])] = item["desc"]
        except Exception:
            pass

    # Format 3 (fallback regex): "id":"<id>",...,"desc":"<desc>"
    if not cap_map:
        for m in re.finditer(r'"id":"(\d{15,25})"[^{]*?"desc":"((?:[^"\\]|\\.)*)"', html):
            try:
                vid = m.group(1)
                desc = json.loads('"' + m.group(2) + '"')  # decode \u, \n, dll
                if desc:
                    cap_map[vid] = desc
            except Exception:
                continue

    return cap_map


def _fetch_tiktok_caption(video_url: str) -> str:
    """
    Ambil caption asli TikTok via oEmbed (zero browser, no login, no captcha).
    Return caption (hashtag + judul kalo ada) atau "".
    """
    try:
        clean = video_url.split("?")[0]
        r = requests.get(
            "https://www.tiktok.com/oembed",
            params={"url": clean},
            headers={"User-Agent": COMMON_HEADERS["User-Agent"]},
            timeout=20,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        return (data.get("title") or "").strip()
    except Exception:
        return ""


def get_tiktok_videos_from_file(urls_file: str, max_video: int = 0, skip_ids: set = None):
    """
    Baca daftar URL video/photo TikTok dari file (1 URL per baris).
    ZERO browser, zero captcha - cocok buat VPS unattended.
    Format file (urls.txt):
        https://www.tiktok.com/@user/video/7541781364996181253
        https://www.tiktok.com/@user/photo/7648467041258736916
        # baris diawali '#' = komentar, di-skip
    Caption tiap video auto-fetch dari TikTok oEmbed.
    skip_ids: set ID yang udah di-post -> di-skip (ga buang request oEmbed).
    """
    if not os.path.exists(urls_file):
        raise RuntimeError(f"File {urls_file} ga ada")

    skip_ids = skip_ids or set()
    items = []
    seen = set()
    already_posted = 0
    with open(urls_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Ekstrak video/photo id dari URL
            m = re.search(r"/(video|photo)/(\d{10,25})", line)
            if not m:
                m2 = re.search(r"(\d{15,25})", line)
                if not m2:
                    print(f"[urls] skip baris (ga ada ID): {line[:60]}")
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
                already_posted += 1
                continue  # udah di-post, skip lebih awal (ga fetch oEmbed)
            items.append({
                "id": vid_id,
                "title": "",  # diisi pas fetch caption di bawah
                "url_no_wm": f"SSSTIK:{full_url}",
                "_page_url": full_url,
            })
            if max_video and len(items) >= max_video:
                break

    if already_posted:
        print(f"[posted] {already_posted} video udah pernah di-post -> di-skip")
    if not items:
        if already_posted:
            print("[posted] Semua video di file udah pernah di-post. Ga ada yang baru.")
            return []
        raise RuntimeError(f"{urls_file} kosong / ga ada URL valid")

    # Fetch caption asli tiap video via oEmbed
    print(f"[urls] {len(items)} video baru dari {urls_file}, ambil caption ...")
    for it in items:
        cap = _fetch_tiktok_caption(it["_page_url"])
        if cap:
            it["title"] = cap
        print(f"[urls]   {it['id']}: {(it['title'] or '(kosong)')[:60]}")
        time.sleep(0.5)  # sopan ke oEmbed, hindari rate-limit
    return items


def _inject_tiktok_cookies(context):
    """
    Inject cookie TikTok dari tiktok_cookies.txt (Netscape format) ke browser
    Playwright. Bikin TikTok anggap user login -> captcha jauh lebih jarang.
    Kalo file ga ada, di-skip (browser jalan tanpa login).
    """
    path = "tiktok_cookies.txt"
    if not os.path.exists(path):
        return
    try:
        cookies = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line or line.startswith("#"):
                    # Netscape pakai '#HttpOnly_' prefix buat httpOnly cookie
                    if line.startswith("#HttpOnly_"):
                        line = line[len("#HttpOnly_"):]
                    else:
                        continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, cpath, secure, expiry, name, value = parts[:7]
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": cpath or "/",
                    "secure": secure.upper() == "TRUE",
                    "expires": int(expiry) if expiry.isdigit() else -1,
                })
        if cookies:
            context.add_cookies(cookies)
            print(f"[playwright] inject {len(cookies)} cookie TikTok (anti-captcha)")
    except Exception as e:
        print(f"[playwright] gagal inject cookie TikTok: {e}")


def _wait_if_captcha(page):
    """
    Deteksi captcha TikTok. Kalo browser keliatan (non-headless), kasih waktu
    user selesaiin manual. Kalo headless, cuma warning (ga bisa interaksi).
    """
    try:
        html = page.content().lower()
    except Exception:
        return
    if "captcha" not in html and "verify" not in html and "/verify" not in page.url.lower():
        return
    if PLAYWRIGHT_HEADLESS:
        print("[playwright] KENA CAPTCHA (headless - ga bisa di-solve otomatis).")
        print("             Set PLAYWRIGHT_HEADLESS=False buat solve manual,")
        print("             atau pakai mode urls.txt biar ga ada captcha.")
        return
    print("\n" + "=" * 50)
    print(" KENA CAPTCHA TikTok!")
    print(" Selesaiin captcha-nya di window browser yang kebuka.")
    print(" Setelah selesai, tekan ENTER di sini buat lanjut ...")
    print("=" * 50)
    try:
        input()
    except Exception:
        page.wait_for_timeout(15000)


def _src_playwright(username: str, max_video: int):
    """
    Pakai Playwright headless (browser asli) buat scrape TikTok profile.
    PALING RELIABLE karena ke-detect sebagai browser real.
    """
    if not _PLAYWRIGHT_OK:
        raise RuntimeError(
            "Playwright ga terinstall. Run: pip install playwright && playwright install chromium"
        )

    url = f"https://www.tiktok.com/@{username}"
    print(f"[playwright] launch browser, goto {url}")

    seen = set()
    items = []

    # Persistent profile -> browser inget 'kepercayaan' (captcha sekali, abis itu inget)
    profile_dir = Path(".pw_profile").absolute()
    profile_dir.mkdir(exist_ok=True)

    # Stealth script: sembunyiin jejak otomasi yg dipake TikTok buat deteksi bot
    stealth_js = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        window.chrome = { runtime: {} };
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) => (
            p.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(p)
        );
    """

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=PLAYWRIGHT_HEADLESS,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context.add_init_script(stealth_js)

        # Inject cookie TikTok kalo ada (tiktok_cookies.txt Netscape format)
        # -> TikTok anggap user login -> captcha jauh lebih jarang
        _inject_tiktok_cookies(context)

        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            context.close()
            raise RuntimeError(f"playwright goto fail: {e}")

        # Deteksi captcha -> kasih waktu user selesai-in manual (kalo non-headless)
        _wait_if_captcha(page)

        # Tunggu konten ke-render
        try:
            page.wait_for_selector(
                'a[href*="/video/"], a[href*="/photo/"]', timeout=20000
            )
        except Exception:
            pass  # mungkin profile sepi / kena verify page
        page.wait_for_timeout(3000)

        # Scroll buat trigger lazy-load (sampai max_video tercapai atau ga ada lagi)
        scroll_target = max(max_video * 2, 12) if max_video else 30
        last_count = 0
        for i in range(scroll_target):
            # Ambil tiap link video/photo + caption-nya via DOM.
            # Caption TikTok ada di alt-text <img> thumbnail di dalam link.
            anchors = page.query_selector_all(
                f'a[href*="/@{username}/video/"], a[href*="/@{username}/photo/"]'
            )
            for a in anchors:
                href = a.get_attribute("href") or ""
                m = re.search(
                    rf"/@{re.escape(username)}/(video|photo)/(\d{{10,25}})",
                    href,
                    re.IGNORECASE,
                )
                if not m:
                    continue
                path, vid_id = m.group(1), m.group(2)
                if vid_id in seen:
                    continue
                seen.add(vid_id)
                # Caption fallback: alt-text <img> (dibersihin dari 'created by...')
                caption = ""
                try:
                    img = a.query_selector("img[alt]")
                    if img:
                        caption = _clean_alt_caption((img.get_attribute("alt") or "").strip())
                except Exception:
                    pass
                items.append(
                    {
                        "id": vid_id,
                        "title": caption,
                        "url_no_wm": f"SSSTIK:https://www.tiktok.com/@{username}/{path}/{vid_id}",
                    }
                )
            if max_video and len(items) >= max_video:
                break
            # Scroll bawah
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)
            if len(items) == last_count:
                # Ga ada video baru setelah 3x scroll = stop
                if i > 2 and last_count > 0:
                    break
            last_count = len(items)

        # Override caption dari JSON hydration TikTok (desc asli, lebih lengkap
        # daripada alt-text). Fallback ke alt-text kalo JSON ga punya.
        try:
            cap_map = _build_caption_map(page.content())
            if cap_map:
                hit = 0
                for it in items:
                    desc = cap_map.get(it["id"])
                    if desc:
                        it["title"] = desc
                        hit += 1
                print(f"[playwright] caption asli dari JSON: {hit}/{len(items)} video")
        except Exception as e:
            print(f"[playwright] build caption map gagal: {e}")

        # Kalo ga nemu apa-apa, dump HTML buat debug + cek apa kena verify/captcha
        if not items:
            final_html = page.content()
            page_title = page.title()
            try:
                Path("tiktok_debug.html").write_text(final_html, encoding="utf-8")
            except Exception:
                pass
            verify_hint = ""
            low = final_html.lower()
            if "captcha" in low or "verify" in low or "verification" in low:
                verify_hint = " [KENA CAPTCHA/VERIFY - coba PLAYWRIGHT_HEADLESS=False & selesaikan manual]"
            print(f"[playwright] page title: '{page_title}'{verify_hint}")
            print(
                f"[playwright] HTML di-dump ke tiktok_debug.html ({len(final_html)} bytes)"
            )

        context.close()

    if not items:
        raise RuntimeError(
            "playwright: ga ketemu URL video di profile (profile private / kosong?)"
        )

    if max_video:
        items = items[:max_video]

    # Log caption yang ke-ambil
    for it in items:
        cap = it.get("title") or "(kosong)"
        print(f"[playwright]   {it['id']}: {cap[:70]}")

    return items


# ============= Slideshow compiler (foto + audio -> MP4 via ffmpeg) =============
def _find_ffmpeg() -> Optional[str]:
    """Cari executable ffmpeg di PATH atau lokasi umum Windows."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    for cand in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if os.path.exists(cand):
            return cand
    return None


def _compile_slideshow(slides_data_b64: str, out_path: Path) -> bool:
    """
    Decode slides_data (base64 JSON dari ssstik), lalu compile MP4.
    (Fallback path - URL foto dari tiktokcdn mentah, bisa ke-block dari IP server.)
    """
    try:
        padded = slides_data_b64 + "=" * (-len(slides_data_b64) % 4)
        data = json.loads(base64.b64decode(padded).decode("utf-8", "replace"))
    except Exception as e:
        print(f"[slideshow] gagal decode slides_data: {e}")
        return False
    photo_urls = []
    i = 0
    while str(i) in data:
        entry = data[str(i)]
        url = entry.get("url") if isinstance(entry, dict) else None
        if url:
            photo_urls.append(url)
        i += 1
    audio_url = data.get("music") or data.get("pattern")
    return _images_audio_to_mp4(photo_urls, audio_url, out_path)


def _images_audio_to_mp4(photo_urls, audio_url, out_path: Path) -> bool:
    """
    Download list URL foto + audio, compile jadi MP4 slideshow pakai ffmpeg.
    Durasi tiap foto = durasi_audio / jml_foto. Return True kalo sukses.
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print(
            "[slideshow] ffmpeg ga ketemu. Install: winget install ffmpeg (lalu buka PowerShell baru)"
        )
        return False

    if not photo_urls:
        print("[slideshow] ga ada URL foto")
        return False
    print(
        f"[slideshow] {len(photo_urls)} foto"
        f"{', + audio' if audio_url else ', tanpa audio'} -> compile MP4 ..."
    )

    sess = _SCRAPER if _SCRAPER is not None else requests
    dl_headers = {
        "User-Agent": COMMON_HEADERS["User-Agent"],
        "Referer": "https://ssstik.io/",
        "Accept": "image/webp,image/*,*/*;q=0.8",
    }
    tmpdir = Path(tempfile.mkdtemp(prefix="slideshow_"))
    try:
        # Download tiap foto
        img_paths = []
        for idx, url in enumerate(photo_urls):
            try:
                r = sess.get(url, headers=dl_headers, timeout=60)
                if r.status_code != 200 or not r.content:
                    print(f"[slideshow] foto {idx} gagal (HTTP {r.status_code})")
                    continue
                p = tmpdir / f"img_{idx:03d}.img"
                p.write_bytes(r.content)
                img_paths.append(p)
            except Exception as e:
                print(f"[slideshow] foto {idx} error: {e}")
        if not img_paths:
            print("[slideshow] ga ada foto yang berhasil di-download")
            return False

        # Download audio (opsional)
        audio_path = None
        if audio_url:
            try:
                r = sess.get(audio_url, headers=dl_headers, timeout=120)
                if r.status_code == 200 and r.content:
                    audio_path = tmpdir / "audio.mp3"
                    audio_path.write_bytes(r.content)
            except Exception as e:
                print(f"[slideshow] audio error: {e}")

        # Tentuin durasi audio buat hitung durasi per-foto
        audio_dur = None
        if audio_path:
            audio_dur = _ffprobe_duration(audio_path)
        if not audio_dur or audio_dur <= 0:
            audio_dur = len(img_paths) * 3.0  # fallback 3 detik per foto
        per_photo = max(audio_dur / len(img_paths), 1.0)

        # Bikin file concat list buat ffmpeg
        concat_file = tmpdir / "list.txt"
        lines = []
        for p in img_paths:
            lines.append(f"file '{p.as_posix()}'")
            lines.append(f"duration {per_photo:.3f}")
        lines.append(
            f"file '{img_paths[-1].as_posix()}'"
        )  # repeat terakhir tanpa duration
        concat_file.write_text("\n".join(lines), encoding="utf-8")

        # Scale ke 1080x1920 (portrait reels), pad biar ga gepeng
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
        )
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
        ]
        if audio_path:
            cmd += ["-i", str(audio_path)]
        cmd += ["-vf", vf, "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if audio_path:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-shortest"]
        cmd += [str(out_path)]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"[slideshow] ffmpeg gagal: {result.stderr[-500:]}")
            return False
        if not out_path.exists() or out_path.stat().st_size == 0:
            print("[slideshow] ffmpeg selesai tapi file kosong")
            return False
        print(f"[slideshow] OK -> {out_path.name} ({out_path.stat().st_size//1024} KB)")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _ffprobe_duration(media_path: Path) -> Optional[float]:
    """Ambil durasi media (detik) pakai ffprobe. None kalo gagal."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ff = _find_ffmpeg()
        if ff:
            cand = Path(ff).with_name("ffprobe.exe")
            if cand.exists():
                ffprobe = str(cand)
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


# ============= ssstik downloader (untuk video & photo) =============
def _browser_download_slideshow(photo_page_url: str, out_path: Path) -> bool:
    """
    Buka ssstik.io di browser asli (Playwright), submit URL photo TikTok,
    klik 'Download as video', tangkap MP4 hasil server ssstik (yang ada animasi).
    Ga butuh ffmpeg. Return True kalo sukses.
    """
    if not _PLAYWRIGHT_OK:
        print("[browser-dl] Playwright ga ada")
        return False

    print(f"[browser-dl] buka ssstik.io buat compile slideshow ...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=PLAYWRIGHT_HEADLESS,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 768},
                accept_downloads=True,
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.goto(
                "https://ssstik.io/en", wait_until="domcontentloaded", timeout=60000
            )
            page.wait_for_timeout(2000)

            # Isi URL & submit
            try:
                inp = page.locator('#main_page_text, input[name="id"], #id').first
                inp.fill(photo_page_url, timeout=15000)
            except Exception:
                inp = page.locator('input[type="text"]').first
                inp.fill(photo_page_url, timeout=15000)

            # Klik tombol download/convert utama
            try:
                page.locator(
                    '#submit, button[type="submit"], .pure-button-primary'
                ).first.click(timeout=10000)
            except Exception:
                page.keyboard.press("Enter")

            # Tunggu hasil (slideshow) muncul -> tombol "Download as video"
            try:
                page.wait_for_selector("#slides_generate", timeout=30000)
            except Exception:
                print(
                    "[browser-dl] tombol 'Download as video' ga muncul (mungkin bukan photo?)"
                )
                browser.close()
                return False

            # Klik "Download as video" & tangkap download MP4
            try:
                with page.expect_download(timeout=180000) as dl_info:
                    page.locator("#slides_generate").click()
                download = dl_info.value
                download.save_as(str(out_path))
            except Exception as e:
                print(f"[browser-dl] gagal tangkap download: {e}")
                browser.close()
                return False

            browser.close()

        if out_path.exists() and out_path.stat().st_size > 0:
            print(
                f"[browser-dl] OK -> {out_path.name} ({out_path.stat().st_size//1024} KB)"
            )
            return True
        print("[browser-dl] file kosong")
        return False
    except Exception as e:
        print(f"[browser-dl] error: {e}")
        return False


def _extract_ssstik_caption(html: str) -> str:
    """Ambil caption TikTok (judul + teks/hashtag) dari HTML response ssstik."""

    def _clean(s: str) -> str:
        # Buang tag HTML, decode entity dasar, rapihin whitespace
        s = re.sub(r"<[^>]+>", "", s)
        s = (
            s.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&#39;", "'")
            .replace("&nbsp;", " ")
        )
        return re.sub(r"\s+", " ", s).strip()

    judul = ""
    m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL)
    if m:
        judul = _clean(m.group(1))

    teks = ""
    m = re.search(r'<p[^>]*class="[^"]*maintext[^"]*"[^>]*>(.*?)</p>', html, re.DOTALL)
    if m:
        teks = _clean(m.group(1))

    # Gabung judul + teks, hindari duplikat kalo sama
    parts = []
    if judul:
        parts.append(judul)
    if teks and teks != judul:
        parts.append(teks)
    return "\n".join(parts).strip()


def _ssstik_resolve(video_url: str):
    """
    Ambil URL/binary video dari ssstik.io. Handle dua kasus:
      - VIDEO TikTok (URL /video/ID): return ("url", "https://tikcdn.io/...")
      - PHOTO TikTok (URL /photo/ID, slideshow): return ("binary", <mp4 bytes>)
    Return None kalo gagal.
    """
    # Pake cloudscraper kalo ada (bypass Cloudflare di ssstik & r.ssstik.top)
    sess = _SCRAPER if _SCRAPER is not None else requests.Session()

    try:
        # Scrape token tt dari homepage (kadang berubah)
        home = sess.get("https://ssstik.io/", headers=COMMON_HEADERS, timeout=20).text
        m = re.search(r"s_tt\s*=\s*['\"]([A-Za-z0-9+/=_-]+)['\"]", home)
        tt = m.group(1) if m else "SzVUZTk3"

        r = sess.post(
            "https://ssstik.io/abc?url=dl",
            data={"id": video_url, "locale": "en", "tt": tt},
            headers={
                **COMMON_HEADERS,
                "HX-Request": "true",
                "HX-Current-URL": "https://ssstik.io/",
                "HX-Target": "target",
                "HX-Trigger": "_gcaptcha_pt",
                "Origin": "https://ssstik.io",
                "Referer": "https://ssstik.io/",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[ssstik] /abc HTTP {r.status_code}")
            return None

        html = r.text
        if len(html) < 50:
            print(
                f"[ssstik] /abc response sangat pendek ({len(html)} chars): {html[:200]}"
            )
            return None

        # Caption diambil dari halaman TikTok (saat scraping Playwright), BUKAN dari ssstik.
        # <h2> di ssstik itu judul/author, bukan caption asli + hashtag.
        caption = ""

        # Kasus 1: Photo carousel (slideshow video).
        # Ambil slides_data dari HTML, POST ke r.ssstik.top -> baca header hx-redirect
        # yang isinya URL MP4 final (server ssstik compile slideshow + animasi + audio).
        m_slides = re.search(
            r'name=["\']slides_data["\']\s+value=["\']([^"\']+)["\']', html
        )
        if not m_slides:
            m_slides = re.search(r'slides_data\s*=\s*["\']([^"\']+)["\']', html)

        if m_slides:
            slides_data = m_slides.group(1)
            print(f"[ssstik] photo carousel -> request compile video ...")
            try:
                r2 = sess.post(
                    "https://r.ssstik.top/b/index.sh",
                    data={"slides_data": slides_data},
                    headers={
                        **COMMON_HEADERS,
                        "HX-Request": "true",
                        "HX-Current-URL": "https://ssstik.io/",
                        "HX-Target": "slides_generate",
                        "HX-Trigger": "slides_generate",
                        "Origin": "https://ssstik.io",
                        "Referer": "https://ssstik.io/",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    timeout=180,
                    allow_redirects=False,
                )
                # MP4 URL ada di header hx-redirect (HTMX redirect), BUKAN di body
                mp4_url = r2.headers.get("hx-redirect") or r2.headers.get("HX-Redirect")
                if mp4_url:
                    print(f"[ssstik] slideshow video URL: {mp4_url}")
                    return ("url", mp4_url, caption)
                # Fallback: kadang URL di Location header
                mp4_url = r2.headers.get("Location")
                if mp4_url:
                    print(f"[ssstik] slideshow video URL (Location): {mp4_url}")
                    return ("url", mp4_url, caption)
                print(f"[ssstik] ga ada hx-redirect. headers: {dict(r2.headers)}")
            except Exception as e:
                print(f"[ssstik] compile slideshow gagal: {e}")

            # Fallback terakhir: compile sendiri pakai URL foto (butuh ffmpeg)
            photo_urls = re.findall(
                r'<a\s+href="(https://tikcdn\.io/ssstik/[A-Za-z0-9+/=_-]+)"[^>]*\bdownload\b[^>]*>Download this slide</a>',
                html,
            )
            if not photo_urls:
                raw = re.findall(
                    r'data-splide-lazy="(https://tikcdn\.io/ssstik/s/[^"]+)"', html
                )
                photo_urls = [u.replace("/ssstik/s/", "/ssstik/") for u in raw]
            audio_url = None
            m_audio = re.search(r'href="(https://tikcdn\.io/ssstik/m/[^"]+)"', html)
            if m_audio:
                audio_url = m_audio.group(1)
            if photo_urls:
                print(f"[ssstik] fallback ffmpeg: {len(photo_urls)} foto")
                return ("photos", {"photos": photo_urls, "audio": audio_url}, caption)

        # Kasus 2: Video biasa - ambil href no-watermark MP4
        m = re.search(r'href="(https://tikcdn\.io/ssstik/[^"]+)"', html)
        if m:
            return ("url", m.group(1), caption)

        print(
            f"[ssstik] response ga ada video URL maupun slides_data. Preview: {html[:300]}"
        )
        return None
    except Exception as e:
        print(f"[ssstik] resolve gagal: {e}")
        return None


SOURCES = [
    ("playwright", _src_playwright),
]


def get_tiktok_videos(username: str, max_video: int = 0):
    """Ambil daftar video user TikTok dgn auto-fallback antar source."""
    username = username.lstrip("@").strip()
    print(f"[TIKTOK] Ambil daftar video user @{username} ...")
    if _SCRAPER is None:
        print("[TIKTOK] (tip: 'pip install cloudscraper' buat bypass Cloudflare)")

    last_err = None
    for name, fn in SOURCES:
        try:
            print(f"[TIKTOK] Coba source: {name}")
            videos = fn(username, max_video)
            if videos:
                print(f"[TIKTOK] OK via {name}: {len(videos)} video")
                return videos
            print(f"[TIKTOK] {name}: ga ada video")
        except Exception as e:
            print(f"[TIKTOK] {name} gagal: {e}")
            last_err = e
            continue

    print(f"[TIKTOK] Semua source gagal. Last error: {last_err}")
    return []


def safe_filename(s: str, maxlen: int = 80) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen] or "video").strip()


def download_video(video: dict, outdir: Path) -> Optional[Path]:
    if not video.get("url_no_wm"):
        print(f"[DOWNLOAD] Skip {video.get('id')} - ga ada URL")
        return None

    outdir.mkdir(parents=True, exist_ok=True)
    fname = f"{video['id']}_{safe_filename(video.get('title',''))}.mp4"
    path = outdir / fname

    if path.exists() and path.stat().st_size > 0:
        print(f"[DOWNLOAD] Sudah ada, skip: {fname}")
        return path

    url = video["url_no_wm"]

    # --- Pakai ssstik.io kalau URL diawali marker SSSTIK: ---
    if url.startswith("SSSTIK:"):
        webpage_url = url[len("SSSTIK:") :]
        print(f"[DOWNLOAD] ssstik resolve -> {fname}")
        resolved_url = webpage_url
        result = _ssstik_resolve(webpage_url)
        # Fallback: kalo gagal & URL nya /video/, coba juga sebagai /photo/
        if not result and "/video/" in webpage_url:
            alt_url = webpage_url.replace("/video/", "/photo/")
            print(f"[DOWNLOAD] retry sebagai photo: {alt_url}")
            result = _ssstik_resolve(alt_url)
            if result:
                resolved_url = alt_url
        if not result:
            print("[DOWNLOAD] ssstik gagal resolve URL")
            return None
        # result = (kind, payload, caption) - caption opsional
        kind, payload = result[0], result[1]
        ss_caption = result[2] if len(result) > 2 else ""
        # Caption utama dari TikTok (video["title"]). ssstik caption cuma fallback
        # kalo TikTok ga ngasih caption.
        if ss_caption and not video.get("title"):
            video["title"] = ss_caption
        try:
            if kind == "photos":
                # Fallback ffmpeg (kalo hx-redirect gagal): compile foto+audio jadi MP4
                ok = _images_audio_to_mp4(payload["photos"], payload.get("audio"), path)
                return path if ok else None
            elif kind == "slideshow":
                # Fallback: compile dari slides_data base64
                ok = _compile_slideshow(payload, path)
                return path if ok else None
            elif kind == "binary":
                # Slideshow yang udah di-compile jadi MP4 server-side
                with open(path, "wb") as f:
                    f.write(payload)
                print(f"[DOWNLOAD] slideshow MP4 saved ({len(payload)//1024} KB)")
                return path
            else:
                # URL langsung MP4 (termasuk slideshow video dari r.ssstik.top)
                with requests.get(
                    payload,
                    stream=True,
                    timeout=180,
                    headers={
                        "User-Agent": COMMON_HEADERS["User-Agent"],
                        "Referer": "https://ssstik.io/",
                    },
                ) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                f.write(chunk)
                print(f"[DOWNLOAD] MP4 saved ({path.stat().st_size//1024} KB)")
                return path
        except Exception as e:
            print(f"[DOWNLOAD] ssstik download gagal: {e}")
            if path.exists():
                path.unlink(missing_ok=True)
            return None

    # --- Direct download (TikWM dll) ---
    try:
        print(f"[DOWNLOAD] {fname}")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
        return path
    except Exception as e:
        print(f"[DOWNLOAD] Gagal: {e}")
        if path.exists():
            path.unlink(missing_ok=True)
        return None


# =========================
# 2) FACEBOOK CLIENT (Internal API)
# =========================
class FBClient:
    """Client buat ngomong sama internal API Facebook pake cookie."""

    def __init__(self, cookies_path: str):
        self.session = requests.Session()
        # Header browser-like yang lengkap - FB sensitive sama header lengkap
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "max-age=0",
                "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"Windows"',
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            }
        )
        self._load_cookies(cookies_path)

        self.user_id: Optional[str] = None
        self.i_user: Optional[str] = None
        self.fb_dtsg: Optional[str] = None
        self.jazoest: Optional[str] = None
        self.lsd: Optional[str] = None
        self.spin_r: Optional[str] = None
        self.spin_t: Optional[str] = None
        self.hsi: Optional[str] = None
        self.av: Optional[str] = None  # acting user (page ID kalo posting as page)
        self.access_token: Optional[str] = None  # buat rupload Authorization header

    def _load_cookies(self, path: str):
        if not os.path.exists(path):
            print(f"[FB] File cookie ga ada: {path}")
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        # Bikin RAW Cookie header (lebih aman daripada session.cookies.set
        # karena value cookie FB sering ada karakter khusus yg di-re-encode salah)
        parts = []
        for c in cookies:
            name = c.get("name")
            val = c.get("value")
            if name and val is not None:
                parts.append(f"{name}={val}")
        self._cookie_header = "; ".join(parts)

        # Juga set ke session.cookies buat fallback / akses by-name
        for c in cookies:
            try:
                self.session.cookies.set(
                    c["name"],
                    c["value"],
                    domain=c.get("domain", ".facebook.com"),
                    path=c.get("path", "/"),
                )
            except Exception:
                pass

        # Set ke default session header
        self.session.headers["Cookie"] = self._cookie_header

    def bootstrap(self):
        """Buka facebook.com, scrape fb_dtsg, jazoest, lsd, dll."""
        print("[FB] Bootstrap - scrape token dari halaman utama ...")

        # User ID dari cookie c_user
        cu = self.session.cookies.get("c_user")
        if not cu:
            print("[FB] Cookie c_user ga ada - login gagal.")
            sys.exit(1)
        self.user_id = cu

        # i_user = page yang lagi diaktifkan (kalo udah switch ke fanspage)
        iu = self.session.cookies.get("i_user")
        self.i_user = iu if iu else None

        # Coba beberapa URL FB - kadang token-nya di-render beda per halaman
        # business.facebook.com biasanya ngandung access_token EAA untuk publish
        candidates = [
            "https://business.facebook.com/latest/home",
            "https://business.facebook.com/",
            "https://www.facebook.com/",
            "https://www.facebook.com/me",
            "https://m.facebook.com/",
        ]
        html = ""  # halaman dgn dtsg/token
        html_with_eaa = ""  # halaman yg ada EAA access_token
        last_body = ""
        for u in candidates:
            try:
                r = self.session.get(u, timeout=30, allow_redirects=True)
                status = r.status_code
                final_url = r.url
                size = len(r.text) if r.text else 0
                is_login = (
                    "login" in final_url.lower() or "checkpoint" in final_url.lower()
                )
                has_dtsg = bool(
                    r.text and ("DTSGInit" in r.text or "fb_dtsg" in r.text)
                )
                has_eaa = bool(
                    r.text
                    and "EAA" in r.text
                    and re.search(r"EAA[A-Za-z0-9_-]{30,}", r.text)
                )
                print(
                    f"[FB] GET {u} -> {status} ({size}b) -> {final_url} "
                    f"{'[LOGIN!]' if is_login else ''} "
                    f"dtsg={'Y' if has_dtsg else 'N'} eaa={'Y' if has_eaa else 'N'}"
                )
                if status >= 400:
                    last_body = r.text[:800] if r.text else ""
                if is_login:
                    continue
                if r.text and size > 5000:
                    if has_dtsg and not html:
                        html = r.text
                    if has_eaa and not html_with_eaa:
                        html_with_eaa = r.text
                    # break kalo udah dpt dua-duanya
                    if html and html_with_eaa:
                        break
            except Exception as e:
                print(f"[FB] {u} err: {e}")
                continue
        if last_body:
            print("[FB] --- body response 4xx (first 800 chars) ---")
            print(last_body)
            print("[FB] --- end body ---")

        if not html:
            print("[FB] Semua URL FB gagal / kena login.")
            print("     Sebab umum:")
            print(
                "     1. Cookie udah expired -> login ulang di Chrome, export cookie lagi"
            )
            print("     2. FB invalidate session karena curiga bot ->")
            print("        login di Chrome, klik2 manual dulu beberapa kali,")
            print("        baru export cookie lagi.")
            print("     3. Cookie 'xs' / 'c_user' rusak.")
            print(
                f"     Cookie sekarang: c_user={self.user_id}, "
                f"xs={'ada' if self.session.cookies.get('xs') else 'GA ADA'}, "
                f"i_user={self.i_user or 'ga ada'}"
            )
            sys.exit(1)

        # Pattern fb_dtsg - banyak variasi format FB
        self.fb_dtsg = self._extract(
            html,
            [
                r'"DTSGInitialData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
                r'"DTSGInitData"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
                r'\["DTSGInitialData",\[\],\{"token":"([^"]+)"',
                r'\["DTSGInitData",\[\],\{"token":"([^"]+)"',
                r'name="fb_dtsg"\s+value="([^"]+)"',
                r'"fb_dtsg"\s*:\s*"([^"]+)"',
                r'"dtsg"\s*:\s*\{\s*"token"\s*:\s*"([^"]+)"',
                r'"token":"([^"]+)","async_get_token"',
            ],
        )
        self.jazoest = self._extract(
            html,
            [
                r'name="jazoest"\s+value="([^"]+)"',
                r'"jazoest"\s*:\s*"([^"]+)"',
                r"&jazoest=(\d+)",
            ],
        )
        self.lsd = self._extract(
            html,
            [
                r'"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
                r'\["LSD",\[\],\{"token":"([^"]+)"',
                r'name="lsd"\s+value="([^"]+)"',
                r'"lsd"\s*:\s*"([^"]+)"',
            ],
        )
        self.spin_r = self._extract(
            html, [r'"__spin_r"\s*:\s*(\d+)', r"__spin_r=(\d+)"]
        )
        self.spin_t = self._extract(
            html, [r'"__spin_t"\s*:\s*(\d+)', r"__spin_t=(\d+)"]
        )
        self.hsi = self._extract(html, [r'"hsi"\s*:\s*"(\d+)"', r"&hsi=(\d+)"])

        # Access token EAA - cari di halaman yg memang biasa ngandung token
        token_src = html_with_eaa or html
        self.access_token = self._extract(
            token_src,
            [
                r'"accessToken"\s*:\s*"(EAA[^"]+)"',
                r'"access_token"\s*:\s*"(EAA[^"]+)"',
                r'\\"accessToken\\":\\"(EAA[^\\]+)\\"',
                r"(EAA[A-Za-z0-9_-]{50,})",
            ],
        )

        if not self.fb_dtsg:
            # Dump HTML buat debugging
            dump_path = Path("fb_debug.html")
            try:
                dump_path.write_text(html, encoding="utf-8")
                print(f"[FB] Dump HTML -> {dump_path.absolute()}")
            except Exception:
                pass
            print(
                "[FB] Gagal ambil fb_dtsg - cookie mungkin invalid / HTML FB berubah."
            )
            print(
                "     Cek isi fb_debug.html, search kata 'dtsg' - kasih tau gua format-nya."
            )
            sys.exit(1)

        print(
            f"[FB] OK - user_id={self.user_id}, fb_dtsg={'ok' if self.fb_dtsg else 'MISS'}, "
            f"lsd={'ok' if self.lsd else 'MISS'}, jazoest={'ok' if self.jazoest else 'MISS'}, "
            f"access_token={'ok' if self.access_token else 'MISS'}"
        )

    def _extract(self, html: str, patterns) -> Optional[str]:
        for p in patterns:
            m = re.search(p, html)
            if m:
                return m.group(1)
        return None

    def set_acting_page(self, page_id: str):
        """Set acting user ke fanspage (biar post atas nama page)."""
        self.av = page_id
        print(f"[FB] Acting as page_id={page_id}")

    # =========================================================
    # FULL UPLOAD + PUBLISH FLOW (cookie-only, no EAA needed)
    # =========================================================
    # Flow:
    #   1. POST /ajax/video/upload/requests/start/  -> register upload, dapet video_id
    #   2. POST https://rupload-...up.facebook.com/fb_video/{md5}-0-{size} -> upload binary
    #   3. POST /ajax/video/upload/requests/receive/ -> finalize, dapet fbuploader_video_file_chunk
    #   4. POST /api/graphql/ ComposerStoryCreateMutation -> publish reels
    # =========================================================

    def _common_form_fields(self, req_num: int = 1, actor_id: str = None) -> dict:
        """Common __dyn/__csr/__hsi/etc fields buat semua POST FB graphql/ajax.
        actor_id: page_id kalo posting as page (utk __user). Default = c_user."""
        return {
            "__user": str(actor_id or self.user_id or "0"),
            "__a": "1",
            "__req": str(req_num),
            "__hs": "20611.HYP:comet_pkg.2.1...0",
            "dpr": "1",
            "__ccg": "EXCELLENT",
            "__rev": self.spin_r or "",
            "__s": secrets.token_hex(4),
            "__hsi": self.hsi or "",
            "__comet_req": "15",
            "fb_dtsg": self.fb_dtsg,
            "jazoest": self.jazoest or "",
            "lsd": self.lsd or "",
            "__spin_r": self.spin_r or "",
            "__spin_b": "trunk",
            "__spin_t": self.spin_t or "",
            "__crn": "comet.fbweb.CometHomeRoute",
            "qpl_active_flow_ids": "884152905",
        }

    def _ajax_headers(self, friendly_name: str = None) -> dict:
        h = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://www.facebook.com",
            "Referer": "https://www.facebook.com/",
            "X-FB-LSD": self.lsd or "",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        if friendly_name:
            h["X-FB-Friendly-Name"] = friendly_name
        return h

    def upload_video(self, video_path: Path, page_id: str) -> str:
        """
        Full upload flow. Return: fbuploader_video_file_chunk token
        (ini yang dipake di publish step sebagai handle, bukan video_id biasa).
        Sebenernya juga return tuple (video_id, chunk_token) - tapi disesuaiin sama interface lama.
        """
        if not video_path.exists():
            raise FileNotFoundError(str(video_path))

        size = video_path.stat().st_size
        mime = "video/mp4"
        waterfall_id = str(uuid.uuid4())

        # -------- Step 1: /start/ --------
        print(f"[FB] [1/3] Register upload ({size/1024/1024:.2f} MB) ...")
        start_url = f"https://vupload-edge.facebook.com/ajax/video/upload/requests/start/?av={page_id}&__a=1"
        start_form = {
            "waterfall_id": waterfall_id,
            "target_id": page_id,
            "source": "reel_composer",
            "composer_entry_point_ref": "comet_ap_plus_reel_composer_feed_sprout",
            "supports_chunking": "true",
            "supports_file_api": "true",
            "file_size": str(size),
            "file_extension": "mp4",
            "partition_start_offset": "0",
            "partition_end_offset": str(size),
            "has_file_been_replaced": "false",
            "__aaid": "0",
            **self._common_form_fields(req_num=14, actor_id=page_id),
        }
        start_headers = self._ajax_headers()
        start_headers["x_fb_video_waterfall_id"] = waterfall_id
        start_headers["Sec-Fetch-Site"] = "same-site"

        r = self.session.post(
            start_url, data=start_form, headers=start_headers, timeout=60
        )
        start_resp = self._parse_json_response(r, "start")
        # Format: {"video_id": "...", "upload_url": "...", "upload_handle": "..."}
        # Atau ke-nest di "payload"
        payload = start_resp.get("payload") or start_resp
        video_id = str(payload.get("video_id") or payload.get("videoID") or "")
        upload_url = payload.get("upload_url")
        if not video_id:
            raise RuntimeError(f"/start/ ga return video_id: {str(start_resp)[:400]}")
        if not upload_url:
            # Build manually dari pattern yg keliatan dari curl: file MD5 jadi key
            file_md5 = self._md5_file(video_path)
            upload_url = (
                f"https://rupload-cgk1-1.up.facebook.com/fb_video/{file_md5}-0-{size}"
            )
        print(f"[FB]     video_id={video_id}")
        print(f"[FB]     upload_url={upload_url[:80]}...")

        # -------- Step 2: upload binary chunk --------
        print(f"[FB] [2/3] Upload binary ...")
        # Tambah query params yg dibutuhin (fb_dtsg_ag, dll)
        # Format URL dari curl: ?__aaid=0&__user=...&__a=1&...&fb_dtsg_ag=...&jazoest=...
        if "?" not in upload_url:
            upload_url += "?"
        else:
            upload_url += "&"
        upload_qs = {
            "__aaid": "0",
            **self._common_form_fields(req_num=44, actor_id=page_id),
            "fb_dtsg_ag": self.fb_dtsg,
        }
        upload_url += "&".join(
            f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in upload_qs.items()
        )

        with open(video_path, "rb") as f:
            file_bytes = f.read()

        upload_filename = f"{video_id}_{int(time.time())}.mp4"
        binary_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/octet-stream",
            "Offset": "0",
            "X-Entity-Length": str(size),
            "X-Entity-Name": upload_filename,
            "X-Entity-Type": mime,
            "Origin": "https://www.facebook.com",
            "Referer": "https://www.facebook.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }
        r = self.session.post(
            upload_url, data=file_bytes, headers=binary_headers, timeout=600
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Binary upload gagal: HTTP {r.status_code}: {r.text[:400]}"
            )
        try:
            binary_resp = r.json()
        except Exception:
            binary_resp = {"raw": r.text[:400]}
        print(f"[FB]     binary upload OK -> {json.dumps(binary_resp)[:300]}")

        # -------- Step 3: /receive/ (finalize, dapet chunk token) --------
        print(f"[FB] [3/3] Finalize upload ...")
        receive_url = f"https://vupload-edge.facebook.com/ajax/video/upload/requests/receive/?av={page_id}&__a=1"

        # Build fbuploader_video_file_chunk dari response binary upload
        # Format dari curl: 1:<base64_filename>:<mime>:<HANDLE>:e:<expiry>:<signature>
        # Response binary upload biasanya ngandung field2 ini:
        #   "h" (handle), "s" (signature), atau "video_id"
        #   atau bahkan "fbuploader_video_file_chunk" siap pakai
        if "fbuploader_video_file_chunk" in binary_resp:
            chunk_token = binary_resp["fbuploader_video_file_chunk"]
        elif "fbu_video_file_chunk" in binary_resp:
            chunk_token = binary_resp["fbu_video_file_chunk"]
        elif binary_resp.get("h") and ":" in str(binary_resp.get("h", "")):
            # Kadang "h" itu sendiri udah full chunk token
            chunk_token = binary_resp["h"]
        else:
            # Build dari pieces. Cari handle, signature, expiry.
            handle = (
                binary_resp.get("h")
                or binary_resp.get("handle")
                or binary_resp.get("video_id")
                or ""
            )
            signature = binary_resp.get("s") or binary_resp.get("signature") or ""
            expiry = (
                binary_resp.get("e")
                or binary_resp.get("expiry")
                or str(int(time.time()) + 86400)
            )
            filename_b64 = self._b64url(f"{video_id}_{video_path.stem[:50]}.mp4")
            if handle and signature:
                chunk_token = f"1:{filename_b64}:{mime}:{handle}:e:{expiry}:{signature}"
            elif handle:
                chunk_token = f"1:{filename_b64}:{mime}:{handle}"
            else:
                # Last resort: pake video_id sebagai handle
                chunk_token = f"1:{filename_b64}:{mime}:{video_id}"
        print(f"[FB]     chunk_token={chunk_token[:80]}...")

        receive_form = {
            "waterfall_id": waterfall_id,
            "target_id": page_id,
            "video_id": video_id,
            "source": "reel_composer",
            "composer_entry_point_ref": "comet_ap_plus_reel_composer_feed_sprout",
            "supports_chunking": "true",
            "supports_upload_service": "true",
            "partition_start_offset": "0",
            "partition_end_offset": str(size),
            "start_offset": "0",
            "end_offset": str(size),
            "upload_speed": "2000000",
            "fbuploader_video_file_chunk": chunk_token,
            "has_file_been_replaced": "false",
            "__aaid": "0",
            **self._common_form_fields(req_num=46, actor_id=page_id),
        }
        receive_headers = self._ajax_headers()
        receive_headers["x_fb_video_waterfall_id"] = waterfall_id
        receive_headers["Sec-Fetch-Site"] = "same-site"

        r = self.session.post(
            receive_url, data=receive_form, headers=receive_headers, timeout=120
        )
        receive_resp = self._parse_json_response(r, "receive")
        # Sukses = ga ada errors, video_id ke-confirm
        if receive_resp.get("error"):
            raise RuntimeError(
                f"/receive/ error: {receive_resp.get('errorSummary') or receive_resp}"
            )
        print(f"[FB]     finalize OK")
        return video_id

    def publish_reel(self, video_id: str, caption: str, page_id: str) -> dict:
        """
        Publish video sebagai Reels ke page.
        Pake ComposerStoryCreateMutation (doc_id=26950268144643228).
        """
        print(f"[FB] Publish reels (video_id={video_id}) ke page {page_id} ...")
        url = "https://www.facebook.com/api/graphql/"
        idem = str(uuid.uuid4())

        variables = {
            "input": {
                "composer_entry_point": "comet_ap_plus_reel_composer_feed_sprout",
                "composer_source_surface": "short_form_video",
                "idempotence_token": f"{idem}_FEED",
                "source": "WWW",
                "attachments": [
                    {
                        "video": {
                            "audio_descriptions": None,
                            "id": str(video_id),
                            "additional_video_metadata": {
                                "translatedAudioMetadata": [],
                                "autoGenCaptionsSettings": {
                                    "autogenerate_captions_enabled": True,
                                    "should_review_all_captions": False,
                                },
                            },
                            "notify_when_processed": True,
                            "transcriptions": None,
                            "was_created_via_unified_video_flow": {
                                "was_created_via_unified_video_flow": True,
                            },
                            "story_media_audio_data": {"raw_media_type": "VIDEO"},
                            "video_media_metadata": {
                                "audio": {
                                    "audio_type": "original_audio",
                                    "start_time_s": 0,
                                    "volume_level": 1,
                                },
                                "is_audio_muted": False,
                                "length_in_sec": 30,
                            },
                        }
                    }
                ],
                "fb_shorts": {
                    "has_overridden_video_format": True,
                    "is_fb_short": False,
                    "remix_status": "DISABLED",
                },
                "post_publish_story_data": {"reshare_post_as_sticker": "DISABLED"},
                "message": {"ranges": [], "text": caption or ""},
                "audience": {
                    "privacy": {
                        "allow": [],
                        "base_state": "EVERYONE",
                        "deny": [],
                        "tag_expansion_state": "UNSPECIFIED",
                    }
                },
                "with_tags_ids": None,
                "reels_remix": {"remix_status": "ENABLED"},
                "stars_receivable": {"is_receiving_stars_disabled": False},
                "logging": {"composer_session_id": idem},
                "navigation_data": {"attribution_id_v2": ""},
                "tracking": [None],
                "event_share_metadata": {"surface": "timeline"},
                "actor_id": str(page_id),
                "client_mutation_id": str(secrets.randbelow(100)),
            },
            "feedLocation": "NEWSFEED",
            "feedbackSource": 1,
            "focusCommentID": None,
            "gridMediaWidth": None,
            "groupID": None,
            "scale": 1,
            "privacySelectorRenderLocation": "COMET_STREAM",
            "checkPhotosToReelsUpsellEligibility": False,
            "referringStoryRenderLocation": None,
            "renderLocation": "homepage_stream",
            "useDefaultActor": False,
            "inviteShortLinkKey": None,
            "isFeed": True,
            "isFundraiser": False,
            "isFunFactPost": False,
            "isGroup": False,
            "isEvent": False,
            "isTimeline": False,
            "isSocialLearning": False,
            "isPageNewsFeed": False,
            "isProfileReviews": False,
            "isWorkSharedDraft": False,
            # Relay provider flags (wajib semua, kalo missing FB return missing_required_variable_value)
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

        form = {
            "av": page_id,
            "__aaid": "0",
            **self._common_form_fields(req_num=63, actor_id=page_id),
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "ComposerStoryCreateMutation",
            "variables": json.dumps(variables),
            "server_timestamps": "true",
            "doc_id": "26950268144643228",
        }
        headers = self._ajax_headers(friendly_name="ComposerStoryCreateMutation")
        headers["Referer"] = f"https://www.facebook.com/profile.php?id={page_id}"

        r = self.session.post(url, data=form, headers=headers, timeout=120)
        resp = self._parse_json_response(r, "publish")
        if resp.get("errors"):
            raise RuntimeError(f"Publish error: {resp['errors']}")
        if resp.get("error"):
            raise RuntimeError(f"Publish error: {resp.get('errorSummary') or resp}")
        return resp

    def _parse_json_response(self, r: requests.Response, label: str) -> dict:
        if r.status_code != 200:
            raise RuntimeError(f"/{label}/ HTTP {r.status_code}: {r.text[:400]}")
        # FB suka kasih prefix "for (;;);" atau multi-line JSON
        text = r.text.strip()
        if text.startswith("for (;;);"):
            text = text[len("for (;;);") :]
        # Coba langsung parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # Multi-line: ambil baris pertama yg valid
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except Exception:
                continue
        raise RuntimeError(f"/{label}/ response bukan JSON: {text[:400]}")

    @staticmethod
    def _md5_file(path: Path) -> str:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 256), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _b64url(s: str) -> str:
        import base64

        return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


# =========================
# 3) MAIN
# =========================
def load_posted() -> set:
    """Baca set ID video yang udah pernah di-post dari POSTED_FILE."""
    posted = set()
    if os.path.exists(POSTED_FILE):
        try:
            with open(POSTED_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    vid = line.strip().split("\t")[0].strip()
                    if vid and not vid.startswith("#"):
                        posted.add(vid)
        except Exception as e:
            print(f"[posted] gagal baca {POSTED_FILE}: {e}")
    return posted


def mark_posted(video_id: str, caption: str = ""):
    """Catat video_id sebagai udah di-post ke POSTED_FILE."""
    try:
        with open(POSTED_FILE, "a", encoding="utf-8") as f:
            # format: <id>\t<caption singkat>  (caption buat referensi manusia)
            cap = (caption or "").replace("\n", " ")[:50]
            f.write(f"{video_id}\t{cap}\n")
    except Exception as e:
        print(f"[posted] gagal nulis {POSTED_FILE}: {e}")


def main():
    print("=" * 60)
    print(" AUTO POST TIKTOK -> FACEBOOK REELS (Internal API)")
    print("=" * 60)

    # Pilih mode. Bisa lewat argumen CLI (buat VPS non-interaktif):
    #   python autopost.py urls [file.txt]
    #   python autopost.py username <username>
    args = sys.argv[1:]
    if args:
        if args[0] == "urls":
            mode = "urls"
            urls_file = args[1] if len(args) > 1 else URLS_FILE
            username = "from_urls"
        elif args[0] == "username" and len(args) > 1:
            mode = "username"
            username = args[1].lstrip("@")
        else:
            print("Usage: python autopost.py [urls <file.txt> | username <name>]")
            sys.exit(1)
        print(f"[MODE] {mode}" + (f" ({urls_file})" if mode == "urls" else f" (@{username})"))
    else:
        # Interaktif: pilih mode di terminal
        print("\nPilih mode input TikTok:")
        print("  1. File urls.txt  (zero browser, no captcha - cocok VPS)")
        print("  2. Username       (scrape profile pakai browser/Playwright)")
        pilih = input("Masukin pilihan [1/2] (default 1): ").strip() or "1"

        if pilih == "2":
            mode = "username"
            username = input("Masukin username TikTok (tanpa @): ").strip()
            if not username:
                print("Username kosong, batal.")
                sys.exit(1)
        else:
            mode = "urls"
            urls_file = input(f"Nama file URL (default {URLS_FILE}): ").strip() or URLS_FILE
            username = "from_urls"  # buat nama folder download

    # 1) Bootstrap FB DULU - biar kalo cookie invalid langsung tau, ga buang waktu scrape
    fb = FBClient(COOKIES_FILE)
    fb.bootstrap()

    page_id = PAGE_ID.strip() if PAGE_ID else fb.i_user
    if not page_id:
        print("[FB] Ga ada PAGE_ID dan cookie 'i_user' juga ga ada.")
        print("     Login FB di browser, SWITCH ke fanspage, baru export cookie ulang.")
        sys.exit(1)
    if page_id == fb.user_id:
        print(
            "[FB] WARNING: page_id == user_id - lu kayaknya belum switch ke fanspage."
        )
        print("     Hasilnya bakal post ke profile pribadi, bukan fanspage.")
    fb.set_acting_page(page_id)

    # Load daftar video yang udah pernah di-post (biar ga dobel pas re-run)
    posted = load_posted()

    # 2) Ambil daftar video sesuai mode (sekalian skip yang udah di-post)
    if mode == "urls":
        videos = get_tiktok_videos_from_file(urls_file, max_video=MAX_VIDEO, skip_ids=posted)
    else:
        videos = get_tiktok_videos(username, max_video=MAX_VIDEO)
        # Mode username: filter posted setelah scrape
        if posted:
            before = len(videos)
            videos = [v for v in videos if v.get("id") not in posted]
            if before - len(videos):
                print(f"[posted] {before - len(videos)} video udah pernah di-post -> di-skip")
    if not videos:
        print("Ga ada video baru buat di-post.")
        sys.exit(0)

    outdir = Path(DOWNLOAD_DIR) / username
    total = len(videos)

    # 3) STREAMING: download 1 -> upload 1 -> delay -> next
    success = 0
    failed = 0
    consecutive_fail = 0  # gagal berturut-turut (deteksi cookie FB mati)
    for i, v in enumerate(videos, start=1):
        print(f"\n===== [{i}/{total}] {v.get('id')} =====")

        # Download
        vpath = download_video(v, outdir)
        if not vpath:
            print(f"[SKIP] Video {v.get('id')} gagal di-download (URL salah/dihapus/private).")
            failed += 1
            continue

        # Upload + publish
        try:
            video_id = fb.upload_video(vpath, page_id=page_id)
            fb.publish_reel(video_id, caption=v.get("title", ""), page_id=page_id)
            print(f"[FB] SUKSES posting reels: {vpath.name}")
            success += 1
            consecutive_fail = 0
            # Catat sebagai udah di-post (biar ga dobel pas re-run)
            mark_posted(v.get("id"), v.get("title", ""))
            # Hapus file MP4 (hemat disk - penting buat VPS)
            if DELETE_AFTER_POST:
                try:
                    vpath.unlink(missing_ok=True)
                    print(f"[CLEANUP] File dihapus: {vpath.name}")
                except Exception as e:
                    print(f"[CLEANUP] Gagal hapus file: {e}")
        except Exception as e:
            print(f"[FB] GAGAL posting video ini: {e}")
            failed += 1
            consecutive_fail += 1
            # Kalo gagal 3x berturut-turut = kemungkinan cookie FB mati / API berubah
            # -> stop, karena sisanya pasti gagal juga
            if consecutive_fail >= 3:
                print(f"\n[STOP] Gagal {consecutive_fail}x berturut-turut.")
                print("       Kemungkinan cookie FB expired / internal API berubah.")
                print(f"       Berhasil: {success}, Gagal: {failed}, Sisa: {total - i}")
                sys.exit(1)
            print(f"[SKIP] Lanjut ke video berikutnya ...")
            continue

        # Delay sebelum video berikutnya (cuma kalo masih ada yang berhasil di-proses)
        if i < total:
            wait = DELAY_MENIT * 60
            print(f"[DELAY] Tunggu {DELAY_MENIT} menit sebelum next video ...")
            time.sleep(wait)

    print(f"\n[DONE] Selesai. Berhasil: {success}, Gagal: {failed}, Total: {total}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[STOP] Dihentikan user.")
