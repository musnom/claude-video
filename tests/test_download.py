"""yt-dlp argv construction and caption-track selection in download.py.

Regression guard: the --sub-langs request must stay BOUNDED. yt-dlp fullmatches
each comma-separated entry as a regex against every available track, so a broad
pattern is not a filter — `all` or `en.*` matches dozens of YouTube's
auto-translated tracks, fires a request per track, and collects rate-limit
rejections for minutes before the video download even starts. Measured on a real
video: `en.*` matched 33 tracks and took 13.7s where the bounded list takes 2.6s.

This is deliberately not an "English-only" guard. The request includes the
source-language track (`.*-orig`) so a non-English video yields its own captions
rather than YouTube's machine translation of them.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import download  # noqa: E402

URL = "https://www.youtube.com/watch?v=rlOpbu3Enkw"

# Anything outside this set risks the fan-out described above.
ALLOWED_SUB_LANGS = {".*-orig", "en", "en-US", "en-GB"}


def _capture_argv(
    monkeypatch: pytest.MonkeyPatch, rc: int = 0, stderr_text: str = ""
) -> list[list[str]]:
    """Stub the yt-dlp seam (_stream_ytdlp) and record every argv.

    The seam, not subprocess: download.py runs yt-dlp through _stream_ytdlp
    (Popen + stderr tee), so a subprocess.run stub would silently stop
    intercepting. The stale-version check is silenced too, so no test output
    depends on the machine's installed yt-dlp.
    """
    calls: list[list[str]] = []

    def fake_stream(cmd):
        calls.append(list(cmd))
        return rc, stderr_text

    monkeypatch.setattr(download, "_stream_ytdlp", fake_stream)
    monkeypatch.setattr(download, "_warn_if_stale_ytdlp", lambda: None)
    return calls


def _sub_langs(argv: list[str]) -> str:
    idx = argv.index("--sub-langs")
    return argv[idx + 1]


def _assert_bounded(langs: str) -> None:
    tokens = langs.split(",")
    assert "all" not in tokens, f"must not request all languages, got {langs!r}"
    unexpected = set(tokens) - ALLOWED_SUB_LANGS
    assert not unexpected, (
        f"unbounded sub-langs entries {sorted(unexpected)!r} in {langs!r}; "
        "a broad regex fans out across YouTube's auto-translated tracks"
    )


# --- argv ---------------------------------------------------------------------


def test_fetch_captions_requests_bounded_langs(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    download.fetch_captions(URL, tmp_path / "download")
    _assert_bounded(_sub_langs(calls[0]))


def test_download_url_requests_bounded_langs(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    # _pick_video returns None with no real file, which raises SystemExit after
    # the yt-dlp argv is already built — that's all we need to inspect.
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path / "download")
    _assert_bounded(_sub_langs(calls[0]))


def test_both_call_sites_request_the_same_langs(monkeypatch, tmp_path):
    """_pick_subtitle can only choose from what these fetch, so they must agree."""
    calls = _capture_argv(monkeypatch)
    download.fetch_captions(URL, tmp_path / "a")
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path / "b")
    assert _sub_langs(calls[0]) == _sub_langs(calls[1]) == download.SUB_LANGS


def test_original_language_track_is_requested():
    """Without this a non-English video gets a translation of its own ASR."""
    assert ".*-orig" in download.SUB_LANGS.split(",")


# --- _pick_subtitle precedence ------------------------------------------------


def _touch(dirpath: Path, *names: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    for name in names:
        (dirpath / name).write_text("WEBVTT\n", encoding="utf-8")
    return dirpath


def test_original_track_outranks_english(tmp_path):
    """A Spanish video: es-orig is the real audio, en is a machine translation."""
    d = _touch(tmp_path / "d", "video.en.vtt", "video.es-orig.vtt")
    assert download._pick_subtitle(d).name == "video.es-orig.vtt"


def test_english_used_when_no_original_track(tmp_path):
    d = _touch(tmp_path / "d", "video.en.vtt", "video.fr.vtt")
    assert download._pick_subtitle(d).name == "video.en.vtt"


def test_english_variants_are_accepted(tmp_path):
    d = _touch(tmp_path / "d", "video.de.vtt", "video.en-GB.vtt")
    assert download._pick_subtitle(d).name == "video.en-GB.vtt"


def test_falls_back_to_any_track(tmp_path):
    d = _touch(tmp_path / "d", "video.pt.vtt")
    assert download._pick_subtitle(d).name == "video.pt.vtt"


def test_no_subtitle_returns_none(tmp_path):
    d = _touch(tmp_path / "d")
    assert download._pick_subtitle(d) is None


def test_english_video_is_unaffected(tmp_path):
    """On an English video the -orig track *is* the English one."""
    d = _touch(tmp_path / "d", "video.en.vtt", "video.en-orig.vtt")
    assert download._pick_subtitle(d).name == "video.en-orig.vtt"


def test_read_info_passes_through_the_live_flags(tmp_path):
    """is_live was unreachable: _read_info narrowed yt-dlp's dict to four keys
    and dropped it, so nothing downstream could refuse a live URL."""
    import json as _json

    info_path = tmp_path / "video.info.json"
    info_path.write_text(_json.dumps({
        "title": "Live now", "channel": "Someone", "duration": None,
        "webpage_url": "https://example.com/live",
        "is_live": True, "live_status": "is_live",
        "width": 1920, "height": 1080, "vcodec": "avc1",
        "formats": [{"irrelevant": True}],
    }), encoding="utf-8")

    info = download._read_info(info_path, "https://example.com/live")
    assert info["is_live"] is True
    assert info["live_status"] == "is_live"
    assert (info["width"], info["height"]) == (1920, 1080)
    assert info["vcodec"] == "avc1"
    # ...and still narrows: the raw dict has hundreds of keys.
    assert set(info) == {
        "title", "uploader", "duration", "url",
        "is_live", "live_status", "width", "height", "vcodec",
        "_type", "playlist_count",
    }


def test_read_info_on_a_missing_file_is_empty(tmp_path):
    assert download._read_info(tmp_path / "nope.json", "https://example.com") == {}


# --- cookies -------------------------------------------------------------------
# Opt-in access to login-walled sources. The value reaches yt-dlp's argv, so the
# only thing standing between a caller and an arbitrary yt-dlp option is the
# leading-dash check.


def test_no_cookie_flags_means_no_cookie_argv():
    assert download.cookie_args() == []
    assert download.cookie_args(None, None) == []


def test_browser_spec_is_forwarded_verbatim():
    """yt-dlp validates the browser name and its BROWSER[+KEYRING][:PROFILE]
    syntax, and reports a better error than a copy of its table would."""
    assert download.cookie_args("firefox:default") == ["--cookies-from-browser", "firefox:default"]
    assert download.cookie_args("chrome+gnomekeyring::Work") == [
        "--cookies-from-browser", "chrome+gnomekeyring::Work",
    ]


@pytest.mark.parametrize("hostile", ["--config-location", "-o/tmp/x", "--exec=rm -rf /"])
def test_flag_injection_is_rejected_before_yt_dlp_runs(hostile):
    """Defence in depth, and the code says so.

    Measured against the real binary: yt-dlp's optparse takes the next token as
    the option's value, so `--cookies-from-browser --config-location URL` fails
    with "unsupported browser specified" rather than loading a config file. This
    guard fails earlier and names the flag the caller typed, and it keeps holding
    if that parsing ever changes — it is not plugging a live hole."""
    with pytest.raises(SystemExit, match="may not start with"):
        download.cookie_args(hostile)
    with pytest.raises(SystemExit, match="may not start with"):
        download.cookie_args(None, hostile)


def test_cookie_file_must_exist(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        download.cookie_args(None, str(tmp_path / "nope.txt"))


def test_cookie_file_is_resolved(tmp_path):
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert download.cookie_args(None, str(jar)) == ["--cookies", str(jar.resolve())]


def _capture_ytdlp(monkeypatch) -> list[list[str]]:
    calls = _capture_argv(monkeypatch)
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    return calls


def test_public_download_argv_is_unchanged_without_cookies(monkeypatch, tmp_path):
    """The no-cookie path must be byte-identical to before this existed."""
    calls = _capture_ytdlp(monkeypatch)
    download.fetch_captions("https://example.com/v", tmp_path)
    argv = calls[0]
    assert "--cookies-from-browser" not in argv and "--cookies" not in argv
    assert argv[-2:] == ["--", "https://example.com/v"]


def test_cookies_reach_the_caption_pass(monkeypatch, tmp_path):
    calls = _capture_ytdlp(monkeypatch)
    download.fetch_captions("https://example.com/v", tmp_path, cookies_from_browser="chrome")
    argv = calls[0]
    assert argv[argv.index("--cookies-from-browser") + 1] == "chrome"
    # Options must precede the "--" terminator or yt-dlp reads them as URLs.
    assert argv.index("--cookies-from-browser") < argv.index("--")
    assert argv[-1] == "https://example.com/v"


def test_cookies_reach_the_video_download(monkeypatch, tmp_path):
    calls = _capture_ytdlp(monkeypatch)
    (tmp_path / "video.mp4").write_bytes(b"x")
    download.download_url("https://example.com/v", tmp_path, cookies_from_browser="firefox")
    argv = calls[0]
    assert argv[argv.index("--cookies-from-browser") + 1] == "firefox"
    assert argv.index("--cookies-from-browser") < argv.index("--")


def test_local_files_ignore_cookie_flags(tmp_path):
    """A local path never reaches yt-dlp, so nothing should be read."""
    clip = tmp_path / "v.mp4"
    clip.write_bytes(b"x")
    result = download.download(str(clip), tmp_path, cookies_from_browser="chrome")
    assert result["video_path"] == str(clip.resolve())


# --- YouTube resilience: stale check, 403 retry, classification -----------------


import datetime
import os


@pytest.mark.parametrize(
    "version,expected",
    [
        ("2025.08.01", 375),
        ("2026.8.1", 10),                        # unpadded month/day
        ("2025.06.30.232815", 407),              # nightly suffix
        ("2025.06.30.dev0", 407),                # dev builds still lead with the date
        ("unknown", None),
        ("", None),
        ("10.2", None),
        ("2025.13.45", None),                    # not a real date -> never warn
    ],
)
def test_ytdlp_age_parses_calver(version, expected):
    today = datetime.date(2026, 8, 11)
    assert download._ytdlp_age_days(version, today) == expected


def test_stale_threshold_is_a_named_constant():
    assert download.YTDLP_STALE_DAYS == 90


def test_download_url_carries_the_live_filter_and_playlist_bound(monkeypatch, tmp_path):
    calls = _capture_ytdlp(monkeypatch)
    (tmp_path / "video.mp4").write_bytes(b"x")
    download.download_url(URL, tmp_path)
    argv = calls[0]
    assert argv[argv.index("--match-filter") + 1] == "!is_live"
    assert argv[argv.index("--playlist-items") + 1] == "1"
    assert argv[-2:] == ["--", URL]


def test_fetch_captions_bounds_playlists_but_keeps_no_filter(monkeypatch, tmp_path):
    """The caption pass is already bounded (--skip-download) and must not lose
    metadata for a live URL — the pre-download guard reads it."""
    calls = _capture_ytdlp(monkeypatch)
    download.fetch_captions(URL, tmp_path)
    argv = calls[0]
    assert argv[argv.index("--playlist-items") + 1] == "1"
    assert "--match-filter" not in argv


def test_403_triggers_exactly_one_android_retry(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_stream(cmd):
        calls.append(list(cmd))
        if len(calls) == 2:
            (tmp_path / "video.mp4").write_bytes(b"x")
        return 1, "ERROR: unable to download video data: HTTP Error 403: Forbidden"

    monkeypatch.setattr(download, "_stream_ytdlp", fake_stream)
    monkeypatch.setattr(download, "_warn_if_stale_ytdlp", lambda: None)
    result = download.download_url(URL, tmp_path)
    assert result["video_path"].endswith("video.mp4")
    assert len(calls) == 2
    retry = calls[1]
    idx = retry.index("--extractor-args")
    assert retry[idx + 1] == "youtube:player_client=android"
    assert idx < retry.index("--"), "extractor-args must precede the terminator"


def test_no_retry_when_the_file_landed_despite_nonzero_exit(monkeypatch, tmp_path):
    """A subtitle 429 exits non-zero with the media present — the docstring
    contract 'video file present is success' must survive the retry logic."""
    calls = _capture_argv(monkeypatch, rc=1, stderr_text="HTTP Error 403 on a subtitle")
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    (tmp_path / "video.mp4").write_bytes(b"x")
    result = download.download_url(URL, tmp_path)
    assert result["downloaded"] is True
    assert len(calls) == 1


def test_non_403_failure_does_not_retry(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch, rc=1, stderr_text="ERROR: This video is unavailable")
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit):
        download.download_url(URL, tmp_path)
    assert len(calls) == 1


def test_fetch_captions_never_retries(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch, rc=1, stderr_text="HTTP Error 403: Forbidden")
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = download.fetch_captions(URL, tmp_path)
    assert len(calls) == 1
    assert result["fetch_failed"] is True


def test_fetch_failed_is_false_when_info_survived(monkeypatch, tmp_path):
    """rc != 0 with info.json present is a subtitle-only failure; the live and
    playlist guards can still evaluate and must not be reported blind."""
    import json as _json

    calls = _capture_argv(monkeypatch, rc=1, stderr_text="429 on a subtitle track")
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    (tmp_path / "video.info.json").write_text(
        _json.dumps({"title": "T", "webpage_url": URL}), encoding="utf-8"
    )
    result = download.fetch_captions(URL, tmp_path)
    assert result["fetch_failed"] is False
    assert calls


@pytest.mark.parametrize(
    "stderr_text,fragment",
    [
        ("ERROR: Sign in to confirm you're not a bot", "--cookies-from-browser"),
        ("ERROR: This video is age-restricted", "--cookies-from-browser"),
        ("ERROR: Private video. Sign in if you", "--cookies-from-browser"),
        ("ERROR: The uploader has not made this video available in your country", "region-locked"),
        ("live thing does not pass filter (!is_live), skipping", "live broadcast"),
        ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "SABR-style"),
        ("something else entirely went wrong", "did not produce a video file"),
    ],
)
def test_failure_classification_routes_remediation(monkeypatch, stderr_text, fragment, tmp_path):
    monkeypatch.setattr(download, "_ytdlp_version", lambda: "2026.01.01")
    message = download._classify_download_failure(stderr_text, 1, tmp_path, retried_android=True)
    assert fragment in message, message


def test_403_classification_names_the_retry_and_the_version(monkeypatch, tmp_path):
    monkeypatch.setattr(download, "_ytdlp_version", lambda: "2025.01.15")
    message = download._classify_download_failure(
        "HTTP Error 403: Forbidden", 1, tmp_path, retried_android=True
    )
    assert "Android player client was already attempted" in message
    assert "2025.01.15" in message
    assert "days old" in message


# --- playlist refusal -----------------------------------------------------------


def test_playlist_url_is_refused():
    with pytest.raises(SystemExit, match="playlist or channel"):
        download.refuse_if_playlist({"playlist_count": 47})
    with pytest.raises(SystemExit, match="playlist or channel"):
        download.refuse_if_playlist({"_type": "playlist"})


def test_single_video_and_empty_info_pass():
    download.refuse_if_playlist({})
    download.refuse_if_playlist({"playlist_count": None})
    download.refuse_if_playlist({"playlist_count": 1})  # its one video IS the request
    download.refuse_if_playlist({"title": "ordinary video"})


def test_read_info_passes_through_playlist_markers(tmp_path):
    import json as _json

    info_path = tmp_path / "video.info.json"
    info_path.write_text(_json.dumps({
        "title": "Entry 1", "webpage_url": "https://example.com/watch?v=a",
        "playlist_count": 47,
    }), encoding="utf-8")
    info = download._read_info(info_path, "https://example.com/playlist?list=x")
    assert info["playlist_count"] == 47


# --- --sub-langs override -------------------------------------------------------


def test_sub_langs_override_reaches_both_call_sites(monkeypatch, tmp_path):
    calls = _capture_ytdlp(monkeypatch)
    download.fetch_captions(URL, tmp_path / "a", sub_langs="en-CA")
    b = tmp_path / "b"
    b.mkdir()
    (b / "video.mp4").write_bytes(b"x")
    download.download_url(URL, b, sub_langs="en-CA")
    assert _sub_langs(calls[0]) == _sub_langs(calls[1]) == "en-CA"


def test_default_sub_langs_are_unchanged(monkeypatch, tmp_path):
    """The measured-better-than-upstream default is the pin; overrides are the
    user's choice and deliberately unpinned."""
    calls = _capture_ytdlp(monkeypatch)
    download.fetch_captions(URL, tmp_path)
    assert _sub_langs(calls[0]) == download.SUB_LANGS == ".*-orig,en,en-US,en-GB"


def test_requested_variant_is_selected_not_just_fetched(tmp_path):
    """The gaps.md 'Rejected' edge, closed properly: en-CA both fetched AND
    picked over an unrelated track."""
    d = _touch(tmp_path / "d", "video.en-CA.vtt", "video.fr.vtt")
    assert download._pick_subtitle(d, ".*-orig,en-CA").name == "video.en-CA.vtt"
    # ...and the default still prefers plain English variants in request order.
    d2 = _touch(tmp_path / "d2", "video.en.vtt", "video.en-GB.vtt")
    assert download._pick_subtitle(d2).name == "video.en.vtt"


# --- the seam itself, through a real subprocess ----------------------------------


def _fake_ytdlp_on_path(monkeypatch, tmp_path: Path, body: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "yt-dlp"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")


def test_stream_ytdlp_forwards_and_captures_stderr(monkeypatch, tmp_path, capfd):
    """The tee is the crux of the resilience work: stderr must reach the user
    live AND come back for classification. capfd, not capsys — stdout is passed
    through at fd level."""
    _fake_ytdlp_on_path(
        monkeypatch, tmp_path,
        'echo "progress line" ; echo "MARKER-ON-STDERR" >&2 ; exit 3',
    )
    rc, captured = download._stream_ytdlp(["yt-dlp", "--fake"])
    assert rc == 3
    assert "MARKER-ON-STDERR" in captured
    assert "MARKER-ON-STDERR" in capfd.readouterr().err


def test_android_retry_end_to_end_through_a_real_subprocess(monkeypatch, tmp_path, capfd):
    """Fail-with-403 then succeed, through the real Popen/tee machinery —
    including surviving a partial .part file from the first attempt."""
    state = tmp_path / "state"
    out_dir = tmp_path / "dl"
    out_dir.mkdir()
    body = f'''
if [ ! -f "{state}" ]; then
  touch "{state}"
  touch "{out_dir}/video.mp4.part"
  echo "ERROR: unable to download video data: HTTP Error 403: Forbidden" >&2
  exit 1
fi
case "$*" in *player_client=android*) : ;; *) echo "retry missing android client: $*" >&2 ; exit 7 ;; esac
touch "{out_dir}/video.mp4"
exit 0
'''
    _fake_ytdlp_on_path(monkeypatch, tmp_path, body)
    monkeypatch.setattr(download, "_warn_if_stale_ytdlp", lambda: None)
    result = download.download_url("https://example.com/v", out_dir)
    assert result["video_path"].endswith("video.mp4")
    err = capfd.readouterr().err
    assert "retrying once with the Android player client" in err
    assert "HTTP Error 403" in err, "the tee did not forward the child's stderr"


def test_persistent_403_stops_after_exactly_one_retry(monkeypatch, tmp_path):
    """SABR blocking both attempts: the run must make exactly two yt-dlp calls
    (original + one android retry) and then fail classified — a regression that
    loops clients would hammer YouTube while the suite stayed green."""
    calls = _capture_argv(
        monkeypatch, rc=1, stderr_text="ERROR: HTTP Error 403: Forbidden"
    )
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(download, "_ytdlp_version", lambda: "2026.01.01")
    with pytest.raises(SystemExit, match="Android player client was already attempted"):
        download.download_url(URL, tmp_path)
    assert len(calls) == 2


def test_pick_video_skips_format_leg_files(tmp_path):
    """A stranded per-format leg (video.f398.mp4, kept by yt-dlp for resume
    when the other leg 403s) is NOT a successful download — counting it as one
    both skipped the retry and shipped a silent video-only file."""
    (tmp_path / "video.f398.mp4").write_bytes(b"x")
    assert download._pick_video(tmp_path) is None
    (tmp_path / "video.mp4").write_bytes(b"x")
    assert download._pick_video(tmp_path).name == "video.mp4"


def test_stranded_leg_still_triggers_the_android_retry(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_stream(cmd):
        calls.append(list(cmd))
        if len(calls) == 1:
            (tmp_path / "video.f398.mp4").write_bytes(b"x")   # stranded video leg
        else:
            (tmp_path / "video.mp4").write_bytes(b"x")        # merged retry result
        return 1, "ERROR: HTTP Error 403: Forbidden"

    monkeypatch.setattr(download, "_stream_ytdlp", fake_stream)
    monkeypatch.setattr(download, "_warn_if_stale_ytdlp", lambda: None)
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = download.download_url(URL, tmp_path)
    assert len(calls) == 2
    assert result["video_path"].endswith("video.mp4")


def test_failed_download_of_a_live_url_is_classified_from_info_json(monkeypatch, tmp_path):
    """yt-dlp's filter-skip line goes to STDOUT (uncaptured), so live detection
    after a failed download reads the info JSON instead of stderr."""
    import json as _json

    def fake_stream(cmd):
        (tmp_path / "video.info.json").write_text(
            _json.dumps({"title": "Live", "is_live": True, "live_status": "is_live"}),
            encoding="utf-8",
        )
        return 0, ""

    monkeypatch.setattr(download, "_stream_ytdlp", fake_stream)
    monkeypatch.setattr(download, "_warn_if_stale_ytdlp", lambda: None)
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit, match="live broadcast"):
        download.download_url(URL, tmp_path)


def test_advisory_sabr_warning_alone_does_not_classify_as_403(monkeypatch, tmp_path):
    """yt-dlp prints a 'SABR-only streaming experiment' WARNING on runs that
    can still succeed; the word alone must trigger neither the android retry
    nor a fabricated 'HTTP 403' verdict."""
    calls = _capture_argv(
        monkeypatch, rc=1,
        stderr_text="WARNING: Some web client https formats have been skipped — "
                    "YouTube may have enabled the SABR-only streaming experiment\n"
                    "ERROR: This video is unavailable",
    )
    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(SystemExit) as exc:
        download.download_url(URL, tmp_path)
    assert len(calls) == 1, "the advisory warning fired the android retry"
    assert "SABR-style" not in str(exc.value)


def test_cookie_advisory_lines_do_not_classify_as_login_wall(monkeypatch):
    """The android retry itself provokes 'Skipping client "android" since it
    does not support cookies' when the user passed --cookies-from-browser; the
    word must not route a 403 to login advice."""
    monkeypatch.setattr(download, "_ytdlp_version", lambda: "2026.01.01")
    message = download._classify_download_failure(
        'Skipping client "android" since it does not support cookies\n'
        "ERROR: HTTP Error 403: Forbidden",
        1, Path("/tmp/x"), retried_android=True,
    )
    assert "403" in message
    assert "login-walled" not in message
