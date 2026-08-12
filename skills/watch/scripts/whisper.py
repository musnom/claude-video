#!/usr/bin/env python3
"""Transcribe a video via Groq or OpenAI Whisper API.

Strategy: extract audio (mono 16kHz mp3, tiny payload), upload to whichever
API has a key. Returns segments in the same shape as transcribe.parse_vtt so
the rest of the pipeline (filter_range, format_transcript) doesn't care where
the transcript came from.

Pure stdlib — no `pip install groq` or `pip install openai` needed.
"""
from __future__ import annotations

import io
import json
import math
import mimetypes
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from config import read_setting  # noqa: E402


GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"

OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MODEL = "whisper-1"

# Self-hosted / local transcription. _post_whisper already speaks exactly the
# OpenAI multipart + verbose_json protocol that whisper.cpp's whisper-server,
# speaches, LocalAI and vLLM all expose, so the only thing standing between this
# script and fully-local transcription is one hardcoded URL — no new dependency,
# no second segment parser, no engine-detection table.
CUSTOM_ENDPOINT_VAR = "WATCH_WHISPER_ENDPOINT"
CUSTOM_MODEL_VAR = "WATCH_WHISPER_MODEL"
CUSTOM_MODEL_DEFAULT = "whisper-1"


def _setting(name: str) -> str | None:
    """Read a setting via config.read_setting — one parser AND one path list
    for the whole codebase (env → ~/.config/watch/.env → ./.env). This module's
    resolution order was the compatibility anchor the shared helper adopted.
    """
    return read_setting(name)


def custom_endpoint() -> str | None:
    """The configured self-hosted transcription URL, if any."""
    return _setting(CUSTOM_ENDPOINT_VAR)

# Both Groq's free tier and OpenAI whisper-1 cap uploads at 25 MB. We target a
# margin under that so multipart framing overhead never pushes a chunk over.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

# Cloud-transcription duration guard. A captionless 4-hour video used to upload
# ~4 hours of audio with no estimate and no confirmation — billed per audio
# minute on both providers. 60 minutes as the line: it is where the upload is
# guaranteed multi-chunk, ~6x the SKILL's own "best accuracy under 10 minutes"
# guidance, and anything past it is nearly always better served by
# --start/--end. The refusal (not a warning — a warning printed while the
# upload proceeds guards nothing; --motion set the house precedent) names the
# exact re-run flag, so the caller relays the estimate to the user and re-runs
# with --transcribe-anyway on a yes. The self-hosted backend is exempt:
# localhost is free per-minute.
WHISPER_GUARD_SECONDS = 60 * 60.0

# Rough per-minute list prices for the refusal message. Order-of-magnitude
# honesty, not billing data — both providers change prices.
_APPROX_USD_PER_MINUTE = {"openai": 0.006, "groq": 0.002}


class LongAudioRefusal(SystemExit):
    """Refused before upload: audio exceeds WHISPER_GUARD_SECONDS.

    Subclasses SystemExit so existing `except SystemExit` call sites and the
    module CLI keep working; watch.py catches THIS type specifically so the
    report can state the real reason instead of "no API key". Any future
    `except Exception` around transcription would miss it — deliberately.
    """


class QuotaExhausted(SystemExit):
    """A 429 whose Retry-After exceeds the ceiling: the server said hours.

    Distinct from a plain per-request failure so transcribe_chunks can abort
    the whole run instead of skipping the chunk — POSTing the remaining ~24 MB
    chunks to an endpoint that just said it won't serve for hours defeats the
    ceiling's entire purpose.
    """


def _long_audio_refusal(minutes: float, backend: str) -> LongAudioRefusal:
    per_minute = _APPROX_USD_PER_MINUTE.get(backend, 0.006)
    est_chunks = max(1, math.ceil(minutes * 60 * 8000 / MAX_UPLOAD_BYTES))
    return LongAudioRefusal(
        f"Whisper upload guard: ~{minutes:.0f} minutes of audio would be "
        f"uploaded to {backend} (~{est_chunks} chunk(s), roughly "
        f"${minutes * per_minute:.2f} at {backend} list rates). "
        "Re-run with --transcribe-anyway to proceed, --start/--end to "
        "transcribe a section, or --no-whisper to skip transcription."
    )


def plan_chunks(
    total_seconds: float,
    total_bytes: int,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> list[tuple[float, float]]:
    """Split a duration into contiguous (offset, duration) chunks under max_bytes.

    Size scales linearly with duration (constant-bitrate mono mp3), so an even
    time split yields evenly-sized chunks. Returns a single full-length chunk
    when the audio already fits.
    """
    if total_bytes <= max_bytes or total_seconds <= 0:
        return [(0.0, total_seconds)]

    n = math.ceil(total_bytes / max_bytes)
    chunk = total_seconds / n
    plan: list[tuple[float, float]] = []
    for i in range(n):
        offset = i * chunk
        # The last chunk absorbs any rounding remainder so durations sum exactly.
        duration = (total_seconds - offset) if i == n - 1 else chunk
        plan.append((round(offset, 3), round(duration, 3)))
    return plan


def load_api_key(preferred: str | None = None) -> tuple[str, str] | tuple[None, None]:
    """Return (backend, api_key). Prefers Groq, falls back to OpenAI.

    If `preferred` is "groq", "openai" or "custom", only that backend is
    considered. A configured self-hosted endpoint wins over the cloud backends
    when nothing was forced — someone who pointed this at localhost meant it —
    and returns an empty key, since local servers do not require one.
    """
    if preferred == "custom" or (preferred is None and custom_endpoint()):
        if custom_endpoint():
            return "custom", ""
        return None, None

    candidates = (("GROQ_API_KEY", "groq"), ("OPENAI_API_KEY", "openai"))
    if preferred is not None:
        candidates = tuple(c for c in candidates if c[1] == preferred)

    for key_name, backend in candidates:
        # config.read_setting shares the parser (quote + inline-comment
        # handling — the private copy this replaced once sent
        # `GROQ_API_KEY=abc  # prod key` verbatim as the bearer token) AND the
        # path list, so setup.py and the hook agree with this module about
        # which key exists.
        value = read_setting(key_name)
        if value:
            return backend, value

    return None, None


def extract_audio(
    video_path: str,
    out_path: Path,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> Path:
    """Extract mono 16kHz 64kbps mp3 — ~480 kB/min, fits any Whisper limit.

    ``start_seconds`` / ``end_seconds`` clip to the focus window before encoding.
    A focused run used to upload the whole video's audio regardless: measured on
    a 10-minute clip with ``--start 5:00 --end 5:20``, 4689 kB went over the wire
    where the window needed ~156 kB. Everything outside the window was
    transcribed, paid for, and then discarded by ``filter_range``.

    Returned segments are 0-based against the *clip*, so a caller passing
    ``start_seconds`` must shift them back into source time —
    :func:`transcribe_video` does.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]
    # Before -i: fast seek, which for an audio-only extraction costs nothing in
    # accuracy that a Whisper timestamp could resolve.
    if start_seconds:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    cmd += ["-i", str(Path(video_path).resolve())]
    if end_seconds is not None:
        # -t (a duration), never -to. With -ss placed before -i the input's clock
        # is rebased to the seek point, so -to 320 on a --start 300 run would cut
        # at 620s of source rather than at 320s — silently transcribing five
        # extra minutes of the wrong material.
        window = max(0.0, end_seconds - (start_seconds or 0.0))
        cmd += ["-t", f"{window:.3f}"]
    cmd += [
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "64k",
        str(out_path.resolve()),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg audio extraction failed: {result.stderr.strip()}")
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise SystemExit("ffmpeg produced no audio — video may have no audio track")
    # A non-zero size is not proof of audio once a range is in play. A window that
    # starts past the end of the stream produces a 333-byte mp3 containing headers
    # and no MPEG frames: the size guard passes, ffprobe cannot find a duration,
    # and that empty payload gets uploaded to the Whisper API and billed.
    if start_seconds or end_seconds is not None:
        try:
            clipped_seconds = audio_duration(out_path)
        except SystemExit:
            # ffprobe refuses the file outright ("Failed to find two consecutive
            # MPEG audio frames") — same verdict, better message.
            clipped_seconds = 0.0
        if clipped_seconds <= 0:
            raise SystemExit(
                f"no audio in the requested range "
                f"({start_seconds or 0:.3f}s-"
                f"{end_seconds if end_seconds is not None else 'end'}) — "
                "check --start/--end against the video's duration"
            )
    return out_path


def audio_duration(audio_path: Path) -> float:
    """Return the duration of an audio file in seconds via ffprobe."""
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(audio_path.resolve()),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")
    fmt = json.loads(result.stdout or "{}").get("format", {})
    return float(fmt.get("duration") or 0.0)


def split_audio(
    full_audio: Path,
    work_dir: Path,
    plan: list[tuple[float, float]],
) -> list[tuple[Path, float]]:
    """Slice full_audio into per-plan chunk files, returning (path, offset) pairs.

    Uses stream copy (`-c copy`) so there is no re-encode and no quality loss;
    mp3 frame boundaries are close enough for transcription's purposes.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    work_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[Path, float]] = []
    for index, (offset, duration) in enumerate(plan):
        out_path = work_dir / f"chunk_{index:03d}.mp3"
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{offset:.3f}",
            "-i", str(full_audio.resolve()),
            "-t", f"{duration:.3f}",
            "-c", "copy",
            str(out_path.resolve()),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
            raise SystemExit(
                f"ffmpeg failed to split audio chunk {index + 1}: {result.stderr.strip()}"
            )
        chunks.append((out_path, offset))
    return chunks


def _build_multipart(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    """Assemble a multipart/form-data body the Whisper APIs accept.

    Whisper's multipart upload is small and predictable — doing it by hand
    keeps us on pure stdlib instead of pulling requests/groq/openai SDKs.
    """
    boundary = f"----WatchBoundary{uuid.uuid4().hex}"
    eol = b"\r\n"
    buf = io.BytesIO()

    for name, value in fields.items():
        buf.write(f"--{boundary}".encode()); buf.write(eol)
        buf.write(f'Content-Disposition: form-data; name="{name}"'.encode()); buf.write(eol)
        buf.write(eol)
        buf.write(str(value).encode()); buf.write(eol)

    mimetype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    buf.write(f"--{boundary}".encode()); buf.write(eol)
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"'.encode()
    )
    buf.write(eol)
    buf.write(f"Content-Type: {mimetype}".encode()); buf.write(eol)
    buf.write(eol)
    buf.write(file_path.read_bytes())
    buf.write(eol)
    buf.write(f"--{boundary}--".encode()); buf.write(eol)

    return buf.getvalue(), boundary


MAX_ATTEMPTS = 4       # initial + 3 retries
MAX_429_RETRIES = 2
RETRY_BASE_DELAY = 2.0
# Longest server-requested wait this client will honor. Per-minute rate limits
# carry Retry-After values of seconds to tens of seconds — worth sleeping
# through. Daily-quota 429s carry HOURS, and honoring one meant /watch printed
# a single line and then blocked the whole session (Groq's quota-exhaustion
# values were measured in the multi-hour range). 60s also stays inside typical
# harness command timeouts, so the failure message is seen instead of the
# process being killed mid-sleep. Past the ceiling the caller is told the
# server's number and decides — switch backends, tell the user, re-run later —
# which a sleeping process cannot do.
RETRY_AFTER_CEILING = 60.0


def _post_whisper(endpoint: str, api_key: str, model: str, audio_path: Path) -> dict:
    fields = {
        "model": model,
        "response_format": "verbose_json",
        "temperature": "0",
    }
    body, boundary = _build_multipart(fields, audio_path)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        # Groq sits behind Cloudflare — the default `Python-urllib/3.x` UA
        # trips WAF rule 1010 (403) before auth even runs. Any non-default
        # UA clears it; we identify honestly.
        "User-Agent": "watch-skill/1.0 (+claude-code; python-urllib)",
    }
    # Omit the header entirely rather than sending a placeholder: local servers
    # do not want one, and a fake bearer token is the kind of thing that gets
    # logged or rejected.
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    context = ssl.create_default_context()
    rate_limit_hits = 0
    last_exc: Exception | None = None
    last_detail = ""

    for attempt in range(MAX_ATTEMPTS):
        request = Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=300, context=context) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            last_exc, last_detail = exc, detail

            # 4xx other than 429 are client errors — no retry will fix them.
            if 400 <= exc.code < 500 and exc.code != 429:
                raise SystemExit(f"Whisper request failed: {exc}{detail}")

            if exc.code == 429:
                rate_limit_hits += 1
                if rate_limit_hits >= MAX_429_RETRIES:
                    raise SystemExit(f"Whisper request failed: {exc}{detail}")
                retry_after = _retry_after(exc)
                if retry_after is not None and retry_after > RETRY_AFTER_CEILING:
                    raise QuotaExhausted(
                        f"Whisper request failed: {exc}{detail} — the server asked to "
                        f"retry after {retry_after:.0f}s (~{retry_after / 60:.0f} min), "
                        f"over the {RETRY_AFTER_CEILING:.0f}s ceiling. That is quota "
                        "exhaustion, not a transient rate limit; sleeping through it "
                        "would hang this run. Try the other backend (--whisper openai / "
                        "--whisper groq), or re-run after the quota resets."
                    )
                delay = retry_after or RETRY_BASE_DELAY * (2 ** attempt) + 1
            else:
                delay = RETRY_BASE_DELAY * (2 ** attempt)

            if attempt < MAX_ATTEMPTS - 1:
                print(
                    f"[watch] whisper HTTP {exc.code} — retrying in {delay:.1f}s "
                    f"(attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionResetError, OSError) as exc:
            last_exc, last_detail = exc, ""
            if attempt < MAX_ATTEMPTS - 1:
                delay = RETRY_BASE_DELAY * (attempt + 1)
                print(
                    f"[watch] whisper network error ({type(exc).__name__}: {exc}) — "
                    f"retrying in {delay:.1f}s (attempt {attempt + 2}/{MAX_ATTEMPTS})",
                    file=sys.stderr,
                )
                time.sleep(delay)
            continue

        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Whisper returned non-JSON response: {exc}: {payload[:200]}")

    raise SystemExit(
        f"Whisper request failed after {MAX_ATTEMPTS} attempts: {last_exc}{last_detail}"
    )


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read()
    except Exception:
        return ""
    if not body:
        return ""
    try:
        return f" — {body.decode('utf-8', errors='replace')[:400]}"
    except Exception:
        return ""


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    header = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def shift_segments(segments: list[dict], offset_seconds: float) -> list[dict]:
    """Return a copy of segments with start/end shifted by offset_seconds.

    Each chunk is transcribed in isolation, so Whisper returns 0-based timestamps
    per chunk; shifting by the chunk's offset stitches them into source time.
    """
    if offset_seconds == 0:
        return segments
    return [
        {
            "start": round(seg["start"] + offset_seconds, 2),
            "end": round(seg["end"] + offset_seconds, 2),
            "text": seg["text"],
        }
        for seg in segments
    ]


def _segments_from_response(data: dict) -> list[dict]:
    """Convert Whisper verbose_json into our {start, end, text} segment format."""
    out: list[dict] = []
    for seg in data.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "start": round(float(seg.get("start") or 0.0), 2),
            "end": round(float(seg.get("end") or 0.0), 2),
            "text": text,
        })

    if not out:
        full = (data.get("text") or "").strip()
        if full:
            out.append({"start": 0.0, "end": 0.0, "text": full})

    return out


def transcribe_chunks(
    chunks: list[tuple[Path, float]],
    transcribe_one,
) -> list[dict]:
    """Transcribe each chunk, shift its segments by the chunk offset, concatenate.

    A chunk that fails after its own retries is logged and skipped so one bad
    slice doesn't discard the whole transcript. Raises only if every chunk fails.
    """
    segments: list[dict] = []
    failures = 0
    for index, (path, offset) in enumerate(chunks):
        try:
            chunk_segments = transcribe_one(path)
        except QuotaExhausted:
            # Not a per-chunk hiccup: the server named a multi-minute wait, so
            # every remaining chunk would meet the same refusal. Abort the run
            # with the server's message instead of paying to find out N times.
            raise
        except SystemExit as exc:
            failures += 1
            print(
                f"[watch] chunk {index + 1}/{len(chunks)} failed — skipping ({exc})",
                file=sys.stderr,
            )
            continue
        segments.extend(shift_segments(chunk_segments, offset))
        print(
            f"[watch] chunk {index + 1}/{len(chunks)} → {len(chunk_segments)} segments",
            file=sys.stderr,
        )

    if failures == len(chunks):
        raise SystemExit("Whisper failed on every audio chunk")
    return segments


def _transcribe_file(backend: str, api_key: str, audio_path: Path) -> list[dict]:
    """Upload one audio file and return its 0-based segments."""
    if backend == "groq":
        response = _post_whisper(GROQ_ENDPOINT, api_key, GROQ_MODEL, audio_path)
    elif backend == "openai":
        response = _post_whisper(OPENAI_ENDPOINT, api_key, OPENAI_MODEL, audio_path)
    elif backend == "custom":
        endpoint = custom_endpoint()
        if not endpoint:
            raise SystemExit(
                f"--whisper custom needs {CUSTOM_ENDPOINT_VAR} set to an "
                "OpenAI-compatible /v1/audio/transcriptions URL"
            )
        model = _setting(CUSTOM_MODEL_VAR) or CUSTOM_MODEL_DEFAULT
        response = _post_whisper(endpoint, api_key, model, audio_path)
    else:
        raise SystemExit(f"Unknown whisper backend: {backend}")
    return _segments_from_response(response)


def transcribe_video(
    video_path: str,
    audio_out: Path,
    backend: str | None = None,
    api_key: str | None = None,
    *,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    allow_long: bool = False,
) -> tuple[list[dict], str]:
    """Run the full flow: extract audio → upload → parse segments.

    Returns (segments, backend_used). Raises SystemExit on any failure.

    With a focus window, only that window is encoded and uploaded, and the
    returned segments are shifted back into absolute source time. The shift is
    not cosmetic: ``watch.filter_range`` selects on absolute time, so unshifted
    segments from a ``--start 5:00`` run would all sit near t=0 and the filter
    would discard the entire transcript — a silent, total loss.

    Keyword-only, so the existing positional calls keep working.
    """
    if backend is None or api_key is None:
        detected_backend, detected_key = load_api_key()
        backend = backend or detected_backend
        api_key = api_key or detected_key

    # The custom backend is deliberately exempt from the key requirement: a
    # local server does not issue one.
    if not backend or (not api_key and backend != "custom"):
        setup_py = Path(__file__).resolve().parent / "setup.py"
        raise SystemExit(
            "No Whisper API key available. Set GROQ_API_KEY (preferred) or OPENAI_API_KEY "
            f"in the environment or in ~/.config/watch/.env, or point {CUSTOM_ENDPOINT_VAR} "
            "at a self-hosted OpenAI-compatible server. "
            f"Run `python3 {setup_py}` to configure."
        )

    # Guard BEFORE the encode, on one cheap ffprobe of the source: the window
    # when one was given, else the full duration. The estimate can only shrink
    # after encoding, so refusing here never refuses a run the post-encode
    # number would have allowed by more than rounding.
    try:
        source_seconds = audio_duration(Path(video_path))
    except SystemExit:
        source_seconds = 0.0
    if end_seconds is not None:
        estimated_seconds = max(0.0, end_seconds - (start_seconds or 0.0))
    elif start_seconds:
        estimated_seconds = max(0.0, source_seconds - start_seconds)
    else:
        estimated_seconds = source_seconds
    if estimated_seconds > WHISPER_GUARD_SECONDS:
        minutes = estimated_seconds / 60.0
        if backend == "custom":
            print(
                f"[watch] transcribing ~{minutes:.0f} minutes of audio on the "
                "self-hosted endpoint (long, but local and free per-minute)…",
                file=sys.stderr,
            )
        elif not allow_long:
            raise _long_audio_refusal(minutes, backend)

    scope = (
        f" over {start_seconds or 0:.1f}-{end_seconds:.1f}s" if end_seconds is not None
        else f" from {start_seconds:.1f}s" if start_seconds else ""
    )
    print(f"[watch] extracting audio for Whisper ({backend}){scope}…", file=sys.stderr)
    audio_path = extract_audio(
        video_path, audio_out, start_seconds=start_seconds, end_seconds=end_seconds
    )
    audio_bytes = audio_path.stat().st_size

    # The authoritative guard, AFTER the encode: the pre-encode estimate reads
    # the container's duration header, which headerless sources (piped output,
    # OBS/browser recordings — long captionless recordings being the guard's
    # exact target) simply do not have, silently bypassing it. The encoded mp3
    # always has a real duration, and the expensive parts — upload and billing —
    # have not happened yet.
    if backend != "custom" and not allow_long:
        try:
            encoded_seconds = audio_duration(audio_path)
        except SystemExit:
            # A failed probe must not kill a run the guard exists to protect;
            # the pre-encode estimate above already had its chance.
            encoded_seconds = 0.0
        if encoded_seconds > WHISPER_GUARD_SECONDS:
            raise _long_audio_refusal(encoded_seconds / 60.0, backend)

    def transcribe_one(path: Path) -> list[dict]:
        return _transcribe_file(backend, api_key, path)

    if audio_bytes <= MAX_UPLOAD_BYTES:
        print(
            f"[watch] audio: {audio_bytes / 1024:.0f} kB — uploading to {backend} Whisper…",
            file=sys.stderr,
        )
        segments = transcribe_one(audio_path)
    else:
        duration = audio_duration(audio_path)
        plan = plan_chunks(duration, audio_bytes, MAX_UPLOAD_BYTES)
        print(
            f"[watch] audio: {audio_bytes / (1024 * 1024):.0f} MB exceeds "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB — splitting into {len(plan)} chunks…",
            file=sys.stderr,
        )
        chunks = split_audio(audio_path, audio_out.parent / "chunks", plan)
        segments = transcribe_chunks(chunks, transcribe_one)

    # One choke point covering both paths above. transcribe_chunks has already
    # shifted each chunk by its offset *within the clip*; this shifts the clip
    # itself into source time, and the two compose. Applying it inside either
    # branch instead would either miss the single-upload path or double-count on
    # the chunked one.
    if start_seconds:
        segments = shift_segments(segments, start_seconds)

    if not segments:
        raise SystemExit("Whisper returned no transcript segments")

    print(f"[watch] transcribed {len(segments)} segments via {backend}", file=sys.stderr)
    return segments, backend


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "usage: whisper.py <video-path> [<audio-out.mp3>] [--backend groq|openai] "
            "[--transcribe-anyway]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    audio_out = Path(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith("--") else Path("audio.mp3")
    backend_override = None
    if "--backend" in sys.argv:
        backend_override = sys.argv[sys.argv.index("--backend") + 1]

    segments, backend = transcribe_video(
        video, audio_out, backend=backend_override,
        allow_long="--transcribe-anyway" in sys.argv,
    )
    print(json.dumps({"backend": backend, "segments": segments}, indent=2))
