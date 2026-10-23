# Render deployment notes

- Create a Render **Web Service** connected to this GitHub repository.
- Choose **Docker** as the runtime if asked.
- The Dockerfile installs FFmpeg and Python dependencies, including `av<19` for faster-whisper compatibility.
- The service uses one Gunicorn worker because the current app stores job state in process memory.
- Free instances have limited CPU/RAM and may sleep when idle. First transcription may be slow while the Whisper model downloads.
- Local files on free hosted instances are ephemeral. Uploaded videos and exported clips can disappear after restarts/redeploys; do not treat this as persistent storage.
- YouTube downloading may fail for some videos due to YouTube restrictions or yt-dlp changes.
- Check the service price before deploying; do not select a paid instance if the goal is zero cost.
