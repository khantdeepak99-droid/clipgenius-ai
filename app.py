import os
import re
import uuid
import time
import threading
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory, send_file, abort
from werkzeug.utils import secure_filename
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit
ALLOWED_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}
JOBS = {}
JOBS_LOCK = threading.Lock()
MODEL = None
MODEL_LOCK = threading.Lock()

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def run_command(args):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Command failed")[-1800:])
    return result.stdout.strip()

def get_duration(path):
    out = run_command([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ])
    return float(out)

def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def load_whisper_model(progress_callback=None):
    global MODEL
    with MODEL_LOCK:
        if MODEL is None:
            if progress_callback:
                progress_callback("Loading free local speech model (first run downloads it)…", 8)
            from faster_whisper import WhisperModel
            # CPU + int8 is intended for 16 GB RAM and Intel integrated graphics.
            MODEL = WhisperModel("base", device="cpu", compute_type="int8")
    return MODEL

HOOK_TERMS = [
    "the truth", "secret", "mistake", "never", "nobody", "everyone", "imagine",
    "what if", "why", "how", "the reason", "biggest", "shocking", "surprising",
    "i learned", "i realized", "i thought", "but then", "until", "finally",
    "changed my life", "the problem", "the answer", "here's", "here is",
    "the most important", "actually", "the first time", "one day", "suddenly",
    "believe me", "you need to", "don't", "didn't know", "worst", "best"
]
EMOTION_TERMS = [
    "love", "fear", "cry", "happy", "angry", "heart", "lost", "won", "regret",
    "dream", "pain", "amazing", "terrible", "beautiful", "scared", "crazy",
    "incredible", "impossible", "failure", "success", "money", "life", "death",
    "fight", "family", "friend", "alone", "survive", "shame", "proud"
]
VALUE_TERMS = [
    "because", "lesson", "learned", "realized", "reason", "important", "solution",
    "answer", "result", "method", "step", "first", "second", "tip", "advice",
    "understand", "remember", "difference", "works", "mistake", "instead"
]

def score_text(text, duration):
    low = text.lower()
    score = 42.0
    reasons = []
    hooks = [w for w in HOOK_TERMS if w in low]
    emotions = [w for w in EMOTION_TERMS if w in low]
    values = [w for w in VALUE_TERMS if w in low]
    if hooks:
        score += min(18, 6 + 3 * len(hooks))
        reasons.append("hook or curiosity language")
    if emotions:
        score += min(14, 5 + 2 * len(emotions))
        reasons.append("emotional language")
    if values:
        score += min(12, 4 + 2 * len(values))
        reasons.append("useful insight or takeaway")
    if "?" in text:
        score += 5
        reasons.append("question creates curiosity")
    if "!" in text:
        score += 2
    words = re.findall(r"\b[\w']+\b", text)
    density = len(words) / max(duration, 1)
    if 1.7 <= density <= 3.5:
        score += 7
        reasons.append("good speech density")
    elif density < 0.8:
        score -= 7
    if len(words) > 45:
        score += 4
    if len(words) < 12:
        score -= 8
    # Reward a clip that has a natural setup/payoff signal.
    if any(w in low for w in ["but", "however", "so", "that's why", "which means", "turns out"]):
        score += 4
        reasons.append("contrast or payoff signal")
    score = max(20, min(97, round(score)))
    if not reasons:
        reasons = ["coherent speech segment; review the preview before using"]
    return score, reasons

def make_candidates(segments, duration):
    # Create several overlapping transcript windows at common short-video lengths.
    # This is a transparent heuristic ranker, not a claim that virality can be guaranteed.
    if duration <= 25:
        lengths = [min(15, duration)]
    elif duration <= 55:
        lengths = [20, 30]
    elif duration <= 100:
        lengths = [20, 30, 45]
    else:
        lengths = [25, 35, 45, 60]
    candidates = []
    for win in lengths:
        if win < 5 or win > duration:
            continue
        stride = max(8, int(win * 0.45))
        starts = list(range(0, max(1, int(duration - win) + 1), stride))
        final_start = max(0, duration - win)
        if not starts or starts[-1] != final_start:
            starts.append(final_start)
        for start in starts:
            end = min(duration, start + win)
            parts = [s for s in segments if s["end"] > start and s["start"] < end]
            text = " ".join(s["text"].strip() for s in parts).strip()
            if len(text.split()) < 10:
                continue
            score, reasons = score_text(text, end-start)
            # Slight preference for clips with a clean opening phrase.
            first = text[:90].lower()
            if any(first.startswith(x) for x in ["why", "how", "the truth", "i never", "imagine", "what if"]):
                score = min(98, score + 4)
                reasons.insert(0, "strong opening phrase")
            candidates.append({
                "start": round(start, 2), "end": round(end, 2),
                "start_label": fmt_time(start), "end_label": fmt_time(end),
                "duration": round(end-start), "score": score,
                "text": text[:520], "reasons": reasons
            })
    candidates.sort(key=lambda c: c["score"], reverse=True)
    selected = []
    for c in candidates:
        # Avoid showing near-duplicate suggestions that cover almost the same moment.
        overlaps = False
        for s in selected:
            intersection = max(0, min(c["end"], s["end"]) - max(c["start"], s["start"]))
            shorter = min(c["end"]-c["start"], s["end"]-s["start"])
            if shorter and intersection / shorter > 0.62:
                overlaps = True
                break
        if not overlaps:
            selected.append(c)
        if len(selected) >= 12:
            break
    selected.sort(key=lambda c: c["start"])
    return selected

def is_youtube_url(raw_url):
    """Accept only normal YouTube watch, short, embed, and live URLs."""
    try:
        parsed = urlparse((raw_url or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed_hosts = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtube-nocookie.com", "youtube-nocookie.com"}
        return parsed.scheme in {"http", "https"} and host in allowed_hosts
    except Exception:
        return False

def download_youtube_job(job_id, url):
    """Download an accessible YouTube video with yt-dlp, then run local analysis."""
    def update(message, progress=None):
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id]["message"] = message
                if progress is not None:
                    JOBS[job_id]["progress"] = progress
    try:
        update("Connecting to YouTube and checking video access…", 3)
        try:
            import yt_dlp
        except ImportError:
            raise RuntimeError("YouTube support is missing. Stop the app and run: pip install -r requirements.txt")
        output_template = str(UPLOAD_DIR / f"{job_id}_youtube.%(ext)s")
        def progress_hook(data):
            status = data.get("status")
            if status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                got = data.get("downloaded_bytes") or 0
                pct = int(got * 100 / total) if total else 0
                update(f"Downloading YouTube video… {pct}%", min(30, 5 + int(pct * 0.25)))
            elif status == "finished":
                update("Download complete. Preparing video for analysis…", 32)
        options = {
            "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": 1024 * 1024 * 1024,
            "socket_timeout": 25,
            "retries": 2,
            "fragment_retries": 2,
            "progress_hooks": [progress_hook],
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            downloaded = Path(ydl.prepare_filename(info))
        # yt-dlp may merge separate streams to .mp4.
        possible = [downloaded, downloaded.with_suffix(".mp4")]
        filepath = next((x for x in possible if x.exists() and x.is_file()), None)
        if filepath is None:
            matches = sorted(UPLOAD_DIR.glob(f"{job_id}_youtube.*"), key=lambda x: x.stat().st_mtime, reverse=True)
            filepath = matches[0] if matches else None
        if filepath is None:
            raise RuntimeError("YouTube download finished but the video file could not be found.")
        with JOBS_LOCK:
            JOBS[job_id].update({"filepath": str(filepath), "filename": info.get("title", "YouTube video")[:180], "source": "youtube"})
        update("Video downloaded. Starting speech analysis…", 35)
        process_job(job_id, filepath)
    except Exception as exc:
        message = str(exc)
        if "Sign in" in message or "bot" in message.lower() or "confirm you’re not a bot" in message.lower():
            message = "YouTube restricted this video or requires sign-in. Try another public video, or upload a video file you are authorised to edit. Details: " + message[-700:]
        elif "maximum file size" in message.lower() or "larger than max-filesize" in message.lower():
            message = "This video exceeds the 1 GB YouTube-download limit. Try a shorter/lower-resolution video or upload a smaller file."
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id].update({"status": "error", "message": message[-1800:], "progress": 100})

def process_job(job_id, filepath):
    def update(message, progress=None):
        with JOBS_LOCK:
            JOBS[job_id]["message"] = message
            if progress is not None:
                JOBS[job_id]["progress"] = progress
    try:
        update("Checking video file…", 3)
        duration = get_duration(filepath)
        update("Loading local speech recognition model…", 8)
        model = load_whisper_model(update)
        update("Transcribing speech on your computer… this may take a while.", 15)
        segments_iter, info = model.transcribe(str(filepath), beam_size=3, vad_filter=True)
        segments = []
        last_progress = 15
        for seg in segments_iter:
            segments.append({"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()})
            p = min(78, 15 + int((seg.end / max(duration, 1)) * 62))
            if p >= last_progress + 3:
                update(f"Transcribing video… {min(99, int(seg.end/max(duration,1)*100))}% of timeline", p)
                last_progress = p
        if not segments:
            raise RuntimeError("No speech was detected. This tool currently finds moments from spoken audio, so try a video with clear speech.")
        update("Finding and ranking promising short-video moments…", 84)
        candidates = make_candidates(segments, duration)
        transcript = [{"start": s["start"], "end": s["end"], "label": fmt_time(s["start"]), "text": s["text"]} for s in segments]
        with JOBS_LOCK:
            JOBS[job_id].update({
                "status": "done", "progress": 100, "message": "Analysis complete.",
                "duration": duration, "duration_label": fmt_time(duration),
                "segments": transcript, "candidates": candidates,
                "language": getattr(info, "language", "unknown")
            })
    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id].update({"status": "error", "message": str(exc), "progress": 100})

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/analyze", methods=["POST"])
def analyze():
    if "video" not in request.files:
        return jsonify(error="Choose a video file first."), 400
    file = request.files["video"]
    if not file.filename or not allowed_file(file.filename):
        return jsonify(error="Choose a supported video file: MP4, MOV, MKV, WEBM, AVI, or M4V."), 400
    job_id = uuid.uuid4().hex
    filename = secure_filename(file.filename)
    if not filename:
        filename = "uploaded_video.mp4"
    filepath = UPLOAD_DIR / f"{job_id}_{filename}"
    file.save(filepath)
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "processing", "progress": 1, "message": "Upload received.", "filename": filename, "filepath": str(filepath)}
    threading.Thread(target=process_job, args=(job_id, filepath), daemon=True).start()
    return jsonify(job_id=job_id)

@app.route("/api/analyze_url", methods=["POST"])
def analyze_url():
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    if not url:
        return jsonify(error="Paste a YouTube video link first."), 400
    if not is_youtube_url(url):
        return jsonify(error="Enter a valid YouTube URL, such as https://www.youtube.com/watch?v=... or https://youtu.be/..."), 400
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "processing", "progress": 1, "message": "YouTube link received.", "filename": "YouTube video", "source": "youtube"}
    threading.Thread(target=download_youtube_job, args=(job_id, url), daemon=True).start()
    return jsonify(job_id=job_id)

@app.route("/api/source/<job_id>")
def source_video(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        path = job.get("filepath") if job else None
    if not path or not Path(path).is_file():
        abort(404)
    return send_file(path, conditional=True)

@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Job not found."), 404
        # Do not return internal filesystem path.
        safe = {k:v for k,v in job.items() if k != "filepath"}
        return jsonify(safe)

@app.route("/api/clip", methods=["POST"])
def create_clip():
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("job_id", ""))
    try:
        start = float(data.get("start"))
        end = float(data.get("end"))
    except (TypeError, ValueError):
        return jsonify(error="Enter valid start and end timestamps."), 400
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="Analyse a video successfully before creating a clip."), 400
    duration = float(job["duration"])
    if start < 0 or end <= start or end > duration + 0.05:
        return jsonify(error=f"Time range must be within 0 and {fmt_time(duration)}; final time must be after initial time."), 400
    if end - start > 600:
        return jsonify(error="For this prototype, each clip is limited to 10 minutes."), 400
    input_path = Path(job["filepath"])
    if not input_path.exists():
        return jsonify(error="Original upload is no longer available. Upload the video again."), 410
    clip_id = uuid.uuid4().hex[:10]
    out_name = f"clip_{clip_id}.mp4"
    out_path = OUTPUT_DIR / out_name
    try:
        # Accurate re-encode, broadly compatible MP4/H.264 + AAC.
        run_command([
            "ffmpeg", "-y", "-ss", str(start), "-i", str(input_path),
            "-t", str(end-start), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
            str(out_path)
        ])
    except Exception as exc:
        return jsonify(error="FFmpeg failed to create this clip. Check that FFmpeg is installed and the source video is valid. Details: " + str(exc)), 500
    return jsonify(
        download_url=f"/download/{out_name}", filename=out_name,
        start_label=fmt_time(start), end_label=fmt_time(end),
        duration=round(end-start, 2)
    )

@app.route("/download/<path:filename>")
def download(filename):
    if not re.fullmatch(r"clip_[a-f0-9]+\.mp4", filename):
        abort(404)
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True, download_name=filename)

@app.errorhandler(413)
def too_large(_):
    return jsonify(error="Upload exceeds the 2 GB limit. Try a smaller video."), 413

if __name__ == "__main__":
    print("\\nClipGenius AI is starting.")
    print("Open http://127.0.0.1:5000 in your browser.")
    print("Keep this terminal open while using the website.\\n")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
