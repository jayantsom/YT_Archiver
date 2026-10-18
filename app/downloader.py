"""Download a YouTube playlist into the local downloads folder."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_DIR = ROOT / "downloads"
SETTINGS_PATH = ROOT / "settings.json"

# Prefer 1080p. If that height is missing, take the best at or below it, then the best stream at all.
DEFAULT_AUDIO = "hi"
AUDIO_CHOICES = {"hi", "en", "bn", "original"}
DEFAULT_QUALITY = 1080
QUALITY_CHOICES = (2160, 1440, 1080, 720, 480, 360)
DEFAULT_CONCURRENCY = 1
MAX_CONCURRENCY = 5

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}


class JobBusyError(Exception):
    """Raised when a playlist download is already running."""


class DownloadStopped(Exception):
    """Raised when the user stops the active video."""


class _SilentLogger:
    def debug(self, msg: str, *args: object, **kwargs: object) -> None:
        return None

    def info(self, msg: str, *args: object, **kwargs: object) -> None:
        return None

    def warning(self, msg: str, *args: object, **kwargs: object) -> None:
        return None

    def error(self, msg: str, *args: object, **kwargs: object) -> None:
        return None


_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_state: dict[str, object] = {
    "active": False,
    "output_dir": None,
    "audio": DEFAULT_AUDIO,
    "quality": DEFAULT_QUALITY,
    "cookies_file": None,
    "concurrency": DEFAULT_CONCURRENCY,
}
_cancel = threading.Event()
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def output_dir() -> Path | None:
    with _lock:
        path = _state["output_dir"]
    if isinstance(path, Path):
        return path
    return None


def set_output_dir(path: str) -> Path:
    destination = _prepare_dir(path)
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        _state["output_dir"] = destination
        _write_settings(output=destination)
    return destination


def browse_output_dir() -> Path | None:
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        initial = _state["output_dir"]
    chosen = _ask_directory(str(initial) if isinstance(initial, Path) else "")
    if not chosen:
        return None
    return set_output_dir(chosen)


def _read_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def _stored_output_dir() -> Path | None:
    raw = _read_settings().get("output_dir")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        candidate = Path(raw).resolve(strict=False)
    except (OSError, ValueError):
        return None
    if _is_default_dir(candidate) or not candidate.is_dir():
        return None
    return candidate


def normalize_quality(value: object, *, fallback: int | None = None) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if isinstance(fallback, int) and not isinstance(fallback, bool) and fallback in QUALITY_CHOICES:
            return fallback
        return DEFAULT_QUALITY
    text = str(value).strip().lower().rstrip("p")
    if text == "4k":
        text = "2160"
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose 4K, 1440p, 1080p, 720p, 480p, or 360p.") from exc
    if number not in QUALITY_CHOICES:
        raise ValueError("Choose 4K, 1440p, 1080p, 720p, 480p, or 360p.")
    return number


def _stored_quality() -> int:
    try:
        return normalize_quality(_read_settings().get("quality"))
    except ValueError:
        return DEFAULT_QUALITY


def _stored_audio() -> str:
    raw = _read_settings().get("audio")
    if isinstance(raw, str) and raw.strip().lower() in AUDIO_CHOICES:
        return raw.strip().lower()
    return DEFAULT_AUDIO


def normalize_concurrency(value: object, *, fallback: int | None = None) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        if isinstance(fallback, int) and not isinstance(fallback, bool) and 1 <= fallback <= MAX_CONCURRENCY:
            return fallback
        return DEFAULT_CONCURRENCY
    if isinstance(value, bool):
        raise ValueError(f"Enter a queue size from 1 to {MAX_CONCURRENCY}.")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"Enter a queue size from 1 to {MAX_CONCURRENCY}.")
        value = int(value)
    try:
        number = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Enter a queue size from 1 to {MAX_CONCURRENCY}.") from exc
    if number < 1 or number > MAX_CONCURRENCY:
        raise ValueError(f"Enter a queue size from 1 to {MAX_CONCURRENCY}.")
    return number


def _stored_concurrency() -> int:
    raw = _read_settings().get("concurrency")
    try:
        return normalize_concurrency(raw)
    except ValueError:
        return DEFAULT_CONCURRENCY


def _stored_cookies_file() -> str | None:
    raw = _read_settings().get("cookies_file")
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw.strip().strip('"'))
    if candidate.is_file():
        return str(candidate)
    return None


def _is_default_dir(path: Path) -> bool:
    try:
        return path.resolve(strict=False) == DOWNLOADS_DIR.resolve(strict=False)
    except OSError:
        return False


_state["output_dir"] = _stored_output_dir()
_state["audio"] = _stored_audio()
_state["quality"] = _stored_quality()
_state["cookies_file"] = _stored_cookies_file()
_state["concurrency"] = _stored_concurrency()


def _prepare_dir(path: str) -> Path:
    cleaned = path.strip().strip('"')
    if not cleaned:
        raise ValueError("Choose a folder.")
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Use a full folder path, such as D:\\Videos.")
    try:
        candidate = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError("That folder path is not valid.") from exc
    if candidate.exists() and not candidate.is_dir():
        raise ValueError("That path is a file. Choose a folder.")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValueError("That folder could not be created. Check that you can write there.") from exc
    return candidate


def _write_settings(
    *,
    output: Path | None = None,
    audio: str | None = None,
    quality: int | None = None,
    cookies: str | None = None,
    update_cookies: bool = False,
    concurrency: int | None = None,
) -> None:
    saved = _read_settings()
    if output is not None:
        saved["output_dir"] = str(output)
    if audio is not None:
        saved["audio"] = audio
    if quality is not None:
        saved["quality"] = quality
    if update_cookies:
        if cookies:
            saved["cookies_file"] = cookies
        else:
            saved.pop("cookies_file", None)
    if concurrency is not None:
        saved["concurrency"] = concurrency
    SETTINGS_PATH.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")


def _ask_directory(initial: str) -> str | None:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    launcher = str(pythonw if pythonw.is_file() else executable)
    env = os.environ.copy()
    if initial and not _is_default_dir(Path(initial)):
        env["PLAYLIST_INITIAL_DIR"] = initial
    result = subprocess.run(
        [launcher, "-c", _FOLDER_PICKER],
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "The folder picker could not open.")
    chosen = result.stdout.decode("utf-8", errors="replace").strip()
    return chosen or None


def _ask_file(initial: str) -> str | None:
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    launcher = str(pythonw if pythonw.is_file() else executable)
    env = os.environ.copy()
    if initial:
        env["PLAYLIST_COOKIE_DIR"] = initial
    result = subprocess.run(
        [launcher, "-c", _COOKIE_PICKER],
        capture_output=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or "The file picker could not open.")
    chosen = result.stdout.decode("utf-8", errors="replace").strip()
    return chosen or None


_FOLDER_PICKER = r"""
import os
import sys
import tkinter as tk
from tkinter import filedialog

initial = os.environ.get("PLAYLIST_INITIAL_DIR") or None
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
    root.update_idletasks()
    chosen = filedialog.askdirectory(
        parent=root,
        initialdir=initial,
        title="Choose a download folder",
        mustexist=False,
    )
finally:
    root.destroy()
sys.stdout.buffer.write((chosen or "").encode("utf-8"))
"""


_COOKIE_PICKER = r"""
import os
import sys
import tkinter as tk
from tkinter import filedialog

initial = os.environ.get("PLAYLIST_COOKIE_DIR") or None
root = tk.Tk()
root.withdraw()
try:
    root.attributes("-topmost", True)
    root.update_idletasks()
    chosen = filedialog.askopenfilename(
        parent=root,
        initialdir=initial,
        title="Choose a cookies.txt file",
        filetypes=[("Cookies file", "*.txt"), ("All files", "*.*")],
    )
finally:
    root.destroy()
sys.stdout.buffer.write((chosen or "").encode("utf-8"))
"""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def deno_available() -> bool:
    return shutil.which("deno") is not None


def validate_playlist_url(url: str) -> str:
    cleaned = url.strip()
    if not cleaned or len(cleaned) > 2048:
        raise ValueError("Enter a YouTube playlist link.")

    parsed = urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in ALLOWED_HOSTS:
        raise ValueError("Enter a YouTube playlist link.")

    list_id = (parse_qs(parsed.query).get("list") or [""])[0].strip()
    if not list_id:
        raise ValueError("That link has no playlist. Paste a URL that includes list=.")
    return cleaned


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return copy.deepcopy(job)


def current_job() -> dict | None:
    """Return the running playlist, or the latest one if nothing is running."""
    with _lock:
        latest = None
        active = None
        for job in _jobs.values():
            latest = job
            if job["status"] in {"preparing", "running", "stopping"}:
                active = job
        chosen = active or latest
        if chosen is None:
            return None
        return copy.deepcopy(chosen)


def audio_preference() -> str:
    with _lock:
        value = _state.get("audio")
    if isinstance(value, str) and value in AUDIO_CHOICES:
        return value
    return DEFAULT_AUDIO


def quality_preference() -> int:
    with _lock:
        value = _state.get("quality")
    try:
        return normalize_quality(value)
    except ValueError:
        return DEFAULT_QUALITY


def set_quality(value: object) -> int:
    chosen = normalize_quality(value)
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        _state["quality"] = chosen
        _write_settings(quality=chosen)
        for job in _jobs.values():
            job["quality"] = chosen
    return chosen


def set_audio(value: str) -> str:
    chosen = normalize_audio(value)
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        _state["audio"] = chosen
        _write_settings(audio=chosen)
        for job in _jobs.values():
            job["audio"] = chosen
    return chosen


def concurrency_preference() -> int:
    with _lock:
        value = _state.get("concurrency")
    try:
        return normalize_concurrency(value)
    except ValueError:
        return DEFAULT_CONCURRENCY


def set_concurrency(value: object) -> int:
    chosen = normalize_concurrency(value)
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        _state["concurrency"] = chosen
        _write_settings(concurrency=chosen)
        for job in _jobs.values():
            job["concurrency"] = chosen
    return chosen


def normalize_audio(value: str | None, *, fallback: str | None = None) -> str:
    if value is not None and str(value).strip():
        cleaned = str(value).strip().lower()
        if cleaned not in AUDIO_CHOICES:
            raise ValueError("Choose Hindi, English, Bangla, or Original.")
        return cleaned
    if isinstance(fallback, str) and fallback.strip().lower() in AUDIO_CHOICES:
        return fallback.strip().lower()
    if value is None and fallback is None:
        raise ValueError("Choose Hindi, English, Bangla, or Original.")
    return DEFAULT_AUDIO


def formats_for(audio: str | None, quality: object = None) -> tuple[str, str]:
    """Return the chosen-height format and the 403 fallback, in the chosen language order."""
    try:
        chosen = normalize_audio(audio)
    except ValueError:
        chosen = DEFAULT_AUDIO
    try:
        height = normalize_quality(quality)
    except ValueError:
        height = DEFAULT_QUALITY
    languages = _language_order(chosen)
    primary = _video_audio_chain(languages, height)
    capped = f"best[height<={height}]"
    selected = f"{primary}/{capped}"
    lower = next((choice for choice in QUALITY_CHOICES if choice < height), None)
    if lower is None:
        return selected, selected
    return selected, f"{_video_audio_chain(languages, lower)}/{selected}"


def _language_order(audio: str) -> tuple[str, ...]:
    if audio == "original":
        return ()
    return (audio, *[code for code in ("hi", "en", "bn") if code != audio])


def _audio_selector(code: str) -> str:
    return f"bestaudio[language^={code}][language!*=desc]"


def _video_audio_chain(languages: tuple[str, ...], height: int) -> str:
    video = f"bestvideo[height<={height}]"
    steps = [f"{video}+{_audio_selector(code)}" for code in languages]
    steps.append(f"{video}+bestaudio")
    return "/".join(steps)


def cookies_file() -> str | None:
    with _lock:
        value = _state.get("cookies_file")
    return value if isinstance(value, str) and value else None


def set_cookies_file(path: str) -> str | None:
    chosen = str(_prepare_cookies(path)) if path.strip() else None
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        _state["cookies_file"] = chosen
        _write_settings(cookies=chosen, update_cookies=True)
        for job in _jobs.values():
            job["cookies_file"] = chosen
    return chosen


def browse_cookies_file() -> str | None:
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        current = _state.get("cookies_file")
    initial = ""
    if isinstance(current, str) and current:
        parent = Path(current).parent
        if parent.is_dir():
            initial = str(parent)
    chosen = _ask_file(initial)
    if not chosen:
        return None
    return set_cookies_file(chosen)


def _prepare_cookies(path: str) -> Path:
    cleaned = path.strip().strip('"')
    if not cleaned:
        raise ValueError("Choose a cookies.txt file.")
    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Use the full path to the cookies.txt file.")
    try:
        candidate = candidate.resolve(strict=False)
    except OSError as exc:
        raise ValueError("That cookies file path is not valid.") from exc
    if not candidate.is_file():
        raise ValueError("That cookies file was not found.")
    try:
        size = candidate.stat().st_size
        head = candidate.read_bytes()[:200]
    except OSError as exc:
        raise ValueError("That cookies file could not be read.") from exc
    if size <= 0:
        raise ValueError("That cookies file is empty.")
    if size > 2_000_000 or b"\x00" in head:
        raise ValueError("That file is not a cookies.txt file.")
    return candidate


def _cookie_copy(path: str | None) -> Path | None:
    """Give each download its own cookies file so parallel saves do not clash."""
    if not isinstance(path, str) or not path.strip():
        return None
    handle = tempfile.NamedTemporaryFile(prefix="playlist-cookies-", suffix=".txt", delete=False)
    handle.close()
    dest = Path(handle.name)
    try:
        shutil.copyfile(path, dest)
    except OSError:
        dest.unlink(missing_ok=True)
        raise
    return dest


def _cookie_options(path: str | None) -> dict:
    if isinstance(path, str) and path.strip():
        return {"cookiefile": path}
    return {}


def _resolve_cookies(value: str | None, *, fallback: str | None) -> str | None:
    if value is None:
        if not fallback:
            return None
        return str(_prepare_cookies(fallback))
    if not str(value).strip():
        return None
    return str(_prepare_cookies(value))


def start_job(
    url: str,
    folder: str | None = None,
    audio: str | None = None,
    cookies: str | None = None,
    concurrency: object = None,
    quality: object = None,
) -> dict:
    cleaned = validate_playlist_url(url)
    if not ffmpeg_available():
        raise RuntimeError("FFmpeg is not installed or not on PATH.")
    destination = _prepare_dir(folder) if folder is not None else None

    with _lock:
        if _state["active"]:
            raise JobBusyError()
        if destination is None:
            saved = _state["output_dir"]
            destination = saved if isinstance(saved, Path) else None
        if destination is None:
            raise ValueError("Choose a folder.")
        stored_audio = _state.get("audio")
        chosen_audio = normalize_audio(
            audio,
            fallback=stored_audio if isinstance(stored_audio, str) else DEFAULT_AUDIO,
        )
        stored_cookies = _state.get("cookies_file")
        chosen_cookies = _resolve_cookies(
            cookies,
            fallback=stored_cookies if isinstance(stored_cookies, str) else None,
        )
        chosen_concurrency = normalize_concurrency(
            concurrency,
            fallback=_state.get("concurrency") if isinstance(_state.get("concurrency"), int) else DEFAULT_CONCURRENCY,
        )
        stored_quality = _state.get("quality")
        chosen_quality = normalize_quality(
            quality,
            fallback=stored_quality if isinstance(stored_quality, int) else DEFAULT_QUALITY,
        )
        _state["output_dir"] = destination
        _state["audio"] = chosen_audio
        _state["cookies_file"] = chosen_cookies
        _state["concurrency"] = chosen_concurrency
        _state["quality"] = chosen_quality
        _write_settings(
            output=destination,
            audio=chosen_audio,
            cookies=chosen_cookies,
            update_cookies=True,
            concurrency=chosen_concurrency,
            quality=chosen_quality,
        )
        destination.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "id": job_id,
            "url": cleaned,
            "status": "preparing",
            "output_dir": str(destination),
            "audio": chosen_audio,
            "cookies_file": chosen_cookies,
            "concurrency": chosen_concurrency,
            "quality": chosen_quality,
            "playlist_title": None,
            "error": None,
            "listed": False,
            "videos": [],
        }
        _state["active"] = True
        _cancel.clear()

    threading.Thread(
        target=_execute,
        args=(job_id, cleaned, destination),
        daemon=True,
    ).start()
    job = get_job(job_id)
    if job is None:
        raise RuntimeError("Could not start the download.")
    return job


def stop_job(job_id: str) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in {"preparing", "running", "stopping"}:
            raise ValueError("That download is not running.")
        job["status"] = "stopping"
    _cancel.set()
    found = get_job(job_id)
    if found is None:
        raise KeyError(job_id)
    return found


def resume_job(job_id: str) -> dict:
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] != "stopped":
            raise ValueError("That download is not paused.")
        _cancel.clear()
        job["status"] = "running"
        job["error"] = None
        _state["active"] = True
        destination = Path(str(job["output_dir"]))
        url = str(job["url"])

    threading.Thread(
        target=_execute,
        args=(job_id, url, destination),
        daemon=True,
    ).start()
    found = get_job(job_id)
    if found is None:
        raise KeyError(job_id)
    return found


def retry_video(job_id: str, index: int) -> dict:
    with _lock:
        if _state["active"]:
            raise JobBusyError()
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        videos = job["videos"]
        if index < 0 or index >= len(videos):
            raise KeyError(index)
        video = videos[index]
        if video["status"] != "failed" or not video["selected"]:
            raise ValueError("Only a failed video can be retried.")
        video["status"] = "queued"
        video["progress"] = 0
        video["error"] = None
        job["status"] = "running"
        job["error"] = None
        _cancel.clear()
        _state["active"] = True
        destination = Path(str(job["output_dir"]))

    threading.Thread(
        target=_retry_execute,
        args=(job_id, index, destination),
        daemon=True,
    ).start()
    found = get_job(job_id)
    if found is None:
        raise KeyError(job_id)
    return found


def set_video_selected(job_id: str, index: int, selected: bool) -> dict:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        videos = job["videos"]
        if index < 0 or index >= len(videos):
            raise KeyError(index)
        video = videos[index]
        if video["status"] in {"downloading", "processing", "done"}:
            raise ValueError("That video has already started.")
        video["selected"] = selected
        if selected and video["status"] in {"skipped", "failed", "stopped"}:
            video["status"] = "queued"
            video["error"] = None
        elif not selected and video["status"] in {"queued", "stopped", "failed"}:
            video["status"] = "skipped"
            video["error"] = None
    found = get_job(job_id)
    if found is None:
        raise KeyError(job_id)
    return found


def _execute(job_id: str, url: str, destination: Path) -> None:
    try:
        with _lock:
            listed = bool(_jobs[job_id]["listed"])
        if not listed:
            with _lock:
                cookies = _jobs[job_id].get("cookies_file")
            title, videos = _list_entries(url, cookies if isinstance(cookies, str) else None)
            with _lock:
                job = _jobs[job_id]
                job["playlist_title"] = title
                job["videos"] = videos
                job["listed"] = True
                if _cancel.is_set():
                    job["status"] = "stopped"
                    job["error"] = None
                    return
                job["status"] = "running"
        elif _cancel.is_set():
            _mark_job_stopped(job_id)
            return

        _download_all(job_id, destination)
    except Exception as exc:
        if _cancel.is_set():
            _mark_job_stopped(job_id)
            return
        with _lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = _short_error(exc)
    finally:
        with _lock:
            _state["active"] = False


def _list_entries(url: str, cookies: str | None = None) -> tuple[str | None, list[dict]]:
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
        "logger": _SilentLogger(),
        **_cookie_options(cookies),
    }
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("Could not read that playlist.")

    ordered: list[tuple[int, dict]] = []
    for position, entry in enumerate(info.get("entries") or [], start=1):
        if not entry or not entry.get("id"):
            continue
        playlist_index = entry.get("playlist_index")
        sort_index = playlist_index if isinstance(playlist_index, int) else position
        ordered.append((sort_index, entry))
    ordered.sort(key=lambda item: item[0])

    used_names: set[str] = set()
    videos: list[dict] = []
    for _, entry in ordered:
        title = entry.get("title") or "Untitled"
        videos.append(
            {
                "id": entry["id"],
                "title": title,
                "filename": _unique_stem(title, used_names),
                "selected": True,
                "status": "queued",
                "progress": 0,
                "error": None,
            }
        )

    if not videos:
        raise RuntimeError("No videos found in that playlist. It may be private or empty.")

    title = info.get("title")
    if title in {None, "NA", url}:
        title = None
    return title, videos


def _download_all(job_id: str, destination: Path) -> None:
    workers = _job_concurrency(job_id)
    cursor = [0]

    def worker() -> None:
        while not _cancel.is_set():
            claimed = _claim_next(job_id, cursor)
            if claimed is None:
                return
            index, video = claimed
            finished = destination / f"{video['filename']}.mp4"
            if finished.is_file() and finished.stat().st_size > 0:
                _update_video(job_id, index, status="done", progress=100, error=None)
                continue
            try:
                _download_one(job_id, index, video, destination)
            except DownloadStopped:
                _update_video(job_id, index, status="stopped")
                return
            except Exception as exc:
                if _cancel.is_set():
                    _update_video(job_id, index, status="stopped")
                    return
                _update_video(job_id, index, status="failed", error=_short_error(exc))
            _settle_finished_job(job_id)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if _cancel.is_set():
        _mark_job_stopped(job_id)
        return
    _settle_job(job_id)


def _job_concurrency(job_id: str) -> int:
    with _lock:
        raw = _jobs[job_id].get("concurrency")
        if raw is None:
            raw = _state.get("concurrency")
    try:
        return normalize_concurrency(raw)
    except ValueError:
        return DEFAULT_CONCURRENCY


def _claim_next(job_id: str, cursor: list[int]) -> tuple[int, dict] | None:
    with _lock:
        videos = _jobs[job_id]["videos"]
        while cursor[0] < len(videos):
            index = cursor[0]
            cursor[0] += 1
            video = videos[index]
            if not video["selected"]:
                if video["status"] != "skipped":
                    video["status"] = "skipped"
                continue
            if video["status"] == "done":
                continue
            return index, dict(video)
        return None


def _retry_execute(job_id: str, index: int, destination: Path) -> None:
    try:
        if _cancel.is_set():
            _mark_job_stopped(job_id)
            return
        with _lock:
            video = dict(_jobs[job_id]["videos"][index])
        try:
            _download_one(job_id, index, video, destination, fresh=True)
        except DownloadStopped:
            _update_video(job_id, index, status="stopped")
            _mark_job_stopped(job_id)
            return
        except Exception as exc:
            if _cancel.is_set():
                _update_video(job_id, index, status="stopped")
                _mark_job_stopped(job_id)
                return
            _update_video(job_id, index, status="failed", error=_short_error(exc))
        _settle_job(job_id)
    except Exception as exc:
        if _cancel.is_set():
            _mark_job_stopped(job_id)
            return
        with _lock:
            job = _jobs[job_id]
            job["status"] = "error"
            job["error"] = _short_error(exc)
    finally:
        with _lock:
            _state["active"] = False


def _download_one(
    job_id: str,
    index: int,
    video: dict,
    destination: Path,
    *,
    fresh: bool = False,
) -> None:
    if _cancel.is_set():
        raise DownloadStopped()
    if _saved_file(destination, video):
        _update_video(job_id, index, status="done", progress=100, error=None)
        return
    if fresh:
        _clear_incomplete(destination, str(video["filename"]))

    with _lock:
        stored = _jobs[job_id].get("audio")
        quality = _jobs[job_id].get("quality")
    formats = formats_for(stored if isinstance(stored, str) else DEFAULT_AUDIO, quality)
    refused: BaseException | None = None
    for attempt, fmt in enumerate(formats):
        try:
            _download_with_format(job_id, index, video, destination, fmt)
            return
        except DownloadStopped:
            raise
        except Exception as exc:
            if _cancel.is_set():
                raise DownloadStopped()
            if not _is_forbidden(exc):
                if refused is not None:
                    raise refused
                raise
            refused = exc
            if attempt == len(formats) - 1:
                raise
            _clear_incomplete(destination, str(video["filename"]))
            _update_video(job_id, index, status="downloading", progress=0, error=None)
            time.sleep(1)
            if _cancel.is_set():
                raise DownloadStopped()


def _download_with_format(
    job_id: str,
    index: int,
    video: dict,
    destination: Path,
    fmt: str,
) -> None:
    last_emit = {"at": 0.0}

    def hook(data: dict) -> None:
        if _cancel.is_set():
            raise DownloadStopped()
        status = data.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_emit["at"] < 0.3:
                return
            last_emit["at"] = now
            _update_video(job_id, index, status="downloading", progress=_percent(data))
        elif status == "finished":
            _update_video(job_id, index, status="processing", progress=100)

    def postprocessor_hook(data: dict) -> None:
        if _cancel.is_set():
            raise DownloadStopped()
        if data.get("status") in {"started", "processing", "finished"}:
            _update_video(job_id, index, status="processing", progress=100)

    stem = str(video["filename"]).replace("%", "%%")
    cookie_copy = _cookie_copy(_job_cookies(job_id))
    options = {
        "format": fmt,
        "merge_output_format": "mp4",
        "outtmpl": str(destination / f"{stem}.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nooverwrites": True,
        "windowsfilenames": True,
        "continuedl": True,
        "retries": 3,
        "fragment_retries": 3,
        "postprocessors": [
            {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
        ],
        "logger": _SilentLogger(),
        "progress_hooks": [hook],
        "postprocessor_hooks": [postprocessor_hook],
        **_cookie_options(str(cookie_copy) if cookie_copy else None),
    }
    _update_video(job_id, index, status="downloading", progress=video.get("progress") or 0)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={video['id']}"])
    finally:
        if cookie_copy is not None:
            cookie_copy.unlink(missing_ok=True)
    if _cancel.is_set():
        raise DownloadStopped()
    if not _saved_file(destination, video):
        raise RuntimeError("The video file was not saved.")
    _update_video(job_id, index, status="done", progress=100, error=None)


def _settle_finished_job(job_id: str) -> None:
    """Mark the playlist finished as soon as nothing is left to download."""
    if _cancel.is_set():
        return
    with _lock:
        job = _jobs.get(job_id)
        if job is None or job["status"] != "running":
            return
        selected = [video for video in job["videos"] if video["selected"]]
        if any(video["status"] in {"queued", "downloading", "processing", "stopped"} for video in selected):
            return
        failed = [video for video in selected if video["status"] == "failed"]
        if selected and len(failed) == len(selected):
            job["status"] = "error"
            job["error"] = "Every selected video failed."
            return
        job["status"] = "done"
        job["error"] = None


def _settle_job(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        selected = [video for video in job["videos"] if video["selected"]]
        if any(video["status"] in {"queued", "stopped"} for video in selected):
            job["status"] = "stopped"
            job["error"] = None
            return
        failed = [video for video in selected if video["status"] == "failed"]
        if selected and len(failed) == len(selected):
            job["status"] = "error"
            job["error"] = "Every selected video failed."
            return
        job["status"] = "done"
        job["error"] = None


def _mark_job_stopped(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        for video in job["videos"]:
            if video["status"] in {"downloading", "processing"}:
                video["status"] = "stopped"
        job["status"] = "stopped"
        job["error"] = None


def _safe_stem(title: str) -> str:
    characters: list[str] = []
    for char in title.replace("\u00a0", " ").strip():
        if char in '<>:"/\\|?*' or ord(char) < 32:
            characters.append(" ")
        else:
            characters.append(char)
    stem = " ".join("".join(characters).split()).rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED:
        return "video"
    return stem[:150]


def _unique_stem(title: str, used: set[str]) -> str:
    base = _safe_stem(title)
    candidate = base
    number = 2
    while candidate.casefold() in used:
        candidate = f"{base} ({number})"
        number += 1
    used.add(candidate.casefold())
    return candidate


def _update_video(job_id: str, index: int, **fields: object) -> None:
    with _lock:
        _jobs[job_id]["videos"][index].update(fields)


def _percent(data: dict) -> float | None:
    downloaded = data.get("downloaded_bytes") or 0
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    if total:
        return round(max(0.0, min(100.0, downloaded / total * 100)), 1)
    fragment_index = data.get("fragment_index")
    fragment_count = data.get("fragment_count")
    if fragment_index and fragment_count:
        return round(max(0.0, min(100.0, fragment_index / fragment_count * 100)), 1)
    return None


def _saved_file(destination: Path, video: dict) -> bool:
    finished = destination / f"{video['filename']}.mp4"
    try:
        return finished.is_file() and finished.stat().st_size > 0
    except OSError:
        return False


def _clear_incomplete(destination: Path, stem: str) -> None:
    """Drop partial files so the next try requests a new stream instead of resuming a refused one."""
    finished = f"{stem}.mp4"
    prefix = f"{stem}."
    try:
        entries = list(destination.iterdir())
    except OSError:
        return
    for path in entries:
        if not path.is_file() or path.name == finished or not path.name.startswith(prefix):
            continue
        try:
            path.unlink()
        except OSError:
            continue


def _job_cookies(job_id: str) -> str | None:
    with _lock:
        value = _jobs[job_id].get("cookies_file")
    return value if isinstance(value, str) and value else None


def _is_bot_check(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not a bot" in text or "sign in to confirm" in text


def _is_forbidden(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "403" in text or "forbidden" in text


def _short_error(exc: BaseException) -> str:
    if _is_bot_check(exc):
        return (
            "YouTube asked you to sign in. Export a fresh cookies.txt from the browser "
            "where you are signed in to YouTube, choose that file, then retry."
        )
    if _is_forbidden(exc):
        return "YouTube refused this video (HTTP 403). Use Retry to request it again."
    text = str(exc)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    message = lines[-1] if lines else "Download failed."
    if message.startswith("ERROR:"):
        message = message[6:].strip()
    if len(message) > 280:
        return message[:277] + "..."
    return message
