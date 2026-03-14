"""
VORTEX - Universal Video Downloader Backend
Run with: python app.py
"""

import os
import sys
import re
import time
import threading
import uuid
import shutil
import tempfile
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp

# ── Paths resolved relative to this script (works from any working dir) ───────
BASE_DIR     = Path(__file__).parent.resolve()
STATIC_DIR   = BASE_DIR / "static"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

import tempfile

def _get_server_cookie_file():
    """Return a valid cookie file path for yt-dlp to use server-side."""
    # Option 1: explicit file path env var (paid Render plan / Secret Files)
    file_path = os.environ.get("YTDLP_COOKIE_FILE")
    if file_path and Path(file_path).exists() and Path(file_path).stat().st_size > 100:
        return file_path

    # Option 2: inline cookie content env var (free Render plan workaround)
    content = os.environ.get("YOUTUBE_COOKIES_CONTENT", "").strip()
    if len(content) > 100:
        tmp = Path(tempfile.gettempdir()) / "yt_server_cookies.txt"
        tmp.write_text(content, encoding="utf-8")
        return str(tmp)

    return None

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)

# ── Job store ─────────────────────────────────────────────────────────────────
jobs: dict = {}
jobs_lock = threading.Lock()

FFMPEG_PATH = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
FFMPEG_AVAILABLE = FFMPEG_PATH is not None

# ── Auto-update yt-dlp on startup (keeps YouTube working long-term) ───────────
def _auto_update_ytdlp():
    """Silently update yt-dlp in background so YouTube never blocks due to old version."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--quiet"],
            capture_output=True, timeout=120
        )
    except Exception:
        pass

threading.Thread(target=_auto_update_ytdlp, daemon=True).start()

# ── Cleanup old downloads every 5 min (keep 30 min) ──────────────────────────
def _cleanup():
    while True:
        time.sleep(300)
        cutoff = time.time() - 1800
        for f in DOWNLOAD_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except Exception:
                pass

threading.Thread(target=_cleanup, daemon=True).start()

# ── yt-dlp helpers ────────────────────────────────────────────────────────────

def base_opts(cookiefile=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "noplaylist": True,
        # Try multiple YouTube clients in order — android/tv_embedded bypass
        # bot-protection without needing cookies in most cases
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "tv_embedded", "web_creator", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    # 1. Use explicitly passed cookie file (highest priority)
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    else:
        # 2. Use server-side cookie file (set once by admin on Render)
        server_cf = _get_server_cookie_file()
        if server_cf:
            opts["cookiefile"] = server_cf
        else:
            # 3. Fallback: try reading from browser on the server (dev only)
            browser = os.environ.get("YTDLP_COOKIES_FROM_BROWSER")
            if browser:
                opts["cookiesfrombrowser"] = tuple(
                    c.strip() for c in browser.split(",") if c.strip()
                )

    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    return opts


def sanitize(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    cleaned = cleaned.strip(". ")
    if not cleaned:
        cleaned = "video"
    reserved = {
        "CON","PRN","AUX","NUL",
        "COM1","COM2","COM3","COM4","COM5","COM6","COM7","COM8","COM9",
        "LPT1","LPT2","LPT3","LPT4","LPT5","LPT6","LPT7","LPT8","LPT9",
    }
    if cleaned.upper() in reserved:
        cleaned = f"_{cleaned}"
    return cleaned[:100]

FORMAT_MAP = {
    "MP4":  {"ext": "mp4",  "audio_only": False},
    "WEBM": {"ext": "webm", "audio_only": False},
    "MKV":  {"ext": "mkv",  "audio_only": False},
    "MOV":  {"ext": "mov",  "audio_only": False},
    "AVI":  {"ext": "avi",  "audio_only": False},
    "3GP":  {"ext": "3gp",  "audio_only": False},
    "MP3":  {"ext": "mp3",  "audio_only": True},
    "AAC":  {"ext": "m4a",  "audio_only": True},
    "FLAC": {"ext": "flac", "audio_only": True},
    "OGG":  {"ext": "ogg",  "audio_only": True},
    "WAV":  {"ext": "wav",  "audio_only": True},
}

QUALITY_HEIGHT = {
    "4K (2160p)": 2160, "1440p": 1440, "1080p": 1080,
    "720p": 720, "480p": 480, "360p": 360, "240p": 240,
}
AUDIO_KBPS = {
    "320 kbps": "320", "256 kbps": "256",
    "192 kbps": "192", "128 kbps": "128",
}

def build_opts(fmt: str, quality: str, job_id: str, cookie_file=None):
    f = FORMAT_MAP.get(fmt, FORMAT_MAP["MP4"])
    ext = f["ext"]
    audio_only = f["audio_only"]
    out_tmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    postprocessors = []

    if audio_only:
        abr = AUDIO_KBPS.get(quality, "320")
        fmt_selector = "bestaudio/best"
        codec = "aac" if ext == "m4a" else ext
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
            "preferredquality": abr,
        })
        merge_fmt = None
    else:
        h = QUALITY_HEIGHT.get(quality, 1080)
        if FFMPEG_AVAILABLE:
            fmt_selector = (
                f"bestvideo[height<={h}]+bestaudio/bestvideo[height<={h}]/best[height<={h}]/best"
            )
            if ext != "webm":
                postprocessors.append({"key": "FFmpegVideoConvertor", "preferedformat": ext})
            merge_fmt = ext
        else:
            fmt_selector = f"best[height<={h}]/best"
            merge_fmt = None

    def progress_hook(d):
        with jobs_lock:
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                dl    = d.get("downloaded_bytes", 0)
                pct   = int(dl / total * 100) if total else 0
                jobs[job_id].update({"status": "downloading", "progress": min(pct, 94)})
            elif d["status"] == "finished":
                jobs[job_id].update({"status": "processing", "progress": 96})

    opts = {
        **base_opts(cookiefile=cookie_file),
        "outtmpl": out_tmpl,
        "format": fmt_selector,
        "progress_hooks": [progress_hook],
        "postprocessors": postprocessors,
        "writethumbnail": False,
        "writeinfojson": False,
    }
    if merge_fmt:
        opts["merge_output_format"] = merge_fmt
    return opts

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(str(STATIC_DIR / "index.html"))

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "yt_dlp_version": yt_dlp.version.__version__,
        "ffmpeg_available": FFMPEG_AVAILABLE,
        "ffmpeg_path": FFMPEG_PATH or "",
        "server_cookies_active": _get_server_cookie_file() is not None,
    })

def _format_yt_dlp_error(exc: Exception) -> str:
    """Normalize common yt-dlp errors into friendlier messages."""
    msg = str(exc)
    if "Sign in to confirm" in msg or "sign in to confirm" in msg.lower() or "bot" in msg.lower():
        return "This video could not be downloaded. It may be region-locked or restricted."
    if "This video is unavailable" in msg or "video unavailable" in msg.lower():
        return "This video is unavailable. It may be private, deleted, or blocked in your region."
    if "Private video" in msg:
        return "This is a private video and cannot be downloaded."
    if "age" in msg.lower() and "restrict" in msg.lower():
        return "This video is age-restricted and cannot be downloaded."
    return "Download failed. Please check the URL and try again."


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(force=True) or {}
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Server handles cookies silently — user never needs to provide them
    try:
        opts = {**base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": _format_yt_dlp_error(e)}), 422
    except Exception as e:
        return jsonify({"error": f"Failed to fetch info: {e}"}), 500

    # Collect heights
    heights = set()
    mp4_h264 = set()
    webm = set()
    if info.get("formats"):
        for f in info["formats"]:
            h = f.get("height")
            if not h or h < 144:
                continue
            heights.add(h)
            vcodec = (f.get("vcodec") or "").lower()
            ext = (f.get("ext") or "").lower()
            if vcodec and vcodec != "none":
                if ext == "mp4" and vcodec.startswith("avc1"):
                    mp4_h264.add(h)
                if ext == "webm":
                    webm.add(h)

    label_map = {2160:"4K (2160p)",1440:"1440p",1080:"1080p",
                 720:"720p",480:"480p",360:"360p",240:"240p",144:"144p"}
    q_video = [label_map.get(h, f"{h}p") for h in sorted(heights, reverse=True)]
    q_mp4 = [label_map.get(h, f"{h}p") for h in sorted(mp4_h264, reverse=True)]
    q_webm = [label_map.get(h, f"{h}p") for h in sorted(webm, reverse=True)]
    if not q_video:
        q_video = ["1080p","720p","480p","360p"]

    thumbnail = info.get("thumbnail") or ""
    if not thumbnail and info.get("thumbnails"):
        thumbnail = info["thumbnails"][-1].get("url","")

    return jsonify({
        "title":          info.get("title","Unknown"),
        "uploader":       info.get("uploader") or info.get("channel") or "Unknown",
        "duration":       info.get("duration"),
        "view_count":     info.get("view_count"),
        "thumbnail":      thumbnail,
        "platform":       info.get("extractor_key","Unknown"),
        "qualities_video": q_video,
        "mp4_compat_qualities": q_mp4,
        "qualities_audio": ["320 kbps","256 kbps","192 kbps","128 kbps"],
    })

@app.route("/api/download", methods=["POST"])
def api_download():
    data    = request.get_json(force=True) or {}
    url     = data.get("url", "").strip()
    fmt     = data.get("format", "MP4").upper()
    quality = data.get("quality", "1080p")

    if not url:
        return jsonify({"error": "No URL"}), 400
    if fmt not in FORMAT_MAP:
        return jsonify({"error": f"Unknown format: {fmt}"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status":"queued","progress":0,"file_path":None,"error":None,"filename":None}

    # Server handles cookies — no cookie_file passed from user
    t = threading.Thread(target=_worker, args=(job_id, url, fmt, quality, None), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})

def _worker(job_id, url, fmt, quality, cookie_file=None):
    with jobs_lock:
        jobs[job_id]["status"] = "starting"
    try:
        opts = build_opts(fmt, quality, job_id, cookie_file)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = sanitize(info.get("title","video"))

        candidates = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        candidates = [c for c in candidates if not c.name.endswith(".part")]
        if not candidates:
            raise FileNotFoundError("Download produced no output file.")
        found = max(candidates, key=lambda p: p.stat().st_size)

        final_ext  = found.suffix
        final_name = f"{title}{final_ext}"
        final_path = DOWNLOAD_DIR / f"{job_id}_final{final_ext}"
        found.rename(final_path)
        for c in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try: c.unlink(missing_ok=True)
            except: pass

        with jobs_lock:
            jobs[job_id].update({"status":"done","progress":100,
                                  "file_path":str(final_path),"filename":final_name})
    except Exception as e:
        err = _format_yt_dlp_error(e)
        with jobs_lock:
            jobs[job_id].update({"status":"error","error":err})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error":"Job not found"}), 404
    return jsonify({k: job[k] for k in ("status","progress","error","filename")})

@app.route("/api/file/<job_id>")
def api_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error":"Not ready"}), 404
    fp = Path(job["file_path"])
    if not fp.exists():
        return jsonify({"error":"File expired"}), 410
    mime_map = {
        ".mp4":"video/mp4",".webm":"video/webm",".mkv":"video/x-matroska",
        ".mov":"video/quicktime",".avi":"video/x-msvideo",".3gp":"video/3gpp",
        ".mp3":"audio/mpeg",".m4a":"audio/mp4",".flac":"audio/flac",
        ".ogg":"audio/ogg",".wav":"audio/wav",
    }
    mime = mime_map.get(fp.suffix.lower(),"application/octet-stream")
    return send_file(str(fp), mimetype=mime, as_attachment=True,
                     download_name=job.get("filename", fp.name))

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "="*52)
    print("  VORTEX Video Downloader")
    print(f"  yt-dlp version: {yt_dlp.version.__version__}")
    print(f"  Server cookies: {'✓ Active' if _get_server_cookie_file() else '✗ Not set (using client fallbacks)'}")
    print("  Open: http://localhost:5000")
    print("="*52 + "\n")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
