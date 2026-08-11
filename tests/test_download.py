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
