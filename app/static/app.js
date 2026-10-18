const form = document.querySelector("#download-form");
const urlInput = document.querySelector("#playlist-url");
const downloadButton = document.querySelector("#download-button");
const outputDirInput = document.querySelector("#output-dir");
const browseButton = document.querySelector("#browse-button");
const audioSelect = document.querySelector("#audio");
const qualitySelect = document.querySelector("#quality");
const queueSizeInput = document.querySelector("#queue-size");
const cookiesInput = document.querySelector("#cookies-file");
const cookiesBrowseButton = document.querySelector("#cookies-browse");
const cookiesClearButton = document.querySelector("#cookies-clear");
const ffmpegWarning = document.querySelector("#ffmpeg-warning");
const formError = document.querySelector("#form-error");
const summary = document.querySelector("#summary");
const emptyState = document.querySelector("#empty-state");
const queue = document.querySelector("#queue");
const queueHeading = document.querySelector("#queue-heading");
const stopButton = document.querySelector("#stop-button");
const resumeButton = document.querySelector("#resume-button");

const STATUS_LABELS = {
  queued: "Queued",
  downloading: "Downloading",
  processing: "Saving MP4",
  done: "Saved",
  failed: "Failed",
  stopped: "Paused",
  skipped: "Skipped",
  stopping: "Stopping",
};

const ACTIVE_STATUSES = new Set(["preparing", "running", "stopping"]);
const BUSY_VIDEO = new Set(["queued", "downloading", "processing", "stopped"]);
const TOGGLE_STATUSES = new Set(["queued", "skipped", "stopped", "failed"]);
const AUDIO_CHOICES = new Set(["hi", "en", "bn", "original"]);
const QUALITY_CHOICES = new Set(["2160", "1440", "1080", "720", "480", "360"]);

let ffmpegReady = false;
let folderReady = false;
let savedFolder = "";
let savedAudio = "hi";
let savedQuality = "1080";
let savedConcurrency = 1;
let savedCookies = "";
let choosingFolder = false;
let choosingCookies = false;
let activeJobId = "";
let pollGeneration = 0;

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  if (!ffmpegReady) {
    showError("FFmpeg is not available, so videos cannot be saved as MP4s yet.");
    return;
  }

  const url = urlInput.value.trim();
  if (!outputDirInput.value.trim()) {
    showError("Choose a folder.");
    return;
  }
  if (!isPlaylistUrl(url)) {
    showError("Paste a YouTube playlist link. It should include list=.");
    return;
  }
  const concurrency = Number(queueSizeInput.value);
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 5) {
    showError("Enter a queue size from 1 to 5.");
    queueSizeInput.value = String(savedConcurrency);
    return;
  }

  setBusy(true);
  summary.textContent = "Starting…";

  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        output_dir: outputDirInput.value.trim(),
        audio: audioSelect.value,
        quality: Number(qualitySelect.value),
        cookies: cookiesInput.value.trim(),
        concurrency: Number(queueSizeInput.value),
      }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      setBusy(false);
      summary.textContent = "Waiting for a playlist.";
      return;
    }
    const job = await response.json();
    renderJob(job);
    poll(job.id);
  } catch {
    showError("The local server did not respond.");
    setBusy(false);
    summary.textContent = "Waiting for a playlist.";
  }
});

outputDirInput.addEventListener("change", () => {
  applyFolder(outputDirInput.value);
});

audioSelect.addEventListener("change", () => {
  applyAudio(audioSelect.value);
});

qualitySelect.addEventListener("change", () => {
  applyQuality(qualitySelect.value);
});

queueSizeInput.addEventListener("change", () => {
  applyConcurrency(queueSizeInput.value);
});

cookiesInput.addEventListener("change", () => {
  applyCookies(cookiesInput.value);
});

cookiesBrowseButton.addEventListener("click", async () => {
  clearError();
  choosingCookies = true;
  cookiesBrowseButton.disabled = true;
  cookiesBrowseButton.textContent = "Choosing…";
  cookiesInput.disabled = true;
  cookiesClearButton.disabled = true;
  try {
    const response = await fetch("/api/cookies/browse", { method: "POST" });
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    const data = await response.json();
    rememberCookies(data.cookies_file);
    if (!data.cancelled) {
      clearError();
    }
  } catch {
    showError("The file picker did not open.");
  } finally {
    choosingCookies = false;
    cookiesBrowseButton.textContent = "Browse";
    setBusy(false);
  }
});

cookiesClearButton.addEventListener("click", () => {
  applyCookies("");
});

browseButton.addEventListener("click", async () => {
  clearError();
  choosingFolder = true;
  browseButton.disabled = true;
  browseButton.textContent = "Choosing…";
  outputDirInput.disabled = true;
  try {
    const response = await fetch("/api/folder/browse", { method: "POST" });
    if (!response.ok) {
      showError(await errorMessage(response));
      return;
    }
    const data = await response.json();
    rememberFolder(data.output_dir);
    if (!data.cancelled) {
      clearError();
    }
  } catch {
    showError("The folder picker did not open.");
  } finally {
    choosingFolder = false;
    browseButton.textContent = "Browse";
    setBusy(false);
  }
});

stopButton.addEventListener("click", async () => {
  if (!activeJobId) {
    return;
  }
  stopButton.disabled = true;
  stopButton.textContent = "Stopping…";
  try {
    const response = await fetch(`/api/jobs/${activeJobId}/stop`, { method: "POST" });
    if (!response.ok) {
      showError(await errorMessage(response));
      stopButton.disabled = false;
      stopButton.textContent = "Stop";
      return;
    }
    renderJob(await response.json());
  } catch {
    showError("The local server did not respond.");
    stopButton.disabled = false;
    stopButton.textContent = "Stop";
  }
});

resumeButton.addEventListener("click", async () => {
  if (!activeJobId) {
    return;
  }
  clearError();
  resumeButton.disabled = true;
  try {
    const response = await fetch(`/api/jobs/${activeJobId}/resume`, { method: "POST" });
    if (!response.ok) {
      showError(await errorMessage(response));
      resumeButton.disabled = false;
      return;
    }
    const job = await response.json();
    renderJob(job);
    poll(job.id);
  } catch {
    showError("The local server did not respond.");
    resumeButton.disabled = false;
  }
});

loadInfo();

async function loadInfo() {
  try {
    const response = await fetch("/api/info");
    if (!response.ok) {
      throw new Error("info failed");
    }
    const info = await response.json();
    rememberFolder(info.output_dir);
    rememberAudio(info.audio);
    rememberQuality(info.quality);
    rememberConcurrency(info.concurrency);
    rememberCookies(info.cookies_file);
    ffmpegReady = Boolean(info.ffmpeg);
    const notes = [];
    if (!ffmpegReady) {
      notes.push("FFmpeg is not on PATH. Install it, open a new terminal, and start the server again.");
    }
    if (!info.deno) {
      notes.push("Deno is not on PATH. YouTube may hide some qualities until it is installed.");
    }
    ffmpegWarning.hidden = notes.length === 0;
    ffmpegWarning.textContent = notes.join(" ");
    setBusy(false);
    await restoreJob();
  } catch {
    outputDirInput.value = "";
    outputDirInput.placeholder = "Folder unavailable";
    showError("The local server did not respond.");
  }
}

function poll(jobId) {
  const generation = ++pollGeneration;
  window.setTimeout(() => trackJob(jobId, generation), 1000);
}

async function trackJob(jobId, generation) {
  if (generation !== pollGeneration) {
    return;
  }
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    if (generation !== pollGeneration) {
      return;
    }
    if (!response.ok) {
      showError(await errorMessage(response));
      finishPolling();
      return;
    }
    const job = await response.json();
    renderJob(job);
    if (isJobActive(job)) {
      window.setTimeout(() => trackJob(jobId, generation), 1000);
      return;
    }
    finishPolling();
  } catch {
    if (generation !== pollGeneration) {
      return;
    }
    showError("Lost contact with the local server.");
    finishPolling();
  }
}

function finishPolling() {
  setBusy(false);
}

async function restoreJob() {
  const job = await loadCurrentJob();
  if (!job) {
    return;
  }
  renderJob(job);
  if (ACTIVE_STATUSES.has(job.status)) {
    poll(job.id);
  }
}

async function loadCurrentJob() {
  try {
    const current = await fetch("/api/jobs/current", { cache: "no-store" });
    if (current.ok) {
      return current.json();
    }
  } catch {
    return null;
  }
  const stored = readStoredJobId();
  const fromStore = await fetchJob(stored);
  if (fromStore) {
    return fromStore;
  }
  try {
    const marker = await fetch("/static/active-job.json", { cache: "no-store" });
    if (!marker.ok) {
      return null;
    }
    const data = await marker.json();
    return fetchJob(typeof data.id === "string" ? data.id : "");
  } catch {
    return null;
  }
}

async function fetchJob(jobId) {
  if (!jobId) {
    return null;
  }
  try {
    const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
    if (response.status === 404) {
      if (readStoredJobId() === jobId) {
        localStorage.removeItem("playlistJobId");
      }
      return null;
    }
    if (!response.ok) {
      return null;
    }
    return response.json();
  } catch {
    return null;
  }
}

function rememberJob(jobId) {
  try {
    localStorage.setItem("playlistJobId", jobId);
  } catch {
    return;
  }
}

function readStoredJobId() {
  try {
    return localStorage.getItem("playlistJobId") || "";
  } catch {
    return "";
  }
}

function renderJob(job) {
  activeJobId = job.id;
  rememberJob(job.id);
  if (job.url) {
    urlInput.value = job.url;
  }
  if (job.output_dir) {
    rememberFolder(job.output_dir);
  }
  queueHeading.textContent = job.playlist_title || "Queue";
  summary.textContent = summaryFor(job);
  if (job.error && job.status === "error" && job.videos.length === 0) {
    showError(job.error);
  }
  renderVideos(job.videos, job.status);
  syncControls(job);
}

function summaryFor(job) {
  if (job.status === "preparing") {
    return "Reading the playlist…";
  }
  const saved = job.videos.filter((video) => video.status === "done").length;
  const failed = job.videos.filter((video) => video.status === "failed").length;
  const selected = job.videos.filter((video) => video.selected !== false).length;
  if (job.status === "stopping") {
    return "Stopping…";
  }
  if (job.status === "stopped") {
    return `Paused. ${saved} of ${selected} saved`;
  }
  if (job.status === "running") {
    return `${saved} of ${selected} saved`;
  }
  if (job.status === "done" && failed === 0) {
    return `${saved} saved`;
  }
  if (failed > 0 && saved > 0) {
    return `${saved} saved, ${failed} failed`;
  }
  return job.error || "The playlist could not be downloaded.";
}

function isJobActive(job) {
  if (!job || !ACTIVE_STATUSES.has(job.status)) {
    return false;
  }
  if (job.status === "preparing" || job.status === "stopping" || !job.videos.length) {
    return true;
  }
  return job.videos.some((video) => video.selected !== false && BUSY_VIDEO.has(video.status));
}

function syncControls(job) {
  const active = isJobActive(job);
  setBusy(active);
  stopButton.hidden = !active;
  resumeButton.hidden = !job || job.status !== "stopped";
  resumeButton.disabled = false;
  if (!stopButton.hidden) {
    stopButton.disabled = job.status === "stopping";
    stopButton.textContent = job.status === "stopping" ? "Stopping…" : "Stop";
  }
}

function renderVideos(videos, status) {
  emptyState.hidden = videos.length > 0;
  emptyState.textContent = status === "preparing"
    ? "Reading the playlist…"
    : "Nothing in the queue yet.";
  const existing = new Map(
    [...queue.children].map((node) => [node.dataset.index, node])
  );
  const seen = new Set();
  let previous = null;

  videos.forEach((video, index) => {
    const key = String(index);
    seen.add(key);
    let row = existing.get(key);
    if (row) {
      updateRow(row, video, index, status);
    } else {
      row = buildRow(video, index, status);
    }
    if (row.parentNode !== queue || row.previousElementSibling !== previous) {
      if (previous) {
        previous.after(row);
      } else {
        queue.prepend(row);
      }
    }
    previous = row;
  });

  for (const [id, node] of existing) {
    if (!seen.has(id)) {
      node.remove();
    }
  }
}

function buildRow(video, index, jobStatus) {
  const item = document.createElement("li");
  item.className = "item";
  item.dataset.index = String(index);

  const pick = document.createElement("input");
  pick.type = "checkbox";
  pick.className = "pick";
  pick.setAttribute("aria-label", `Include ${video.title}`);
  pick.addEventListener("change", () => {
    toggleVideo(Number(item.dataset.index), pick);
  });

  const number = document.createElement("span");
  number.className = "index";

  const meta = document.createElement("div");
  const title = document.createElement("p");
  title.className = "title";
  const reason = document.createElement("p");
  reason.className = "reason";
  const track = document.createElement("div");
  track.className = "track";
  const fill = document.createElement("span");
  fill.className = "fill";
  track.append(fill);
  meta.append(title, reason, track);

  const actions = document.createElement("div");
  actions.className = "item-actions";
  const status = document.createElement("span");
  status.className = "status";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "ghost slim retry";
  retry.textContent = "Retry";
  retry.addEventListener("click", () => {
    retryVideo(Number(item.dataset.index));
  });
  actions.append(status, retry);

  item.append(pick, number, meta, actions);
  updateRow(item, video, index, jobStatus);
  return item;
}

function updateRow(item, video, index, jobStatus) {
  item.dataset.index = String(index);
  const pick = item.querySelector(".pick");
  pick.checked = video.selected !== false;
  pick.disabled = !TOGGLE_STATUSES.has(video.status);
  pick.setAttribute("aria-label", `Include ${video.title}`);
  item.querySelector(".index").textContent = String(index + 1).padStart(2, "0");
  item.querySelector(".title").textContent = video.title;

  const reason = item.querySelector(".reason");
  reason.hidden = !video.error;
  reason.textContent = video.error || "";

  const progress = typeof video.progress === "number" ? video.progress : 0;
  item.querySelector(".track").hidden = false;
  item.querySelector(".fill").style.width = `${Math.max(0, Math.min(100, progress))}%`;

  const status = item.querySelector(".status");
  status.className = `status ${video.status}`;
  const label = STATUS_LABELS[video.status] || video.status;
  status.textContent =
    video.status === "downloading" && typeof video.progress === "number"
      ? `${label} ${Math.round(video.progress)}%`
      : label;

  const retry = item.querySelector(".retry");
  const failed = video.status === "failed";
  retry.hidden = !failed;
  retry.disabled = !failed || ACTIVE_STATUSES.has(jobStatus);
  retry.setAttribute("aria-label", `Retry ${video.title}`);
  if (failed && retry.disabled) {
    retry.title = "Wait until the current download finishes.";
  } else {
    retry.removeAttribute("title");
  }
}

async function retryVideo(index) {
  if (!activeJobId) {
    return;
  }
  clearError();
  const button = queue.querySelector(`[data-index="${index}"] .retry`);
  if (button) {
    button.disabled = true;
  }
  try {
    const response = await fetch(`/api/jobs/${activeJobId}/videos/${index}/retry`, {
      method: "POST",
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      if (button) {
        button.disabled = false;
      }
      return;
    }
    const job = await response.json();
    renderJob(job);
    poll(job.id);
  } catch {
    showError("The local server did not respond.");
    if (button) {
      button.disabled = false;
    }
  }
}

async function toggleVideo(index, pick) {
  if (!activeJobId) {
    return;
  }
  try {
    const response = await fetch(`/api/jobs/${activeJobId}/videos/${index}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected: pick.checked }),
    });
    if (!response.ok) {
      pick.checked = !pick.checked;
      showError(await errorMessage(response));
      return;
    }
    renderJob(await response.json());
  } catch {
    pick.checked = !pick.checked;
    showError("The local server did not respond.");
  }
}

function isPlaylistUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const allowed =
      host === "youtube.com" ||
      host === "www.youtube.com" ||
      host === "m.youtube.com" ||
      host === "music.youtube.com" ||
      host === "youtu.be" ||
      host === "www.youtu.be";
    return allowed && url.searchParams.has("list") && url.searchParams.get("list").trim() !== "";
  } catch {
    return false;
  }
}

function rememberFolder(path) {
  savedFolder = path || "";
  outputDirInput.value = savedFolder;
  folderReady = Boolean(savedFolder);
}

function rememberConcurrency(value) {
  const number = Number(value);
  savedConcurrency = Number.isInteger(number) && number >= 1 && number <= 5 ? number : 1;
  queueSizeInput.value = String(savedConcurrency);
}

async function applyConcurrency(value) {
  const previous = savedConcurrency;
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1 || number > 5) {
    showError("Enter a queue size from 1 to 5.");
    queueSizeInput.value = String(previous);
    return;
  }
  if (number === previous) {
    queueSizeInput.value = String(previous);
    return;
  }
  rememberConcurrency(number);
  try {
    const response = await fetch("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ concurrency: number }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      rememberConcurrency(previous);
      return;
    }
    const data = await response.json();
    rememberConcurrency(data.concurrency);
    clearError();
  } catch {
    showError("The local server did not respond.");
    rememberConcurrency(previous);
  }
}

function rememberQuality(quality) {
  const choice = String(quality);
  savedQuality = QUALITY_CHOICES.has(choice) ? choice : "1080";
  qualitySelect.value = savedQuality;
}

async function applyQuality(quality) {
  const previous = savedQuality;
  const choice = String(quality);
  if (!QUALITY_CHOICES.has(choice)) {
    showError("Choose 4K, 1440p, 1080p, 720p, 480p, or 360p.");
    qualitySelect.value = previous;
    return;
  }
  if (choice === previous) {
    qualitySelect.value = previous;
    return;
  }
  rememberQuality(choice);
  try {
    const response = await fetch("/api/quality", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quality: Number(choice) }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      rememberQuality(previous);
      return;
    }
    const data = await response.json();
    rememberQuality(data.quality);
    clearError();
  } catch {
    showError("The local server did not respond.");
    rememberQuality(previous);
  }
}

function rememberAudio(audio) {
  const choice = AUDIO_CHOICES.has(audio) ? audio : "hi";
  savedAudio = choice;
  audioSelect.value = choice;
}

async function applyAudio(audio) {
  const previous = savedAudio;
  if (!audio || audio === previous) {
    rememberAudio(previous);
    return;
  }
  rememberAudio(audio);
  try {
    const response = await fetch("/api/audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio: savedAudio }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      rememberAudio(previous);
      return;
    }
    const data = await response.json();
    rememberAudio(data.audio);
    clearError();
  } catch {
    showError("The local server did not respond.");
    rememberAudio(previous);
  }
}

function rememberCookies(path) {
  savedCookies = path || "";
  cookiesInput.value = savedCookies;
  cookiesClearButton.disabled = !savedCookies || choosingCookies;
}

async function applyCookies(path) {
  const next = path.trim();
  if (next === savedCookies) {
    cookiesInput.value = savedCookies;
    return;
  }
  try {
    const response = await fetch("/api/cookies", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: next }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      cookiesInput.value = savedCookies;
      return;
    }
    const data = await response.json();
    rememberCookies(data.cookies_file);
    clearError();
  } catch {
    showError("The local server did not respond.");
    cookiesInput.value = savedCookies;
  }
}

function setBusy(busy) {
  const locked = busy || choosingFolder || choosingCookies;
  downloadButton.disabled = locked || !ffmpegReady || !folderReady;
  downloadButton.textContent = busy ? "Downloading…" : "Download playlist";
  urlInput.disabled = locked;
  outputDirInput.disabled = locked;
  browseButton.disabled = locked;
  audioSelect.disabled = locked;
  qualitySelect.disabled = locked;
  queueSizeInput.disabled = locked;
  cookiesInput.disabled = locked;
  cookiesBrowseButton.disabled = locked;
  cookiesClearButton.disabled = locked || !savedCookies;
}

async function applyFolder(path) {
  const next = path.trim();
  if (!next || next === savedFolder) {
    outputDirInput.value = savedFolder;
    return;
  }
  try {
    const response = await fetch("/api/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: next }),
    });
    if (!response.ok) {
      showError(await errorMessage(response));
      outputDirInput.value = savedFolder;
      return;
    }
    const data = await response.json();
    rememberFolder(data.output_dir);
    setBusy(false);
    clearError();
  } catch {
    showError("The local server did not respond.");
    outputDirInput.value = savedFolder;
  }
}

async function errorMessage(response) {
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  const detail = body.detail;
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail) && detail[0] && detail[0].msg) {
    return detail[0].msg;
  }
  if (response.status === 409) {
    return "A playlist is already downloading.";
  }
  return "Something went wrong. Try that again.";
}

function showError(message) {
  formError.hidden = false;
  formError.textContent = message;
}

function clearError() {
  formError.hidden = true;
  formError.textContent = "";
}
