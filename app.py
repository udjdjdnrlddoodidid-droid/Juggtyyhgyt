import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from ipaddress import ip_address
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

APP_VERSION = "3.0.0"
app = FastAPI(title="FilmBuff Subtitle Engine", version=APP_VERSION)

API_KEY = os.getenv("FILMBUFF_SUBTITLE_API_KEY", "").strip()
ALLOWED_HOST_SUFFIXES = [
    x.strip().lower().lstrip(".")
    for x in os.getenv("ALLOWED_HOST_SUFFIXES", "abrtech.top,ir.cdn.ir").split(",")
    if x.strip()
]
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "1") or "1"))
PROBE_TIMEOUT = max(10, int(os.getenv("PROBE_TIMEOUT_SECONDS", "45") or "45"))
EXTRACT_TIMEOUT = max(60, int(os.getenv("EXTRACT_TIMEOUT_SECONDS", "900") or "900"))
MAX_VTT_BYTES = max(1024 * 1024, int(os.getenv("MAX_VTT_BYTES", str(12 * 1024 * 1024))))
JOB_TTL_SECONDS = max(600, int(os.getenv("JOB_TTL_SECONDS", "7200") or "7200"))
USER_AGENT = os.getenv(
    "UPSTREAM_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36 FilmBuff/3.0",
).strip()
# Important: raw CDN access is attempted first, matching Media3/VLC behavior.
# These are fallback referers only if the raw request fails.
UPSTREAM_REFERERS = [
    x.strip()
    for x in os.getenv("UPSTREAM_REFERERS", "https://www.rayanvand.top/,https://www.my-f2mx.top/").split(",")
    if x.strip()
]
UPSTREAM_ORIGIN = os.getenv("UPSTREAM_ORIGIN", "").strip()

SEM = asyncio.Semaphore(MAX_CONCURRENT)
JOBS: dict[str, dict] = {}
JOB_TASKS: dict[str, asyncio.Task] = {}

PERSIAN_LANGS = {"fa", "fas", "per", "pes", "fa-ir", "fas-ir", "persian", "farsi"}
PERSIAN_TITLE_RE = re.compile(r"(?:persian|farsi|فارسی|پارسی)", re.I)
FORCED_TITLE_RE = re.compile(r"(?:forced|signs?|songs?|فورس|اجباری)", re.I)
SUPPORTED_TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "text", "mov_text", "microdvd"}


def _auth(authorization: str | None, x_key: str | None) -> None:
    if not API_KEY:
        raise HTTPException(status_code=503, detail="FILMBUFF_SUBTITLE_API_KEY is not configured")
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    elif x_key:
        supplied = x_key.strip()
    if not supplied or not secrets.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=401, detail="unauthorized")


def _host_allowed(host: str) -> bool:
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return False
    try:
        ip = ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return h in ALLOWED_HOST_SUFFIXES
    except ValueError:
        pass
    return any(h == suffix or h.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _validate_url(raw: str) -> str:
    try:
        u = urlparse(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="bad url")
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise HTTPException(status_code=400, detail="only http/https URLs are supported")
    if not _host_allowed(u.hostname):
        raise HTTPException(status_code=403, detail=f"host not allowed: {u.hostname}")
    if not u.path.lower().endswith(".mkv"):
        raise HTTPException(status_code=415, detail="embedded subtitle extraction expects MKV")
    return raw


def _canonical_identity(raw: str) -> str:
    u = urlparse(raw)
    path = unquote(u.path or "/")
    m = re.search(r"/(Series|Serial|Movies?|Films?)/", path, re.I)
    if m:
        path = path[m.start():]
    path = re.sub(r"/{2,}", "/", path).lower()
    return path


def _job_id(raw: str) -> str:
    return hashlib.sha256(("v3|" + _canonical_identity(raw)).encode("utf-8")).hexdigest()[:32]


def _cleanup_jobs() -> None:
    now = time.time()
    dead = []
    for jid, j in JOBS.items():
        if j.get("status") in {"done", "error"} and now - float(j.get("updated", now)) > JOB_TTL_SECONDS:
            dead.append(jid)
    for jid in dead:
        JOBS.pop(jid, None)
        t = JOB_TASKS.pop(jid, None)
        if t and not t.done():
            t.cancel()


def _input_http_options(referer: str | None = None) -> list[str]:
    opts = [
        "-rw_timeout", "60000000",
        "-user_agent", USER_AGENT,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
    ]
    headers = []
    if referer:
        headers.append(f"Referer: {referer}")
    if referer and UPSTREAM_ORIGIN:
        headers.append(f"Origin: {UPSTREAM_ORIGIN}")
    if headers:
        headers.append("Accept: */*")
        opts += ["-headers", "\r\n".join(headers) + "\r\n"]
    return opts


async def _run(cmd: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except Exception:
            pass
        raise RuntimeError("ffmpeg/ffprobe timeout")
    return proc.returncode, out, err


def _stream_lang(st: dict) -> str:
    return str((st.get("tags") or {}).get("language") or "").strip().lower().replace("_", "-")


def _stream_title(st: dict) -> str:
    return str((st.get("tags") or {}).get("title") or "").strip()


def _score_stream(st: dict, total_subs: int) -> int:
    lang = _stream_lang(st)
    title = _stream_title(st)
    codec = str(st.get("codec_name") or "").strip().lower()
    disp = st.get("disposition") or {}
    if codec not in SUPPORTED_TEXT_CODECS:
        return -100000
    score = 100
    base_lang = lang.split("-")[0] if lang else ""
    if lang in PERSIAN_LANGS or base_lang in PERSIAN_LANGS:
        score += 2000
    if PERSIAN_TITLE_RE.search(title):
        score += 1800
    if disp.get("default"):
        score += 80
    if disp.get("forced"):
        score -= 20
    if FORCED_TITLE_RE.search(title):
        score -= 80
    if total_subs == 1:
        score += 250
    return score


def _brief(st: dict, total: int) -> dict:
    return {
        "index": st.get("index"),
        "codec": st.get("codec_name"),
        "language": _stream_lang(st) or None,
        "title": _stream_title(st) or None,
        "default": bool((st.get("disposition") or {}).get("default")),
        "forced": bool((st.get("disposition") or {}).get("forced")),
        "score": _score_stream(st, total),
    }


async def _probe_once(video_url: str, referer: str | None) -> list[dict]:
    cmd = ["ffprobe", "-v", "error"]
    cmd += _input_http_options(referer)
    # Matroska track metadata is near the beginning. Keep probing bounded.
    cmd += [
        "-probesize", "1048576",
        "-analyzeduration", "2000000",
        "-select_streams", "s",
        "-show_streams",
        "-of", "json",
        video_url,
    ]
    code, out, err = await _run(cmd, PROBE_TIMEOUT)
    if code != 0:
        detail = err.decode("utf-8", "replace")[-1200:]
        raise RuntimeError(detail or f"ffprobe exit {code}")
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except Exception as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc
    streams = [x for x in (data.get("streams") or []) if x.get("codec_type") == "subtitle"]
    if not streams:
        raise RuntimeError("no subtitle tracks in MKV")
    return streams


async def _probe(video_url: str) -> list[dict]:
    errors = []
    # Raw request first = closest to Android Media3/VLC.
    attempts = [None] + UPSTREAM_REFERERS
    seen = set()
    for referer in attempts:
        if referer in seen:
            continue
        seen.add(referer)
        try:
            return await _probe_once(video_url, referer)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("ffprobe failed: " + " | ".join(errors[-3:])[-1800:])


def _pick_stream(streams: list[dict]) -> dict:
    text_streams = [st for st in streams if str(st.get("codec_name") or "").lower() in SUPPORTED_TEXT_CODECS]
    if not text_streams:
        codecs = sorted({str(st.get("codec_name") or "unknown") for st in streams})
        raise RuntimeError("no convertible text subtitle track: " + ",".join(codecs))
    ranked = sorted(
        text_streams,
        key=lambda st: (_score_stream(st, len(streams)), -int(st.get("index", 10**9))),
        reverse=True,
    )
    best = ranked[0]
    if _score_stream(best, len(streams)) < 1000 and len(text_streams) > 1:
        defaults = [st for st in text_streams if (st.get("disposition") or {}).get("default")]
        if defaults:
            best = defaults[0]
    return best


async def _extract_once(video_url: str, stream: dict, referer: str | None) -> bytes:
    idx = int(stream["index"])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    cmd += _input_http_options(referer)
    cmd += [
        "-i", video_url,
        "-map", f"0:{idx}",
        "-vn", "-an",
        "-c:s", "webvtt",
        "-f", "webvtt",
        "pipe:1",
    ]
    code, out, err = await _run(cmd, EXTRACT_TIMEOUT)
    if code != 0:
        detail = err.decode("utf-8", "replace")[-1600:]
        raise RuntimeError(detail or f"ffmpeg exit {code}")
    if not out or len(out) > MAX_VTT_BYTES:
        raise RuntimeError("empty/oversized subtitle output")
    text = out.decode("utf-8", "replace").lstrip("\ufeff")
    if not text.lstrip().startswith("WEBVTT") or "-->" not in text:
        raise RuntimeError("ffmpeg did not return valid WebVTT")
    return text.encode("utf-8")


async def _extract_vtt(video_url: str, stream: dict) -> bytes:
    errors = []
    attempts = [None] + UPSTREAM_REFERERS
    seen = set()
    for referer in attempts:
        if referer in seen:
            continue
        seen.add(referer)
        try:
            return await _extract_once(video_url, stream, referer)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("ffmpeg subtitle extraction failed: " + " | ".join(errors[-3:])[-2200:])


async def _process_job(jid: str) -> None:
    job = JOBS.get(jid)
    if not job:
        return
    job["status"] = "running"
    job["updated"] = time.time()
    try:
        async with SEM:
            streams = await _probe(job["url"])
            picked = _pick_stream(streams)
            job["selected"] = _brief(picked, len(streams))
            job["tracks"] = [_brief(x, len(streams)) for x in streams]
            job["updated"] = time.time()
            vtt = await _extract_vtt(job["url"], picked)
        job["vtt"] = vtt
        job["status"] = "done"
        job["error"] = None
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)[-2500:]
    finally:
        job["updated"] = time.time()


def _start_or_get_job(video_url: str) -> dict:
    _cleanup_jobs()
    jid = _job_id(video_url)
    now = time.time()
    job = JOBS.get(jid)
    if job:
        # A failed job may be retried with a fresh rotating CDN URL.
        if job.get("status") == "error" and now - float(job.get("updated", now)) > 15:
            job = None
        else:
            # Keep the freshest signed CDN URL while a queued job has not started.
            if job.get("status") == "queued":
                job["url"] = video_url
            return job
    job = {
        "id": jid,
        "url": video_url,
        "canonical": _canonical_identity(video_url),
        "status": "queued",
        "created": now,
        "updated": now,
        "error": None,
        "vtt": None,
        "selected": None,
        "tracks": None,
    }
    JOBS[jid] = job
    task = asyncio.create_task(_process_job(jid))
    JOB_TASKS[jid] = task
    task.add_done_callback(lambda _t, _jid=jid: JOB_TASKS.pop(_jid, None))
    return job


def _job_public(job: dict) -> dict:
    return {
        "ok": job.get("status") != "error",
        "id": job.get("id"),
        "status": job.get("status"),
        "selected": job.get("selected"),
        "error": job.get("error"),
        "created": job.get("created"),
        "updated": job.get("updated"),
        "retry_after_ms": 2500 if job.get("status") in {"queued", "running"} else 0,
    }


@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "FilmBuff Subtitle Engine",
        "version": APP_VERSION,
        "mode": "async-jobs",
        "usage": "/health, POST /jobs?url=..., GET /jobs/{id}, GET /jobs/{id}/result",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "mode": "async-jobs",
        "api_key_configured": bool(API_KEY),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "allowed_hosts": ALLOWED_HOST_SUFFIXES,
        "max_concurrent": MAX_CONCURRENT,
        "jobs": len(JOBS),
    }


@app.post("/jobs")
async def create_job(
    url: str = Query(..., min_length=10),
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    video_url = _validate_url(url)
    job = _start_or_get_job(video_url)
    return _job_public(job)


@app.get("/jobs/{job_id}")
async def job_status(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    _cleanup_jobs()
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_public(job)


@app.get("/jobs/{job_id}/result")
async def job_result(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    _cleanup_jobs()
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="job not ready")
    if job.get("status") == "error":
        raise HTTPException(status_code=422, detail=job.get("error") or "extraction failed")
    vtt = job.get("vtt")
    if not vtt:
        raise HTTPException(status_code=500, detail="result missing")
    selected = job.get("selected") or {}
    return Response(
        content=vtt,
        media_type="text/vtt",
        headers={
            "Cache-Control": "no-store",
            "X-FilmBuff-Version": APP_VERSION,
            "X-FilmBuff-Job": job_id,
            "X-FilmBuff-Track-Index": str(selected.get("index", "")),
            "X-FilmBuff-Track-Language": str(selected.get("language") or "")[:40],
            "X-FilmBuff-Track-Title": str(selected.get("title") or "")[:160],
        },
    )


# Kept only for manual diagnostics. FilmBuff Worker no longer uses this route,
# because a full remote MKV extraction can legitimately take more than a proxy timeout.
@app.get("/extract")
async def extract_sync(
    url: str = Query(..., min_length=10),
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    video_url = _validate_url(url)
    async with SEM:
        streams = await _probe(video_url)
        picked = _pick_stream(streams)
        vtt = await _extract_vtt(video_url, picked)
    return Response(content=vtt, media_type="text/vtt", headers={"Cache-Control": "no-store"})
