# ClipGenius AI — working local website

This version implements the workflow requested:
1. Upload a video file **or paste a YouTube URL**.
2. For a YouTube URL, the app uses `yt-dlp` to download an accessible public video to your computer (maximum configured size: 1 GB). Some videos cannot be fetched because YouTube may require sign-in or block downloads.
3. The app transcribes speech locally using the free `faster-whisper` model.
4. It ranks timestamped moments with a transparent heuristic score and explains the signals.
5. Select a suggested moment or enter manual start/end timestamps.
6. The app cuts that exact portion with FFmpeg and provides an MP4 download.

## Important, honest limitations
- This is a local website, running on your computer. It is not yet published to a public URL.
- No paid API is used. The first transcription downloads the free Whisper `base` model, and CPU transcription may be slow.
- "Viral potential" is an estimate based on transcript signals, not a guarantee or a trained prediction of future views.
- It analyses speech/transcript. It does not yet understand every visual event, facial expression, or on-screen action.
- YouTube link support is included through `yt-dlp`, but it cannot bypass private-video restrictions, sign-in requirements, age gates, regional restrictions, or other access controls. YouTube changes can occasionally break downloads until `yt-dlp` is updated.
- Only upload videos you own or are authorised to edit.
- FFmpeg must be installed and available on PATH.

## Windows setup (beginner-friendly)
1. Install Python 3.11 or 3.12 from https://www.python.org/downloads/ . During install, tick **Add Python to PATH**.
2. Install FFmpeg:
   - Easiest option if `winget` is available: open Command Prompt or PowerShell and run `winget install Gyan.FFmpeg`.
   - Close and reopen the terminal after installation.
   - Check with `ffmpeg -version` and `ffprobe -version`.
3. Extract this ZIP to a normal folder, e.g. `C:\ClipGenius`.
4. Open that folder in VS Code.
5. In VS Code choose Terminal → New Terminal.
6. Run these commands one at a time:

   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   python app.py
   ```

7. Open http://127.0.0.1:5000 in Chrome or Edge.
8. Either paste a public YouTube URL and click **Fetch and analyse YouTube video**, or choose a local video file and click **Analyse uploaded video**. Start with a short video (1–3 minutes).
9. The first analysis downloads the speech model; allow it to finish. Select a suggested moment, or type exact start and end times, then click **Cut this portion and prepare download**.

Keep the terminal open while using the site. Stop the server with Ctrl+C.

## Free usage and machine notes
Your 16 GB RAM and Intel integrated graphics are suitable for the CPU/int8 `base` model. It may take a while for long videos. The app uses local CPU and local disk, and does not require an API key or subscription. Ensure you have several GB free disk space for model downloads and temporary videos.

## Troubleshooting
- `ffmpeg not found`: install FFmpeg, restart VS Code/terminal, then run `ffmpeg -version`.
- YouTube link fails: update yt-dlp with `python -m pip install --upgrade yt-dlp`; try a public video that does not require sign-in. You can always use a local video file if you are authorised to edit it.
- `No module named flask` / `faster_whisper`: activate `.venv` and run `pip install -r requirements.txt`.
- Upload / analysis is slow: start with a short, low-resolution video and clear speech.
- No speech detected: this version requires spoken audio to suggest moments.
- Windows firewall: the Flask server binds to `127.0.0.1`, so it is only accessible on your computer by default.
- If the `py` command fails, use `python` instead if Python is installed and added to PATH.

## Project files
- `app.py`: web server, local transcription, candidate scoring, exact clip generation and download.
- `templates/index.html`: responsive website UI.
- `requirements.txt`: free Python dependencies, including `yt-dlp` for YouTube URLs.
- `uploads/`: temporary uploaded videos.
- `outputs/`: generated MP4 clips.
