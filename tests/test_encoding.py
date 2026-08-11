"""Encoding behaviour that only bites on Windows, exercised on POSIX.

Three distinct defects, three mechanisms:

* Console ENCODE — driven with PYTHONIOENCODING, capturing bytes so the
  parent's own decode is not part of what is under test.
* Config-file DECODE — byte-literal .env fixtures written with write_bytes().
* Subprocess DECODE — a real multibyte locale, plus an AST guard that runs
  everywhere so the invariant survives on machines without one.
"""
from __future__ import annotations

import ast
import json
import locale
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
WATCH = SCRIPTS / "watch.py"
SETUP = SCRIPTS / "setup.py"


# --- console encode -----------------------------------------------------------


def _run_bytes(args: list[str], **env_extra) -> subprocess.CompletedProcess:
    """Run a script capturing raw bytes, never letting the parent decode."""
    env = os.environ.copy()
    env.pop("WATCH_DETAIL", None)
    env.update(env_extra)
    return subprocess.run(args, capture_output=True, env=env)


@pytest.mark.parametrize("codepage", ["cp1252", "ascii", "cp932"])
def test_report_survives_a_legacy_console(cut_clip: Path, tmp_path, codepage):
    """The report contains U+2192 and an em dash; --start forces the arrow.

    Pre-fix this exits non-zero *after* the download and every frame extraction
    have succeeded, so the entire run's output is thrown away at the last step.
    """
    result = _run_bytes(
        [sys.executable, str(WATCH), str(cut_clip), "--no-whisper",
         "--start", "1", "--end", "3", "--out-dir", str(tmp_path / "w")],
        PYTHONIOENCODING=codepage,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "→".encode("utf-8") in result.stdout


def test_progress_lines_survive_legacy_console(cut_clip: Path, tmp_path):
    """The ellipsis in the progress lines goes to stderr — a separate stream."""
    result = _run_bytes(
        [sys.executable, str(WATCH), str(cut_clip), "--no-whisper",
         "--out-dir", str(tmp_path / "w")],
        PYTHONIOENCODING="cp1252",
    )
    assert result.returncode == 0
    assert "…".encode("utf-8") in result.stderr


def test_non_ascii_title_survives_legacy_console(tmp_path):
    """resolve_local uses the filename as the title, so a CJK/emoji name is the
    no-network way to reproduce a CJK/emoji video title."""
    import shutil as _shutil
    from conftest import build_cut_clip

    clip = tmp_path / "🎬日本語のビデオ.mp4"
    try:
        build_cut_clip(clip, n=4)
    except (OSError, UnicodeEncodeError):  # pragma: no cover
        pytest.skip("filesystem cannot hold a non-ASCII filename")
    assert _shutil.which("ffmpeg")

    result = _run_bytes(
        [sys.executable, str(WATCH), str(clip), "--no-whisper",
         "--out-dir", str(tmp_path / "w")],
        PYTHONIOENCODING="cp1252",
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "日本語のビデオ".encode("utf-8") in result.stdout


def test_setup_installer_survives_legacy_console(tmp_path):
    """cmd_install prints an em dash and the config path."""
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        p = stub / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    result = _run_bytes(
        [sys.executable, str(SETUP)],
        PYTHONIOENCODING="cp932",
        PATH=f"{stub}:{os.environ.get('PATH','')}",
        HOME=str(home),
        USERPROFILE=str(home),
        GROQ_API_KEY="",
        OPENAI_API_KEY="",
    )
    # exit 3 == "one step left: add a Whisper API key", which is the branch
    # carrying the em dash. What matters is that it did not die encoding it.
    assert result.returncode in (0, 3), result.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in result.stderr


def test_utf8_alone_is_not_enough_for_surrogates():
    """Why ensure_utf8_console passes errors= and not just encoding=.

    Paths come back from os.fsdecode with surrogateescape, and strict UTF-8
    rejects lone surrogates. Runs everywhere, unlike the filesystem-dependent
    test below.
    """
    import io

    surrogate = b"bad\xff.mp4".decode("utf-8", "surrogateescape")
    strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        strict.write(surrogate)
        strict.flush()

    lenient = io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", errors="backslashreplace"
    )
    lenient.write(surrogate)
    lenient.flush()


def test_surrogate_path_does_not_crash(cut_clip: Path, tmp_path):
    """A path with an undecodable byte survives as lone surrogates.

    Strict UTF-8 rejects those, so this fails with encoding="utf-8" alone and
    is what makes errors="backslashreplace" load-bearing rather than cosmetic.
    """
    weird = tmp_path / b"out\xff".decode("utf-8", "surrogateescape")
    try:
        weird.mkdir()
    except (OSError, UnicodeEncodeError):  # pragma: no cover
        pytest.skip("filesystem rejects surrogate paths")
    result = _run_bytes(
        [sys.executable, str(WATCH), str(cut_clip), "--no-whisper",
         "--out-dir", str(weird)],
        PYTHONIOENCODING="cp1252",
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


# --- config-file decode -------------------------------------------------------

BODY = "GROQ_API_KEY=abc\nWATCH_DETAIL=efficient\n"
EXPECTED = {"GROQ_API_KEY": "abc", "WATCH_DETAIL": "efficient"}

ENV_ENCODINGS = {
    "utf-8": BODY.encode("utf-8"),
    "utf-8-sig": BODY.encode("utf-8-sig"),
    "powershell-utf16le-bom": BODY.encode("utf-16"),
    "utf-16be-bom": b"\xfe\xff" + BODY.encode("utf-16-be"),
    "bare-utf-16-le": BODY.encode("utf-16-le"),
    "bare-utf-16-be": BODY.encode("utf-16-be"),
    "utf-32-bom": BODY.encode("utf-32"),
    "ansi-cp1252": BODY.encode("cp1252"),
    "cp932": BODY.encode("cp932"),
}


@pytest.mark.parametrize("label", sorted(ENV_ENCODINGS))
def test_env_readable_in_every_windows_encoding(tmp_path, label):
    path = tmp_path / ".env"
    path.write_bytes(ENV_ENCODINGS[label])
    assert config.read_env_file(path) == EXPECTED


def test_bom_never_leaks_into_a_key_name(tmp_path):
    """The silent-corruption case: parses fine but the key never matches."""
    path = tmp_path / ".env"
    path.write_bytes(BODY.encode("utf-8-sig"))
    result = config.read_env_file(path)
    assert "GROQ_API_KEY" in result
    assert "﻿GROQ_API_KEY" not in result


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"\xff\xfe\x00\x00A", "utf-32"),
        (b"\xef\xbb\xbfA", "utf-8-sig"),
        (b"\xff\xfeA\x00", "utf-16"),
        (b"\xfe\xff\x00A", "utf-16"),
        (b"A\x00B\x00", "utf-16-le"),
        (b"\x00A\x00B", "utf-16-be"),
        (b"plain ascii", "utf-8"),
    ],
)
def test_sniff_table(data, expected):
    assert config._sniff_env_encoding(data) == expected


def test_undecodable_env_never_raises(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(bytes(range(256)) * 4)
    assert isinstance(config.read_env_file(path), dict)


def test_ansi_fallback_is_pluggable(tmp_path, monkeypatch):
    """Proves the Japanese-Windows path without a Japanese Windows box."""
    monkeypatch.setattr(config, "_ANSI_ENCODINGS", ("cp932",))
    path = tmp_path / ".env"
    path.write_bytes("GROQ_API_KEY=abc\nWATCH_DETAIL=efficient\n".encode("cp932"))
    assert config.read_env_file(path) == EXPECTED


def test_clean_env_is_silent(tmp_path, capsys, monkeypatch):
    """A losslessly-decoded file must not warn, or the silence assertions in
    test_setup.py start lying."""
    monkeypatch.setattr(config, "_WARNED", set())
    path = tmp_path / ".env"
    path.write_bytes(BODY.encode("utf-16"))
    config.read_env_file(path)
    assert capsys.readouterr().err == ""


def test_fallback_warns_once_and_is_ascii(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(config, "_WARNED", set())
    monkeypatch.setattr(config, "_ANSI_ENCODINGS", ())
    path = tmp_path / ".env"
    path.write_bytes(b"GROQ_API_KEY=\xff\xfe\xfd\n")
    for _ in range(5):
        config.read_env_file(path)
    err = capsys.readouterr().err
    assert err.count("could not decode") == 1
    assert err.isascii(), "a warning that needs encoding can itself crash"


def test_unreadable_env_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_bytes(BODY.encode("utf-8"))

    def boom(self, *a, **k):
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert config.read_env_file(path) == {}


def test_all_three_readers_agree(tmp_path, monkeypatch):
    """config, setup and whisper must not drift apart again."""
    import setup as setup_mod
    import whisper as whisper_mod

    for label, data in ENV_ENCODINGS.items():
        home = tmp_path / label
        cfg = home / ".config" / "watch"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / ".env").write_bytes(data)
        monkeypatch.setattr(setup_mod, "CONFIG_FILE", cfg / ".env")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)

        assert config.read_env_file(cfg / ".env")["GROQ_API_KEY"] == "abc", label
        assert setup_mod._read_env_key("GROQ_API_KEY") == "abc", label
        assert whisper_mod.load_api_key() == ("groq", "abc"), label


def test_inline_comment_stripped_by_whisper_reader(tmp_path, monkeypatch):
    """whisper.py's private parser lacked this, so the comment went out as part
    of the bearer token and came back as a 401 that looked like a bad key."""
    import whisper as whisper_mod

    home = tmp_path / "home"
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text("GROQ_API_KEY=abc  # prod key\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert whisper_mod.load_api_key() == ("groq", "abc")


# --- subprocess decode --------------------------------------------------------


def _legacy_locale() -> str | None:
    """Find a real multibyte locale.

    LC_ALL=C is useless here: CPython auto-enables UTF-8 mode under it, and
    neither PYTHONUTF8=0 nor -X utf8=0 turns that back off.
    """
    for loc in ("ja_JP.SJIS", "ko_KR.eucKR", "zh_CN.GB18030", "ja_JP.eucJP"):
        out = subprocess.run(
            [sys.executable, "-c",
             "import locale;print(locale.getpreferredencoding(False))"],
            env={**os.environ, "LC_ALL": loc}, capture_output=True, text=True,
        )
        if out.returncode == 0 and out.stdout.strip().upper() not in ("UTF-8", "UTF8"):
            return loc
    return None


LEGACY_LOCALE = _legacy_locale()

PROBE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import frames
print(json.dumps(frames.get_metadata(sys.argv[2])))
"""


@pytest.mark.skipif(LEGACY_LOCALE is None, reason="no multibyte locale available")
def test_get_metadata_under_legacy_locale(tmp_path):
    """ffprobe echoes the filename in its JSON, so a CJK name in a SJIS locale
    reproduces the crash: text=True would decode UTF-8 bytes as shift_jis."""
    from conftest import build_cut_clip

    clip = tmp_path / "日本語.mp4"
    try:
        build_cut_clip(clip, n=4)
    except (OSError, UnicodeEncodeError):  # pragma: no cover
        pytest.skip("filesystem cannot hold a non-ASCII filename")

    result = subprocess.run(
        [sys.executable, "-c", PROBE, str(SCRIPTS), str(clip)],
        env={**os.environ, "LC_ALL": LEGACY_LOCALE},
        capture_output=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["duration_seconds"] > 0


def _subprocess_run_calls(tree: ast.AST) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr == "run":
            base = fn.value
            if isinstance(base, ast.Name) and base.id == "subprocess":
                out.append(node)
    return out


def test_every_text_subprocess_pins_utf8():
    """Anything decoding child output as text must say which codec.

    Otherwise it uses the parent's locale codec and dies on a CJK Windows box.
    """
    offenders = []
    for src in sorted(SCRIPTS.glob("*.py")):
        for call in _subprocess_run_calls(ast.parse(src.read_text(encoding="utf-8"))):
            kw = {k.arg for k in call.keywords}
            if {"text", "universal_newlines"} & kw and not {"encoding"} & kw:
                offenders.append(f"{src.name}:{call.lineno}")
    assert not offenders, f"text=True without encoding=: {offenders}"


def test_rawvideo_dedup_read_stays_in_bytes():
    """_thumb_frames slices stdout as raw grayscale pixels."""
    tree = ast.parse((SCRIPTS / "frames.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_thumb_frames"
    )
    calls = _subprocess_run_calls(fn)
    assert len(calls) == 1
    kw = {k.arg for k in calls[0].keywords}
    assert not ({"text", "encoding", "errors", "universal_newlines"} & kw), (
        "decoding the rawvideo stream would silently corrupt dedup"
    )
