#!/usr/bin/env python3
"""Download a video via yt-dlp, or resolve a local file path.

Also fetches subtitles (manual first, then auto-generated) in VTT format so
transcribe.py can parse them without needing Whisper.
"""
from __future__ import annotations

import datetime
import functools
import json
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".wmv"}

# Stderr substrings that identify YouTube's 403 / SABR-style blocking. Case-
# insensitive; pinned as a constant so the retry and the classifier agree.
# Deliberately NOT the bare word "sabr": yt-dlp prints an advisory "SABR-only
# streaming experiment" *warning* on runs that still succeed with other
# formats, and matching it would fire the android retry — and report an HTTP
# 403 that never happened — on any unrelated failure sharing that stderr.
# Actual SABR blocking always surfaces as an HTTP 403.
_SIG_403 = ("http error 403", "403: forbidden")

# yt-dlp versions are CalVer (YYYY.MM.DD), so staleness is readable off the
# version string with no network call. 90 days: yt-dlp ships every 2-6 weeks
# and YouTube extractor breakage has consistently been fixed within days of a
# site change, so an install >3 months old predates at least one breaking
# change with near-certainty. 30 would nag most Homebrew users (brew lags
# weeks); 180 misses the window in which the SABR-era breakage landed.
YTDLP_STALE_DAYS = 90

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


def _forward_stderr_bytes(chunk: bytes) -> None:
    """Write a child's stderr chunk to ours, never letting the echo raise.

    Bytes-level via the buffer when available: ensure_utf8_console may have
    reconfigured the text stream, and yt-dlp's stderr can carry bytes that are
    not valid UTF-8.
    """
    try:
        buffer = getattr(sys.stderr, "buffer", None)
        if buffer is not None:
            buffer.write(chunk)
            buffer.flush()
        else:
            sys.stderr.write(chunk.decode("utf-8", errors="replace"))
            sys.stderr.flush()
    except Exception:
        pass


def _stream_ytdlp(cmd: list[str]) -> tuple[int, str]:
    """Run yt-dlp, streaming its output live while capturing stderr.

    The old call sites did ``subprocess.run(cmd, stdout=sys.stderr,
    stderr=sys.stderr)`` — both streams inherited our fd, so nothing was
    capturable and every failure collapsed into "no video file (exit N)" with
    no way to tell a login wall from a region lock from SABR.

    yt-dlp writes download *progress* to stdout and errors to stderr, so only
    the low-volume stream needs a pipe: stdout keeps going straight to our
    stderr fd (live progress, no parent-side pumping), while stderr is teed —
    forwarded chunk-by-chunk AND buffered for classification. One pipe, no
    thread, no deadlock. ``read1`` rather than ``readline`` so ``\\r``-terminated
    retry lines forward promptly.
    """
    try:
        sys.stderr.fileno()
        stdout_target = sys.stderr
    except Exception:
        # No real fd behind stderr (embedded/captured stream): lose the
        # progress display, keep working.
        stdout_target = subprocess.DEVNULL
    proc = subprocess.Popen(cmd, stdout=stdout_target, stderr=subprocess.PIPE)
    chunks: list[bytes] = []
    assert proc.stderr is not None
    while True:
        chunk = proc.stderr.read1(4096)
        if not chunk:
            break
        _forward_stderr_bytes(chunk)
        chunks.append(chunk)
    proc.wait()
    return proc.returncode, b"".join(chunks).decode("utf-8", errors="replace")


@functools.lru_cache(maxsize=None)
def _ytdlp_version() -> str:
    """The installed yt-dlp's version string, or "" when unavailable."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip()


def _ytdlp_age_days(version: str, today: datetime.date) -> int | None:
    """Days since the release the version string names; None if unparseable.

    Accepts stable ``2025.06.30`` and nightly ``2025.06.30.232815`` forms.
    Unparseable never warns — a guess would nag users of forks and dev builds.
    """
    match = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", version)
    if not match:
        return None
    try:
        released = datetime.date(*(int(g) for g in match.groups()))
    except ValueError:
        return None
    return (today - released).days


def _upgrade_hint() -> str:
    system = platform.system()
    if system == "Darwin":
        return "brew upgrade yt-dlp"
    if system == "Windows":
        return "winget upgrade yt-dlp.yt-dlp (or: pip install -U yt-dlp)"
    return "pipx upgrade yt-dlp (or: pip install -U yt-dlp)"


_STALE_WARNED = False


def _warn_if_stale_ytdlp() -> None:
    """One stderr line when the installed yt-dlp predates the current extractor
    generation — the single most common cause of YouTube 403/format failures."""
    global _STALE_WARNED
    if _STALE_WARNED:
        return
    version = _ytdlp_version()
    age = _ytdlp_age_days(version, datetime.date.today())
    if age is not None and age > YTDLP_STALE_DAYS:
        _STALE_WARNED = True
        print(
            f"[watch] warning: yt-dlp {version} is {age} days old. YouTube "
            "403/format failures are most often a stale yt-dlp. Upgrade: "
            f"{_upgrade_hint()}",
            file=sys.stderr,
        )


def _matches_403(stderr_text: str) -> bool:
    low = stderr_text.lower()
    return any(sig in low for sig in _SIG_403)


# No bare "cookies" entry: yt-dlp mentions cookies in advisory lines on
# non-login failures — including "Skipping client "android" since it does not
# support cookies", which this module's OWN retry provokes when the user passed
# --cookies-from-browser — so that substring would route 403s to login advice.
_LIVE_REFUSAL = (
    "This URL is a live broadcast. yt-dlp would record it until it ends, "
    "which for an ongoing stream means /watch never returns.\n"
    "Re-run once the stream has finished — the same URL then resolves to "
    "a normal recording."
)

_LOGIN_SIGS = (
    "sign in to confirm", "age-restricted", "age restricted", "private video",
    "members-only", "members only", "login required", "join this channel",
    "confirm your age",
)
_REGION_SIGS = (
    "not available in your country", "geo restrict",
    "not made this video available", "blocked it in your country",
)


def _classify_download_failure(
    stderr_text: str, returncode: int, out_dir: Path, retried_android: bool
) -> str:
    """Turn yt-dlp's stderr into a message that routes to the right remediation.

    The old message — "no video file (exit N)" — could not distinguish a login
    wall (fix: cookies, with the user's consent) from a region lock (cookies
    will not help) from SABR blocking (fix: upgrade yt-dlp / android client),
    so the model reading it could not follow SKILL.md's failure table.
    First match wins; the login check runs first because YouTube's age-gate
    messages often also mention 403.
    """
    low = stderr_text.lower()
    if any(sig in low for sig in _LOGIN_SIGS):
        return (
            "Download failed: this video appears to be login-walled, age-gated or "
            "members-only.\nRe-run with --cookies-from-browser <browser> (e.g. "
            "chrome, firefox) so yt-dlp can authenticate as you — ask the user "
            "before reading their browser's cookies."
        )
    if any(sig in low for sig in _REGION_SIGS):
        return (
            "Download failed: this video is region-locked. Cookies will not "
            "help — it needs a different source or region."
        )
    if "does not pass filter" in low and "is_live" in low:
        # Belt only: yt-dlp writes the filter-skip line to STDOUT, which is not
        # captured, so this branch rarely sees it. The primary live detection
        # after a failed download is download_url's info.json check.
        return _LIVE_REFUSAL
    if _matches_403(stderr_text):
        version = _ytdlp_version()
        age = _ytdlp_age_days(version, datetime.date.today())
        age_note = f" ({age} days old)" if age is not None else ""
        retry_note = (
            "A retry with the Android player client was already attempted and "
            "also failed. "
            if retried_android else ""
        )
        return (
            "Download failed with HTTP 403 (SABR-style blocking). "
            f"{retry_note}Installed yt-dlp: {version or 'unknown'}{age_note} — "
            "an outdated yt-dlp is the most common cause. Upgrade with "
            f"`{_upgrade_hint()}` and re-run."
        )
    tail = stderr_text.strip()[-400:]
    message = f"yt-dlp did not produce a video file in {out_dir} (exit {returncode})."
    if tail:
        message += f"\nLast yt-dlp output:\n{tail}"
    return message


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


def _pick_subtitle(out_dir: Path, sub_langs: str = SUB_LANGS) -> Path | None:
    """Choose the best downloaded caption track.

    The source-language track wins over anything requested: on a non-English
    video the English file is a machine translation of the ASR, so preferring
    it loses fidelity for nothing. On an English video the -orig track *is* the
    English one, so this ordering costs nothing there.

    After that, preference follows the literal (non-regex) tokens of the
    effective ``--sub-langs`` value, in the order they were requested — so a
    user who asked for ``en-CA`` gets the en-CA track *selected*, not merely
    fetched and then passed over for a hardcoded English list.
    """
    candidates = sorted(out_dir.glob("video*.vtt"))
    if not candidates:
        return None
    original = [c for c in candidates if "-orig." in c.name]
    if original:
        return original[0]
    for token in (t.strip() for t in sub_langs.split(",")):
        if not token or any(ch in token for ch in ".*[]()?+\\^$"):
            continue  # regex patterns cannot become filename markers
        marker = f".{token}."
        for candidate in candidates:
            if marker in candidate.name:
                return candidate
    return candidates[0]


# yt-dlp downloads merged formats leg by leg as video.fNNN.<ext> and keeps a
# finished leg for resume when the other leg fails. Such a file is NOT a
# successful download: counting it as one both skipped the 403 retry (the
# failure that stranded it) and returned a video-only file whose silence
# downstream reads as "this source has no audio".
_FORMAT_LEG_RE = re.compile(r"^video\.f\d+\.")


def _pick_video(out_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus"):
        for candidate in out_dir.glob(f"video*{ext}"):
            if not _FORMAT_LEG_RE.match(candidate.name):
                return candidate
    for candidate in out_dir.glob("video.*"):
        if candidate.suffix.lower() in VIDEO_EXTS and not _FORMAT_LEG_RE.match(candidate.name):
            return candidate
    return None


def fetch_captions(
    url: str,
    out_dir: Path,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    sub_langs: str | None = None,
) -> dict:
    """Fetch metadata and best available VTT captions without downloading video."""
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    effective_langs = sub_langs or SUB_LANGS
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", effective_langs,
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        # Bounds the pass when the URL is a bare playlist/channel: without it a
        # 300-entry playlist fires 300 metadata+caption fetches (all colliding
        # on video.%(ext)s) before any guard can read the info JSON. With it,
        # one entry is processed and its info carries playlist_count for the
        # caller's refusal. Ignored for ordinary single-video URLs.
        "--playlist-items", "1",
        "--ignore-errors",
        "-o", output_template,
    ]
    # Before the "--" terminator, so they are read as options; the URL stays the
    # only positional argument.
    cmd += cookie_args(cookies_from_browser, cookies_file)
    cmd += ["--", url]
    returncode, _stderr_text = _stream_ytdlp(cmd)
    subtitle = _pick_subtitle(out_dir, effective_langs)
    info = _read_info(out_dir / "video.info.json", url)
    return {
        "video_path": None,
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": False,
        # A nonzero exit WITH info present is a subtitle-only failure (e.g. a
        # 429 on one track) — the live/playlist guards can still evaluate. Only
        # a failed metadata fetch blinds them, and the caller warns then.
        "fetch_failed": returncode != 0 and not info,
        "fetch_returncode": returncode,
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
                # Playlist markers, free in the same JSON: `_type` when only
                # the playlist metafile got written, and the entry counts that
                # yt-dlp injects into every entry extracted via a playlist.
                # Without them a /playlist?list=… URL downloads an arbitrary
                # entry and the report describes it as though it were the URL.
                "_type": raw.get("_type"),
                "playlist_count": raw.get("playlist_count") or raw.get("n_entries"),
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
    sub_langs: str | None = None,
) -> dict:
    if shutil.which("yt-dlp") is None:
        raise SystemExit("yt-dlp is not installed. Install with: brew install yt-dlp")

    out_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(out_dir / "video.%(ext)s")
    effective_langs = sub_langs or SUB_LANGS

    fmt = "ba/bestaudio" if audio_only else "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"
    cmd = [
        "yt-dlp",
        "-N", "8",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--write-info-json",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", effective_langs,
        "--sub-format", "vtt",
        "--convert-subs", "vtt",
        "--no-playlist",
        "--playlist-items", "1",
        # Refuses a currently-live stream at download time. The pre-download
        # guard (watch.refuse_if_live) is blind exactly when the metadata fetch
        # failed — i.e. when yt-dlp is struggling — and a live URL then means
        # recording until the broadcast ends. `!is_live` passes when the field
        # is False or missing (non-YouTube extractors unaffected) and rejects
        # only an ongoing broadcast; post-live VODs pass, matching the guard's
        # deliberate post_live allowance. A rejected entry produces no file and
        # the classifier maps the filter message to the same live-broadcast
        # text the guard prints.
        "--match-filter", "!is_live",
        "--ignore-errors",
        "-o", output_template,
    ]
    cmd += cookie_args(cookies_from_browser, cookies_file)
    cmd += ["--", url]

    _warn_if_stale_ytdlp()

    # yt-dlp may exit non-zero if a subtitle variant fails (e.g. 429) even when
    # the video itself downloaded fine. Treat "video file present" as success —
    # which is also why the 403 retry below keys on the missing file, not on
    # the exit code.
    returncode, stderr_text = _stream_ytdlp(cmd)
    video = _pick_video(out_dir)
    retried_android = False
    if video is None and _matches_403(stderr_text):
        # Exactly one retry, media only: the android player client is the one
        # reported to survive SABR blocking, but it also kills captions — and
        # captions were already fetched by fetch_captions before this runs.
        print(
            "[watch] media download hit HTTP 403 — retrying once with the "
            "Android player client…",
            file=sys.stderr,
        )
        retry_cmd = cmd[:-2] + ["--extractor-args", "youtube:player_client=android"] + cmd[-2:]
        returncode, retry_stderr = _stream_ytdlp(retry_cmd)
        stderr_text = f"{stderr_text}\n{retry_stderr}"
        video = _pick_video(out_dir)
        retried_android = True
    if video is None:
        # Live detection reads the info JSON rather than stderr: the
        # --match-filter skip message goes to yt-dlp's STDOUT, which streams to
        # the user but is not captured, so the classifier cannot see it.
        failed_info = _read_info(out_dir / "video.info.json", url)
        if failed_info.get("is_live") or failed_info.get("live_status") == "is_live":
            raise SystemExit(_LIVE_REFUSAL)
        raise SystemExit(
            _classify_download_failure(stderr_text, returncode, out_dir, retried_android)
        )

    subtitle = _pick_subtitle(out_dir, effective_langs)
    info = _read_info(out_dir / "video.info.json", url)

    return {
        "video_path": str(video),
        "subtitle_path": str(subtitle) if subtitle else None,
        "info": info or {"url": url},
        "downloaded": True,
    }


def refuse_if_playlist(info: dict) -> None:
    """Stop when the URL names a playlist or channel rather than one video.

    Without this every entry downloads over the same ``video.%(ext)s`` template
    and the report describes an arbitrary entry as though it were the source.
    ``--playlist-items 1`` (both call sites) bounds the damage to one entry;
    this turns that entry into a refusal with the actual fix.

    Tolerates ``{}`` (a failed metadata fetch is a normal outcome) and lets a
    single-entry playlist through — its one video IS the request.
    """
    count = info.get("playlist_count")
    if info.get("_type") == "playlist" or (count or 0) > 1:
        noun = f"{count} videos" if count else "multiple videos"
        raise SystemExit(
            f"This URL is a playlist or channel ({noun}), not a single video. "
            "/watch reads one video at a time — every entry would be written "
            "over the same output file and the report would describe an "
            "arbitrary entry as though it were the whole source.\n"
            "Pass the URL of one specific video from it and re-run."
        )


def download(
    source: str,
    out_dir: Path,
    audio_only: bool = False,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    sub_langs: str | None = None,
) -> dict:
    if is_url(source):
        return download_url(
            source, out_dir, audio_only=audio_only,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            sub_langs=sub_langs,
        )
    return resolve_local(source)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: download.py <url-or-path> <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    result = download(sys.argv[1], Path(sys.argv[2]))
    print(json.dumps(result, indent=2))
