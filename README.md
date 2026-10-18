# YT Archiver

YT Archiver is a free app you run on your own computer. It saves a YouTube playlist as MP4 files in a folder you choose.

Use it only for videos you own or have permission to save. The [license](LICENSE) does not grant any right to YouTube content.

## Features

- Paste a playlist link and save the videos, in playlist order, into a folder you choose.
- Pick quality from 4K down to 360p. The default is 1080p. A video without that quality uses the best stream at or below it.
- Pick the audio track: Hindi, English, Bangla, or Original. Hindi is tried first, then English, then Bangla, then the original track.
- Download 1 to 5 videos at once. The default is 1.
- Each video has a checkbox, a progress bar, and Retry if it fails. Stop and Resume the playlist while it runs.
- Files use the video title from the playlist. A character Windows cannot keep, such as `:`, is removed. A repeated title gets ` (2)`.
- Optional YouTube sign-in through a `cookies.txt` file, for the “Sign in to confirm you’re not a bot” check.
- Your folder, quality, audio, queue size, and cookies path are remembered on that computer. Nothing downloads until a folder is chosen.

## Setup

These steps are for Windows after you download the project.

1. On the GitHub page, choose **Code → Download ZIP**, then unzip it. Or clone the repository.
2. Install Python 3.12, FFmpeg, and Deno if they are missing. FFmpeg merges video and audio into one MP4. Deno lets yt-dlp read YouTube’s full quality list.

```text
winget install --id Python.Python.3.12 -e --scope user
winget install --id Gyan.FFmpeg -e
winget install --id DenoLand.Deno -e
```

3. Close the terminal and open a new one in the unzipped folder, so Python, FFmpeg, and Deno are on the PATH.
4. Create a virtual environment and install the packages:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No Node.js, database, or YouTube API key is required.

## Cookies

Leave Sign in empty when YouTube does not ask you to sign in.

If YouTube shows “Sign in to confirm you’re not a bot”:

1. In Chrome, install **Get cookies.txt LOCALLY** (Chrome Web Store id `cclelndahbckbenkjhflpdbgdldlbecc`). Use that extension. The older one named only “Get cookies.txt” has been reported as malware.
2. Open an Incognito window, allow the extension there, and sign in to YouTube.
3. In that same tab, open https://www.youtube.com/robots.txt and export the cookies as a Netscape file.
4. Close the Incognito window.
5. In YT Archiver, choose **Sign in → Browse** and pick that file.

The first line of the file must be `# Netscape HTTP Cookie File` or `# HTTP Cookie File`. It is a login session for your computer, and it expires. Export a new file when the bot message returns. Keep it private. The repository ignores `cookies.txt`, so a normal upload does not include it.

## Run

In the project folder:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000 in your browser. Press Ctrl+C in that terminal to stop the app. Closing the browser tab leaves it running.

## License

Free for personal, non-commercial use, and only for content you are allowed to save. See [LICENSE](LICENSE).
