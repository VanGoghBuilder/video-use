"""Transcribe a video with iFlytek Long Form ASR.

Extracts mono 16kHz audio via ffmpeg, uploads it to iFlytek's long-form
transcription API, polls until complete, then normalizes the response to the
word-level JSON shape that the rest of video-use expects:

    {"provider": "xfyun_lfasr", "text": "...", "words": [...]}

Cached: if the output file already exists, the upload is skipped.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language cn
    python helpers/transcribe.py <video_path> --num-speakers 2
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from requests import Session
from requests.exceptions import RequestException


LFASR_BASE_URL = "https://raasr.xfyun.cn/api"
SLICE_SIZE = 10 * 1024 * 1024


def build_session() -> Session:
    """Build a requests session that first tries the host-provided HTTP proxy.

    In this environment, the default localhost:3128 proxy may block domains with a
    generic allowlist error even when the host-side Claude proxy can reach them.
    We prefer the host HTTP proxy when present, and fall back to the ambient
    requests behavior otherwise.
    """
    session = requests.Session()

    host_http_proxy_port = os.environ.get("CLAUDE_CODE_HOST_HTTP_PROXY_PORT")
    if host_http_proxy_port:
        host_http_proxy = f"http://localhost:{host_http_proxy_port}"
        session.proxies.update({"http": host_http_proxy, "https": host_http_proxy})
        session.trust_env = False
    return session


def request_with_fallback(
    session: Session,
    method: str,
    url: str,
    **kwargs,
) -> requests.Response:
    """Send a request with a direct-connect fallback and clearer network errors."""
    try:
        return session.request(method, url, **kwargs)
    except RequestException as first_error:
        fallback = requests.Session()
        fallback.trust_env = False
        try:
            return fallback.request(method, url, **kwargs)
        except RequestException as second_error:
            raise RuntimeError(
                "xfyun network request failed. "
                f"proxy-path error: {first_error!r}; "
                f"direct-path error: {second_error!r}. "
                "Likely causes in this environment: the configured proxy blocks raasr.xfyun.cn, "
                "or direct DNS/network access is unavailable."
            ) from second_error


def load_dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def load_credentials() -> tuple[str, str]:
    env_file = load_dotenv()
    app_id = os.environ.get("XFYUN_APP_ID") or env_file.get("XFYUN_APP_ID", "")
    secret_key = os.environ.get("XFYUN_SECRET_KEY") or env_file.get("XFYUN_SECRET_KEY", "")
    if not app_id or not secret_key:
        sys.exit(
            "XFYUN_APP_ID and XFYUN_SECRET_KEY not found in .env or environment. "
            "Create ~/Developer/video-use/.env with both values."
        )
    return app_id, secret_key


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_signa(app_id: str, secret_key: str, ts: str) -> str:
    """iFlytek LFASR signature: base64(hmac_sha1(md5(app_id + ts), secret_key))."""
    base = hashlib.md5(f"{app_id}{ts}".encode("utf-8")).hexdigest()
    digest = hmac.new(secret_key.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def auth_params(app_id: str, secret_key: str) -> dict[str, str]:
    ts = str(int(time.time()))
    return {"app_id": app_id, "ts": ts, "signa": make_signa(app_id, secret_key, ts)}


def ensure_ok(resp: requests.Response, action: str) -> dict:
    try:
        payload = resp.json()
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{action} returned non-JSON HTTP {resp.status_code}: {resp.text[:300]}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"{action} returned HTTP {resp.status_code}: {payload}")
    if payload.get("ok") != 0:
        failed = payload.get("failed") or "unknown error"
        err_no = payload.get("err_no")
        raise RuntimeError(f"{action} failed ({err_no}): {failed}")
    return payload


class SliceIdGenerator:
    """Generate iFlytek slice ids: aaaaaaaaaa, aaaaaaaaab, ..."""

    def __init__(self) -> None:
        self._value = "aaaaaaaaa`"

    def next(self) -> str:
        chars = list(self._value)
        j = len(chars) - 1
        while j >= 0:
            if chars[j] != "z":
                chars[j] = chr(ord(chars[j]) + 1)
                break
            chars[j] = "a"
            j -= 1
        self._value = "".join(chars)
        return self._value


def prepare_task(
    session: Session,
    audio_path: Path,
    app_id: str,
    secret_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
) -> tuple[str, int]:
    file_len = audio_path.stat().st_size
    slice_num = max(1, math.ceil(file_len / SLICE_SIZE))
    data: dict[str, str | int] = {
        **auth_params(app_id, secret_key),
        "file_len": str(file_len),
        "file_name": audio_path.name,
        "slice_num": slice_num,
        "lfasr_type": "0",
        "has_participle": "true",
        "has_seperate": "true",
        "has_smooth": "false",
        "eng_vad_margin": "0",
    }
    if language:
        data["language"] = language
    if num_speakers:
        data["speaker_number"] = str(num_speakers)
    else:
        data["speaker_number"] = "0"

    resp = request_with_fallback(
        session,
        "POST",
        f"{LFASR_BASE_URL}/prepare",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=60,
    )
    payload = ensure_ok(resp, "prepare")
    return str(payload["data"]), slice_num


def upload_slices(
    session: Session,
    audio_path: Path,
    task_id: str,
    slice_num: int,
    app_id: str,
    secret_key: str,
) -> None:
    gen = SliceIdGenerator()
    with open(audio_path, "rb") as f:
        for _ in range(slice_num):
            chunk = f.read(SLICE_SIZE)
            if not chunk:
                break
            data = {**auth_params(app_id, secret_key), "task_id": task_id, "slice_id": gen.next()}
            resp = request_with_fallback(
                session,
                "POST",
                f"{LFASR_BASE_URL}/upload",
                data=data,
                files={"content": ("slice", chunk, "application/octet-stream")},
                timeout=180,
            )
            ensure_ok(resp, "upload")


def merge_task(session: Session, task_id: str, app_id: str, secret_key: str) -> None:
    data = {**auth_params(app_id, secret_key), "task_id": task_id}
    resp = request_with_fallback(
        session,
        "POST",
        f"{LFASR_BASE_URL}/merge",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=60,
    )
    ensure_ok(resp, "merge")


def get_progress(session: Session, task_id: str, app_id: str, secret_key: str) -> dict:
    data = {**auth_params(app_id, secret_key), "task_id": task_id}
    resp = request_with_fallback(
        session,
        "POST",
        f"{LFASR_BASE_URL}/getProgress",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=60,
    )
    payload = ensure_ok(resp, "getProgress")
    raw = payload.get("data") or "{}"
    return json.loads(raw)


def get_result(session: Session, task_id: str, app_id: str, secret_key: str) -> list[dict]:
    data = {**auth_params(app_id, secret_key), "task_id": task_id}
    resp = request_with_fallback(
        session,
        "POST",
        f"{LFASR_BASE_URL}/getResult",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=120,
    )
    payload = ensure_ok(resp, "getResult")
    raw = payload.get("data") or "[]"
    result = json.loads(raw)
    if not isinstance(result, list):
        raise RuntimeError(f"unexpected getResult data shape: {type(result).__name__}")
    return result


def call_xfyun_lfasr(
    audio_path: Path,
    app_id: str,
    secret_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
    poll_interval: float = 30.0,
    timeout_seconds: float = 6 * 60 * 60,
    verbose: bool = True,
) -> dict:
    session = build_session()
    task_id, slice_num = prepare_task(session, audio_path, app_id, secret_key, language, num_speakers)
    if verbose:
        print(f"  xfyun task: {task_id} ({slice_num} slice{'s' if slice_num != 1 else ''})", flush=True)
    upload_slices(session, audio_path, task_id, slice_num, app_id, secret_key)
    merge_task(session, task_id, app_id, secret_key)

    deadline = time.time() + timeout_seconds
    last_status: int | None = None
    while time.time() < deadline:
        progress = get_progress(session, task_id, app_id, secret_key)
        status = int(progress.get("status", -1))
        if verbose and status != last_status:
            desc = progress.get("desc") or "processing"
            print(f"  status {status}: {desc}", flush=True)
            last_status = status
        if status == 9:
            raw_segments = get_result(session, task_id, app_id, secret_key)
            return normalize_xfyun_result(raw_segments, task_id)
        if status < 0:
            raise RuntimeError(f"xfyun task returned invalid progress: {progress}")
        time.sleep(poll_interval)

    raise TimeoutError(f"xfyun transcription timed out after {timeout_seconds:.0f}s (task {task_id})")


def _speaker_id(raw: object) -> str | None:
    if raw is None:
        return None
    speaker = str(raw)
    if speaker in {"", "0"}:
        return "speaker_0"
    try:
        return f"speaker_{max(0, int(speaker) - 1)}"
    except ValueError:
        return f"speaker_{speaker}"


def _word_text(item: dict) -> str:
    for key in ("wordsName", "word", "text", "onebest"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def normalize_xfyun_result(raw_segments: list[dict], task_id: str) -> dict:
    """Convert iFlytek LFASR output to video-use's normalized word schema."""
    words: list[dict] = []
    full_text_parts: list[str] = []
    previous_end: float | None = None

    for seg in raw_segments:
        seg_start = float(seg.get("bg", 0)) / 1000.0
        seg_end = float(seg.get("ed", seg.get("bg", 0))) / 1000.0
        speaker = _speaker_id(seg.get("speaker"))
        text = str(seg.get("onebest") or "")
        if text:
            full_text_parts.append(text)

        if previous_end is not None and seg_start > previous_end:
            words.append({"type": "spacing", "text": " ", "start": previous_end, "end": seg_start})

        word_items = seg.get("wordsResultList") or []
        if isinstance(word_items, list) and word_items:
            for item in word_items:
                if not isinstance(item, dict):
                    continue
                token = _word_text(item)
                if not token:
                    continue
                start_frame = float(item.get("wordBg", 0))
                end_frame = float(item.get("wordEd", start_frame))
                start = seg_start + start_frame * 0.01
                end = seg_start + end_frame * 0.01
                if end <= start:
                    end = min(seg_end, start + 0.05)
                wp = str(item.get("wp") or "")
                words.append({
                    "type": "word",
                    "text": token,
                    "start": start,
                    "end": end,
                    "speaker_id": speaker,
                    "xfyun_wp": wp,
                    "confidence": item.get("wc"),
                })
        elif text:
            words.append({
                "type": "word",
                "text": text,
                "start": seg_start,
                "end": seg_end,
                "speaker_id": speaker,
            })

        previous_end = max(previous_end or 0.0, seg_end)

    return {
        "provider": "xfyun_lfasr",
        "task_id": task_id,
        "text": "\n".join(full_text_parts),
        "words": words,
        "raw_segments": raw_segments,
    }


def transcribe_one(
    video: Path,
    edit_dir: Path,
    app_id: str,
    secret_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
    poll_interval: float = 30.0,
    timeout_seconds: float = 6 * 60 * 60,
    verbose: bool = True,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  uploading {video.stem}.wav ({size_mb:.1f} MB)", flush=True)
        payload = call_xfyun_lfasr(
            audio,
            app_id,
            secret_key,
            language=language,
            num_speakers=num_speakers,
            poll_interval=poll_interval,
            timeout_seconds=timeout_seconds,
            verbose=verbose,
        )

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        if isinstance(payload, dict) and "words" in payload:
            print(f"    words: {len(payload['words'])}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video with iFlytek Long Form ASR")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional iFlytek language code (default from service is 'cn'; use 'en' for English).",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional number of speakers when known. 0/omitted enables blind separation.",
    )
    ap.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="Seconds between progress checks (default: 30).",
    )
    ap.add_argument(
        "--timeout",
        type=float,
        default=6 * 60 * 60,
        help="Max seconds to wait for transcription (default: 21600 / 6h).",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    app_id, secret_key = load_credentials()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        app_id=app_id,
        secret_key=secret_key,
        language=args.language,
        num_speakers=args.num_speakers,
        poll_interval=args.poll_interval,
        timeout_seconds=args.timeout,
    )


if __name__ == "__main__":
    main()
