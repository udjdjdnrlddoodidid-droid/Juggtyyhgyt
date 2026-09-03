import asyncio
import json
import os
import re
import secrets
from ipaddress import ip_address
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response

app = FastAPI(title="FilmBuff Subtitle Engine", version="1.0.0")

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
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/122 Safari/537.36 FilmBuff/1.0",
)

SEM = asyncio.Semaphore(MAX_CONCURRENT)
PERSIAN_LANGS = {"fa", "fas", "per", "pes", "fa-ir", "fas-ir", "persian", "farsi"}
PERSIAN_TITLE_RE = re.compile(r"(?:persian|farsi|فارسی|پارسی)", re.I)
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
    # Explicitly reject literal loopback/private/link-local addresses in production.
    try:
        ip = ip_address(h)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            # Localhost is allowed ONLY when explicitly listed, useful for local tests.
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
        raise HTTPException(status_code=415, detail="embedded extraction currently expects MKV")
    return raw


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


def _score_stream(st: dict, total_subs: int) -> int:
    tags = st.get("tags") or {}
    lang = str(tags.get("language") or "").strip().lower().replace("_", "-")
    title = str(tags.get("title") or "").strip()
    codec = str(st.get("codec_name") or "").strip().lower()
    disp = st.get("disposition") or {}

    score = 0
    if lang in PERSIAN_LANGS or lang.split("-")[0] in PERSIAN_LANGS:
        score += 1000
    if PERSIAN_TITLE_RE.search(title):
        score += 900
    if disp.get("forced"):
        score += 40
    if disp.get("default"):
        score += 25
    if codec in SUPPORTED_TEXT_CODECS:
        score += 20
    if total_subs == 1:
        score += 10
    return score


async def _probe(video_url: str) -> tuple[dict, list[dict]]:
    cmd = [
        "ffprobe", "-v", "error",
        "-rw_timeout", "30000000",
        "-user_agent", USER_AGENT,
        "-select_streams", "s",
        "-show_streams",
        "-of", "json",
        video_url,
    ]
    code, out, err = await _run(cmd, PROBE_TIMEOUT)
    if code != 0:
        detail = err.decode("utf-8", "replace")[-800:]
        raise HTTPException(status_code=502, detail="ffprobe failed: " + detail)
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except Exception:
        raise HTTPException(status_code=502, detail="ffprobe returned invalid JSON")
    streams = [x for x in (data.get("streams") or []) if x.get("codec_type") == "subtitle"]
    if not streams:
        raise HTTPException(status_code=404, detail="no subtitle tracks in MKV")
    return data, streams


def _pick_stream(streams: list[dict]) -> dict:
    ranked = sorted(
        streams,
        key=lambda st: (_score_stream(st, len(streams)), -int(st.get("index", 10**9))),
        reverse=True,
    )
    best = ranked[0]
    return best


async def _extract_vtt(video_url: str, stream: dict) -> bytes:
    idx = int(stream["index"])
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rw_timeout", "60000000",
        "-user_agent", USER_AGENT,
        "-i", video_url,
        "-map", f"0:{idx}",
        "-vn", "-an",
        "-c:s", "webvtt",
        "-f", "webvtt",
        "pipe:1",
    ]
    code, out, err = await _run(cmd, EXTRACT_TIMEOUT)
    if code != 0:
        detail = err.decode("utf-8", "replace")[-1000:]
        raise HTTPException(status_code=502, detail="ffmpeg subtitle extraction failed: " + detail)
    if not out or len(out) > MAX_VTT_BYTES:
        raise HTTPException(status_code=502, detail="empty/oversized subtitle output")
    txt = out.decode("utf-8", "replace").lstrip("\ufeff")
    if not txt.lstrip().startswith("WEBVTT") or "-->" not in txt:
        raise HTTPException(status_code=502, detail="ffmpeg did not return valid WebVTT")
    return txt.encode("utf-8")


@app.get("/")
async def root():
    return {"ok": True, "service": "FilmBuff Subtitle Engine", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "api_key_configured": bool(API_KEY),
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
        _, streams = await _probe(video_url)
    picked = _pick_stream(streams)
    def brief(st: dict):
        return {
            "index": st.get("index"),
            "codec": st.get("codec_name"),
            "language": (st.get("tags") or {}).get("language"),
            "title": (st.get("tags") or {}).get("title"),
            "default": bool((st.get("disposition") or {}).get("default")),
            "forced": bool((st.get("disposition") or {}).get("forced")),
            "score": _score_stream(st, len(streams)),
        }
    return {"ok": True, "selected": brief(picked), "tracks": [brief(x) for x in streams]}


@app.get("/extract")
async def extract(
    url: str = Query(..., min_length=10),
    authorization: str | None = Header(default=None),
    x_filmbuff_key: str | None = Header(default=None, alias="X-FilmBuff-Key"),
):
    _auth(authorization, x_filmbuff_key)
    video_url = _validate_url(url)
    async with SEM:
        _, streams = await _probe(video_url)
        picked = _pick_stream(streams)
        vtt = await _extract_vtt(video_url, picked)
    tags = picked.get("tags") or {}
    return Response(
        content=vtt,
        media_type="text/vtt",
        headers={
            "Cache-Control": "no-store",
            "X-FilmBuff-Track-Index": str(picked.get("index", "")),
            "X-FilmBuff-Track-Language": str(tags.get("language") or ""),
            "X-FilmBuff-Track-Title": str(tags.get("title") or "")[:160],
        },
    )
