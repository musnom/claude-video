#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# Caption tracks to request, as a single yt-dlp --sub-langs value shared by both
# call sites (they must stay in step; _pick_subtitle below can only choose from
# what these fetch).
#
# yt-dlp fullmatches each comma-separated entry as a regex against every
# available track, so the old `en.*` was not "English" — on a real video it
# matched 33 tracks, fired 33 requests, collected 22 rate-limit rejections and
# took 13.7s instead of 2.6s, to select a file it would have picked anyway.
#
# `.*-orig` is the source-language track. Without it a non-English video
# resolves to YouTube's machine *translation* of its own ASR while the original
# sits unfetched — so the model reads a translation of a transcription when the
# original was free. Exactly one -orig track exists per video, so this stays
# bounded. The explicit English codes are the fallback for videos that have no
# -orig track at all.
SUB_LANGS = ".*-orig,en,en-US,en-GB"


def cookie_args(
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> list[str]:
    """yt-dlp argv for an opt-in cookie source, or ``[]``.

    Opt-in only, deliberately. Probing browsers automatically when a download
    fails would read a cookie jar because a site returned 403 — on macOS each
    probe can raise a Keychain prompt, and "the tool read my browser profile" is
    not a thing to do on inference.

    ``cookies_from_browser`` is forwarded verbatim, including yt-dlp's
    ``BROWSER[+KEYRING][:PROFILE][::CONTAINER]`` syntax; yt-dlp validates the
    browser name and reports a better error than a re-implementation of its table
    would.

    The leading-dash check is defence in depth, not a patched hole. Checked
    against the real binary: ``yt-dlp --cookies-from-browser --config-location
    URL`` does **not** smuggle an option in — optparse takes the next token as the
    option's value and yt-dlp then rejects it with "unsupported browser
    specified". So this guard is not what stands between a caller and an
    arbitrary yt-dlp option today; it fails faster, with a message that names the
    flag the caller actually typed, and it keeps that true if yt-dlp's argument
    handling ever changes. An earlier version of this docstring claimed the
    stronger thing, which was wrong.

    Values are appended as single argv elements and never interpolated into a
    shell string — subprocess is called with a list throughout this module.
    """
    args: list[str] = []
    for flag, value in (("--cookies-from-browser", cookies_from_browser), ("--cookies", cookies_file)):
        if not value:
            continue
        text = str(value).strip()
        if text.startswith("-"):
            raise SystemExit(
                f"{flag} value may not start with '-' (got {text!r}) — yt-dlp would read "
                "it as another option rather than as a value."
            )
        if flag == "--cookies":
            path = Path(text).expanduser()
            if not path.is_file():
                raise SystemExit(f"--cookies file not found: {path}")
            text = str(path.resolve())
        args += [flag, text]
    return args


def is_url(source: str) -> bool:
    if source.startswith("-"):
        return False
    parsed = urlparse(source)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def resolve_local(path: str) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise SystemExit(f"File not found: {p}")
    if p.suffix.lower() not in VIDEO_EXTS:
        print(
            f"[watch] warning: {p.suffix} is not a known video extension, proceeding anyway",
            file=sys.stderr,
        )
    return {
        "video_path": str(p),
        "subtitle_path": None,
        "info": {"title": p.name, "url": str(p)},
        "downloaded": False,
    }


def _pick_subtitle(out_dir: Path) -> Path | None:
    """Choose the best downloaded caption track.

    The source-language track wins over English: on a non-English video the
    English file is a machine translation of the ASR, so preferring it loses
    fidelity for nothing. On an English video the -orig track *is* the English
    one, so this ordering costs nothing there.
    """
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    original = [c for c in candidates if "-orig." in c.name]
    if original:
        return original[0]
    english = [
        c for c in candidates
        if any(marker in c.name for marker in (".en.", ".en-US.", ".en-GB."))
    ]
    return english[0] if english else candidates[0]


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS:
            return candidate
    return None


def fetch_captions(
    url: str,
    out_dir: Path,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", SUB_LANGS,
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]
    # Before the "--" terminator, so they are read as options; the URL stays the
    # only positional argument.
    cmd += cookie_args(cookies_from_browser, cookies_file)
    cmd += ["--", url]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
    }


def _read_info(info_path: Path, url: str) -> dict:
    """Narrow yt-dlp's info dict to the fields the rest of /watch reads.

    ``is_live`` / ``live_status`` are here so a live URL can be refused *before*
    the download rather than after it hangs: yt-dlp given a live stream records
    until the stream ends, which for a 24/7 channel is never. ``fetch_captions``
    already asks for the info JSON with ``--skip-download``, so both are free at
    that point — they were simply being dropped on the floor.

    ``width`` / ``height`` / ``vcodec`` come along because they answer "is there
    a video stream at all" for a source we have not downloaded yet, and cost
    nothing once the dict is open.
    """
    info: dict = {}
    if info_path.exists():
        try:
            raw = json.loads(info_path.read_text(encoding="utf-8"))
            info = {
                "title": raw.get("title"),
                "uploader": raw.get("uploader") or raw.get("channel"),
                "duration": raw.get("duration"),
                "url": raw.get("webpage_url") or url,
                "is_live": raw.get("is_live"),
                "live_status": raw.get("live_status"),
                "width": raw.get("width"),
                "height": raw.get("height"),
                "vcodec": raw.get("vcodec"),
            }
        except Exception as exc:
            print(f"[watch] info.json parse failed: {exc}", file=sys.stderr)
            info = {"url": url}
    return info


def download_url(
    url: str,
    out_dir: Path,
    audio_only: bool = False,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")

    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", SUB_LANGS,
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--ignore-errors",
        "-o", output_template,
    ]
    cmd += cookie_args(cookies_from_browser, cookies_file)
    cmd += ["--", url]

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success.
    result = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)
    video = _pick_video(out_dir)
    if video is None:
        raise SystemExit(
            f"yt-dlp did not produce a video file in {out_dir} (exit {result.returncode})"
        )

    subtitle = _pick_subtitle(out_dir)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> dict:
    if is_url(source):
        return download_url(
            source, out_dir, audio_only=audio_only,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
        )
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
