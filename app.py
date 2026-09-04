import asyncio
import json
import os
import re
import secrets
import shutil
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response

APP_VERSION = "2.0.0"
app = FastAPI(title="FilmBuff Subtitle Engine", version=APP_VERSION)

API_KEY = os.getenv("FILMBUFF_SUBTITLE_API_KEY", "").strip()
ALLOWED_HOST_SUFFIXES = [
    x.strip().lower().lstrip(".")
    for x in os.getenv("ALLOWED_HOST_SUFFIXES", "abrtech.top,ir.cdn.ir").split(",")
    if x.strip()
]
MAX_CONCURRENT = max(1, int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "1") or "1"))
PROBE_TIMEOUT = max(10, int(os.getenv("PROBE_TIMEOUT_SECONDS", "45") or "45"))
EXTRACT_TIMEOUT = max(30, int(os.getenv("EXTRACT_TIMEOUT_SECONDS", "210") or "210"))
MAX_VTT_BYTES = max(1024 * 1024, int(os.getenv("MAX_VTT_BYTES", str(12 * 1024 * 1024))))
USER_AGENT = os.getenv(
    "UPSTREAM_USER_AGENT",
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36 FilmBuff/2.0",
).strip()
UPSTREAM_REFERERS = [
    x.strip()
    for x in os.getenv(
        "UPSTREAM_REFERERS",
        "https://www.rayanvand.top/,https://www.my-f2mx.top/",
    ).split(",")
    if x.strip()
]
UPSTREAM_ORIGIN = os.getenv("UPSTREAM_ORIGIN", "https://www.rayanvand.top").strip()

SEM = asyncio.Semaphore(MAX_CONCURRENT)

PERSIAN_LANGS = {
    "fa", "fas", "per", "pes", "fa-ir", "fas-ir", "persian", "farsi",
}
PERSIAN_TITLE_RE = re.compile(r"(?:persian|farsi|فارسی|پارسی)", re.I)
FORCED_TITLE_RE = re.compile(r"(?:forced|signs?|songs?|فورس|اجباری)", re.I)
SUPPORTED_TEXT_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "text", "mov_text", "microdvd",
}


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
    if UPSTREAM_ORIGIN:
        headers.append(f"Origin: {UPSTREAM_ORIGIN}")
    headers.append("Accept: */*")
    if headers:
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
        raise HTTPException(status_code=504, detail="ffmpeg/ffprobe timeout")
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
    cmd += [
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
        raise HTTPException(status_code=404, detail="no subtitle tracks in MKV")
    return streams


async def _probe(video_url: str) -> list[dict]:
    errors = []
    referers = UPSTREAM_REFERERS or [None]
    for referer in referers:
        try:
            return await _probe_once(video_url, referer)
        except HTTPException:
            raise
        except Exception as exc:
            errors.append(str(exc))
    msg = " | ".join(errors[-2:])[-1500:]
    raise HTTPException(status_code=502, detail="ffprobe failed: " + msg)


def _pick_stream(streams: list[dict]) -> dict:
    text_streams = [st for st in streams if str(st.get("codec_name") or "").lower() in SUPPORTED_TEXT_CODECS]
    if not text_streams:
        codecs = sorted({str(st.get("codec_name") or "unknown") for st in streams})
        raise HTTPException(status_code=415, detail="no WebVTT-convertible text subtitle track: " + ",".join(codecs))

    ranked = sorted(
        text_streams,
        key=lambda st: (_score_stream(st, len(streams)), -int(st.get("index", 10**9))),
        reverse=True,
    )
    best = ranked[0]

    # If metadata is weak but there is a single text track, use it. Otherwise
    # prefer the default text track, matching Media3's practical fallback.
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
        detail = err.decode("utf-8", "replace")[-1400:]
        raise RuntimeError(detail or f"ffmpeg exit {code}")
    if not out or len(out) > MAX_VTT_BYTES:
        raise RuntimeError("empty/oversized subtitle output")
    text = out.decode("utf-8", "replace").lstrip("\ufeff")
    if not text.lstrip().startswith("WEBVTT") or "-->" not in text:
        raise RuntimeError("ffmpeg did not return valid WebVTT")
    return text.encode("utf-8")


async def _extract_vtt(video_url: str, stream: dict) -> bytes:
    errors = []
    referers = UPSTREAM_REFERERS or [None]
    for referer in referers:
        try:
            return await _extract_once(video_url, stream, referer)
        except HTTPException:
            raise
        except Exception as exc:
            errors.append(str(exc))
    msg = " | ".join(errors[-2:])[-1800:]
    raise HTTPException(status_code=502, detail="ffmpeg subtitle extraction failed: " + msg)


@app.get("/")
async def root():
    return {
        "ok": True,
        "service": "FilmBuff Subtitle Engine",
        "version": APP_VERSION,
        "usage": "/health, /probe?url=..., /extract?url=...",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "version": APP_VERSION,
        "api_key_configured": bool(API_KEY),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "allowed_hosts": ALLOWED_HOST_SUFFIXES,
        "max_concurrent": MAX_CONCURRENT,
    }


@app.get("/probe")
async def probe(
    url: str = Query(..., min_length=10),
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    video_url = _validate_url(url)
    async with SEM:
        streams = await _probe(video_url)
    picked = _pick_stream(streams)
    return {
        "ok": True,
        "selected": _brief(picked, len(streams)),
        "tracks": [_brief(x, len(streams)) for x in streams],
    }


@app.get("/extract")
async def extract(
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
    tags = picked.get("tags") or {}
    return Response(
        content=vtt,
        media_type="text/vtt",
        headers={
            "Cache-Control": "no-store",
            "X-FilmBuff-Version": APP_VERSION,
            "X-FilmBuff-Track-Index": str(picked.get("index", "")),
            "X-FilmBuff-Track-Language": str(tags.get("language") or "")[:40],
            "X-FilmBuff-Track-Title": str(tags.get("title") or "")[:160],
        },
    )
