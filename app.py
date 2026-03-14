"""
VORTEX - Universal Video Downloader Backend
Optimized for high-resolution fetching and hosted environments.
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

# ── Paths resolved relative to this script ───────────────────────────────────
BASE_DIR     = Path(__file__).parent.resolve()
STATIC_DIR   = BASE_DIR / "static"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

def _get_server_cookie_file():
    """Return a valid cookie file path for yt-dlp to use server-side."""
    file_path = os.environ.get("YTDLP_COOKIE_FILE")
    if file_path and Path(file_path).exists() and Path(file_path).stat().st_size > 100:
        return file_path

    content = os.environ.get("YOUTUBE_COOKIES_CONTENT", "").strip()
    if len(content) > 100:
        tmp = Path(tempfile.gettempdir()) / "yt_server_cookies.txt"
        tmp.write_text(content, encoding="utf-8")
        return str(tmp)
    return None

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
CORS(app)

# ── Job Management ───────────────────────────────────────────────────────────
jobs: dict = {}
jobs_lock = threading.Lock()

# Detection: Critical for 1080p+ and merging
FFMPEG_PATH = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
FFMPEG_AVAILABLE = FFMPEG_PATH is not None

def _auto_update_ytdlp():
    """Keeps yt-dlp updated to bypass platform changes."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--quiet"],
            capture_output=True, timeout=120
        )
    except Exception:
        pass

threading.Thread(target=_auto_update_ytdlp, daemon=True).start()

def _cleanup():
    """Periodically cleans up the downloads folder."""
    while True:
        time.sleep(300)
        cutoff = time.time() - 1800 # 30 mins
        for f in DOWNLOAD_DIR.iterdir():
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except Exception:
                pass

threading.Thread(target=_cleanup, daemon=True).start()

# ── yt-dlp Core Configuration ────────────────────────────────────────────────

def base_opts(cookiefile=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                # Using iOS/Android clients bypasses many Data Center IP blocks
                "player_client": ["ios", "android", "web"],
                "player_skip": ["webpage", "configs"],
            },
            # Preserved your PO Token script configuration
            "youtubepot-bgutilscript": {
                "server_home": "/bgutil/server",
            },
            "instagram": {
                "get_test_info": True,
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    cf = cookiefile or _get_server_cookie_file()
    if cf:
        opts["cookiefile"] = cf
    
    if FFMPEG_PATH:
        opts["ffmpeg_location"] = FFMPEG_PATH

    return opts

def sanitize(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "_", name).strip()
    cleaned = cleaned.strip(". ")
    return cleaned[:100] or "video"

FORMAT_MAP = {
    "MP4":  {"ext": "mp4",  "audio_only": False},
    "WEBM": {"ext": "webm", "audio_only": False},
    "MKV":  {"ext": "mkv",  "audio_only": False},
    "MP3":  {"ext": "mp3",  "audio_only": True},
    "AAC":  {"ext": "m4a",  "audio_only": True},
}

QUALITY_HEIGHT = {
    "4K (2160p)": 2160, "1440p": 1440, "1080p": 1080,
    "720p": 720, "480p": 480, "360p": 360,
}

def build_opts(fmt: str, quality: str, job_id: str, cookie_file=None):
    f_info = FORMAT_MAP.get(fmt, FORMAT_MAP["MP4"])
    ext = f_info["ext"]
    audio_only = f_info["audio_only"]
    out_tmpl = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    
    postprocessors = []
    merge_fmt = None

    if audio_only:
        fmt_selector = "bestaudio/best"
        postprocessors.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3" if fmt == "MP3" else "m4a",
            "preferredquality": "320",
        })
    else:
        h = QUALITY_HEIGHT.get(quality, 1080)
        if FFMPEG_AVAILABLE:
            # Force high-res DASH streams when FFmpeg is present
            fmt_selector = f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            merge_fmt = ext if ext in ["mp4", "mkv", "webm"] else "mp4"
        else:
            # Fallback for systems without FFmpeg (limited to 720p)
            fmt_selector = f"best[height<={h}]/best"

    def progress_hook(d):
        with jobs_lock:
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                dl = d.get("downloaded_bytes", 0)
                pct = int(dl / total * 100) if total else 0
                jobs[job_id].update({"status": "downloading", "progress": min(pct, 95)})
            elif d["status"] == "finished":
                jobs[job_id].update({"status": "processing", "progress": 98})

    opts = {
        **base_opts(cookie_file),
        "outtmpl": out_tmpl,
        "format": fmt_selector,
        "progress_hooks": [progress_hook],
        "postprocessors": postprocessors,
        "merge_output_format": merge_fmt,
    }
    
    return opts

# ── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(str(STATIC_DIR / "index.html"))

@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "ffmpeg": FFMPEG_AVAILABLE,
        "cookies": _get_server_cookie_file() is not None
    })

def _format_yt_dlp_error(exc: Exception) -> str:
    msg = str(exc)
    if "403" in msg or "sign in" in msg.lower():
        return "Access denied by platform. Server cookies may be required."
    return f"Error: {msg.split(':')[-1].strip()}"

@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(force=True) or {}
    url  = data.get("url", "").strip()
    if not url: return jsonify({"error": "No URL provided"}), 400

    try:
        opts = {**base_opts(), "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": _format_yt_dlp_error(e)}), 422

    heights = sorted(list(set(f.get("height") for f in info.get("formats", []) if f.get("height"))), reverse=True)
    q_labels = [f"{h}p" for h in heights if h >= 144]

    return jsonify({
        "title": info.get("title", "Unknown"),
        "uploader": info.get("uploader") or info.get("channel") or "Unknown",
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail") or (info.get("thumbnails")[-1]['url'] if info.get("thumbnails") else ""),
        "platform": info.get("extractor_key", "Unknown"),
        "qualities_video": q_labels or ["1080p", "720p", "480p"],
        "qualities_audio": ["320 kbps", "128 kbps"],
    })

@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.get_json(force=True) or {}
    url, fmt, quality = data.get("url"), data.get("format", "MP4"), data.get("quality", "1080p")
    
    if not url: return jsonify({"error": "No URL"}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"status":"queued","progress":0,"file_path":None,"error":None,"filename":None}

    threading.Thread(target=_worker, args=(job_id, url, fmt, quality), daemon=True).start()
    return jsonify({"job_id": job_id})

def _worker(job_id, url, fmt, quality):
    try:
        opts = build_opts(fmt, quality, job_id)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        
        title = sanitize(info.get("title", "video"))
        candidates = list(DOWNLOAD_DIR.glob(f"{job_id}.*"))
        # Filter out .part files to find the finished product
        valid_files = [c for c in candidates if not c.name.endswith(".part")]
        if not valid_files: raise FileNotFoundError("Download finished but file not found.")
        
        found = max(valid_files, key=lambda p: p.stat().st_size)
        final_path = DOWNLOAD_DIR / f"{job_id}_final{found.suffix}"
        found.rename(final_path)

        with jobs_lock:
            jobs[job_id].update({
                "status": "done", 
                "progress": 100, 
                "file_path": str(final_path), 
                "filename": f"{title}{found.suffix}"
            })
    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({"status": "error", "error": _format_yt_dlp_error(e)})

@app.route("/api/status/<job_id>")
def api_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job: return jsonify({"error":"Job not found"}), 404
    return jsonify({k: job[k] for k in ("status","progress","error","filename")})

@app.route("/api/file/<job_id>")
def api_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done": return jsonify({"error":"Not ready"}), 404
    
    fp = Path(job["file_path"])
    if not fp.exists(): return jsonify({"error":"File expired"}), 410

    mime_map = {
        ".mp4":"video/mp4",".webm":"video/webm",".mkv":"video/x-matroska",
        ".mp3":"audio/mpeg",".m4a":"audio/mp4",".flac":"audio/flac",
    }
    mime = mime_map.get(fp.suffix.lower(), "application/octet-stream")
    
    return send_file(str(fp), mimetype=mime, as_attachment=True, download_name=job["filename"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)