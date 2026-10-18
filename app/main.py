"""Local page and API for playlist downloads."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.downloader import (
    JobBusyError,
    audio_preference,
    concurrency_preference,
    quality_preference,
    browse_cookies_file,
    browse_output_dir,
    cookies_file,
    deno_available,
    ffmpeg_available,
    current_job,
    get_job,
    output_dir,
    resume_job,
    retry_video,
    set_audio,
    set_concurrency,
    set_quality,
    set_cookies_file,
    set_output_dir,
    set_video_selected,
    start_job,
    stop_job,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Playlist downloader")


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    output_dir: str | None = Field(default=None, max_length=1024)
    audio: str | None = Field(default=None, max_length=32)
    cookies: str | None = Field(default=None, max_length=1024)
    concurrency: int | None = None
    quality: int | None = None


class AudioRequest(BaseModel):
    audio: str = Field(min_length=1, max_length=32)


class QueueRequest(BaseModel):
    concurrency: int


class QualityRequest(BaseModel):
    quality: int


class CookiesRequest(BaseModel):
    path: str = Field(default="", max_length=1024)


class FolderRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class SelectionRequest(BaseModel):
    selected: bool


@app.get("/api/info")
def info() -> dict:
    chosen = output_dir()
    return {
        "output_dir": str(chosen) if chosen else None,
        "audio": audio_preference(),
        "concurrency": concurrency_preference(),
        "quality": quality_preference(),
        "cookies_file": cookies_file(),
        "ffmpeg": ffmpeg_available(),
        "deno": deno_available(),
    }


@app.post("/api/audio")
def update_audio(body: AudioRequest) -> dict:
    try:
        chosen = set_audio(body.audio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    return {"audio": chosen}


@app.post("/api/quality")
def update_quality(body: QualityRequest) -> dict:
    try:
        chosen = set_quality(body.quality)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    return {"quality": chosen}


@app.post("/api/queue")
def update_queue(body: QueueRequest) -> dict:
    try:
        chosen = set_concurrency(body.concurrency)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    return {"concurrency": chosen}


@app.post("/api/cookies")
def update_cookies(body: CookiesRequest) -> dict:
    try:
        chosen = set_cookies_file(body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    return {"cookies_file": chosen}


@app.post("/api/cookies/browse")
def browse_cookies() -> dict:
    try:
        chosen = browse_cookies_file()
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if chosen is None:
        current = cookies_file()
        return {"cookies_file": current, "cancelled": True}
    return {"cookies_file": chosen, "cancelled": False}


@app.post("/api/folder")
def update_folder(body: FolderRequest) -> dict:
    try:
        path = set_output_dir(body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    return {"output_dir": str(path)}


@app.post("/api/folder/browse")
def browse_folder() -> dict:
    try:
        path = browse_output_dir()
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if path is None:
        current = output_dir()
        return {"output_dir": str(current) if current else None, "cancelled": True}
    return {"output_dir": str(path), "cancelled": False}


@app.post("/api/download")
def download(body: DownloadRequest) -> dict:
    try:
        return start_job(
            body.url,
            body.output_dir,
            body.audio,
            body.cookies,
            body.concurrency,
            body.quality,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/stop")
def stop(job_id: str) -> dict:
    try:
        return stop_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No download found with that id.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/resume")
def resume(job_id: str) -> dict:
    try:
        return resume_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="No download found with that id.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc


@app.post("/api/jobs/{job_id}/videos/{index}/retry")
def retry(job_id: str, index: int) -> dict:
    try:
        return retry_video(job_id, index)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="That video is not in this playlist.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail="A playlist is already downloading.") from exc


@app.post("/api/jobs/{job_id}/videos/{index}")
def select_video(job_id: str, index: int, body: SelectionRequest) -> dict:
    try:
        return set_video_selected(job_id, index, body.selected)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="That video is not in this playlist.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@app.get("/api/jobs/current")
def current(response: Response) -> dict:
    _no_store(response)
    found = current_job()
    if found is None:
        raise HTTPException(status_code=404, detail="No playlist is in progress.")
    return found


@app.get("/api/jobs/{job_id}")
def job(job_id: str, response: Response) -> dict:
    _no_store(response)
    found = get_job(job_id)
    if found is None:
        raise HTTPException(status_code=404, detail="No download found with that id.")
    return found


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
