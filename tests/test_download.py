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


def _capture_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Stub subprocess.run inside download.py and record every argv."""
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _Result()

    monkeypatch.setattr(download.subprocess, "run", fake_run)
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
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = stderr = ""

        return _Result()

    monkeypatch.setattr(download.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(download.subprocess, "run", fake_run)
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
