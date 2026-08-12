"""The SessionStart hook (hooks/scripts/check-setup.sh).

Runs the real script under bash with a fabricated HOME, so the Windows branch
is exercised on POSIX by shadowing `uname` on PATH rather than being reasoned
about.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "scripts" / "check-setup.sh"

READY_ENV = "GROQ_API_KEY=abc\nSETUP_COMPLETE=true\n"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="hook is a bash script"
)


def _stub_bin(tmp_path: Path, *, uname: str | None = None) -> Path:
    """A PATH dir with the binaries the hook probes for."""
    d = tmp_path / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe", "yt-dlp"):
        p = d / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    if uname is not None:
        p = d / "uname"
        p.write_text(f'#!/bin/sh\necho "{uname}"\n', encoding="utf-8")
        p.chmod(0o755)
    return d


def _write_env(home: Path, data: bytes, mode: int = 0o600) -> Path:
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / ".env"
    path.write_bytes(data)
    path.chmod(mode)
    return path


def _run(home: Path, stub: Path, *, extra_env: dict[str, str] | None = None):
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env
    )


# --- .env encodings Windows actually writes -----------------------------------
# The hook must stay silent when setup is complete. If it cannot read
# SETUP_COMPLETE it prints the first-run hint on every single session.


@pytest.mark.parametrize(
    "label,data",
    [
        ("utf-8", READY_ENV.encode("utf-8")),
        ("utf-8-sig", READY_ENV.encode("utf-8-sig")),
        ("powershell-utf16le-bom", READY_ENV.encode("utf-16")),
        ("utf-16be-bom", b"\xfe\xff" + READY_ENV.encode("utf-16-be")),
        ("crlf", READY_ENV.replace("\n", "\r\n").encode("utf-8")),
    ],
)
def test_silent_when_setup_complete(tmp_path, label, data):
    home = tmp_path / "home"
    _write_env(home, data)
    result = _run(home, _stub_bin(tmp_path))
    assert result.stdout == "", f"{label}: expected silence, got {result.stdout!r}"


def test_bom_does_not_corrupt_the_first_key(tmp_path):
    """A BOM attaches to whichever key comes first, so order it first here.

    Without the strip, awk sees a field named "﻿SETUP_COMPLETE", never
    matches, and the hook prints the first-run hint forever.
    """
    home = tmp_path / "home"
    _write_env(home, "SETUP_COMPLETE=true\nGROQ_API_KEY=abc\n".encode("utf-8-sig"))
    assert _run(home, _stub_bin(tmp_path)).stdout == ""


def test_tr_filter_is_locale_independent(tmp_path):
    """BSD tr errors on the UTF-16 BOM in a UTF-8 locale without LC_ALL=C."""
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-16"))
    stub = _stub_bin(tmp_path)
    for locale in ("en_US.UTF-8", "C"):
        result = _run(home, stub, extra_env={"LC_ALL": locale})
        assert result.stdout == "", f"LC_ALL={locale}: {result.stdout!r}"


# --- permission warning -------------------------------------------------------


def test_warns_on_loose_perms_on_posix(tmp_path):
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-8"), mode=0o644)
    result = _run(home, _stub_bin(tmp_path))
    assert "permissions 644" in result.stdout


def test_no_perm_warning_under_windows_shell(tmp_path):
    """Windows synthesizes 644/666/777, so the check can only false-positive.

    Exercised on POSIX by shadowing uname, which the hook resolves from PATH.
    """
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-8"), mode=0o644)
    stub = _stub_bin(tmp_path, uname="MINGW64_NT-10.0-26100")
    result = _run(home, stub)
    assert "WARNING" not in result.stdout, result.stdout
    assert result.stdout == ""


def test_ostype_also_triggers_the_windows_branch(tmp_path):
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-8"), mode=0o644)
    result = _run(home, _stub_bin(tmp_path), extra_env={"OSTYPE": "msys"})
    assert "WARNING" not in result.stdout


# --- the hook still does its job ----------------------------------------------


def test_hint_when_binaries_missing(tmp_path):
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-8"))
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run(home, empty)
    assert "needs ffmpeg + yt-dlp" in result.stdout


def test_hint_when_no_api_key(tmp_path):
    home = tmp_path / "home"
    _write_env(home, b"SETUP_COMPLETE=\n")
    result = _run(home, _stub_bin(tmp_path))
    assert "GROQ_API_KEY" in result.stdout


def test_hook_script_has_no_crlf():
    """Pairs with *.sh eol=lf in .gitattributes.

    bash dies on `set -euo pipefail\\r` before evaluating anything, so a CRLF
    checkout silently disables the hook.
    """
    assert b"\r\n" not in HOOK.read_bytes()


# --- resolution parity with setup.py --check ------------------------------------
# The hook must agree with setup.py about what counts as configured, or the nag
# mismatch just moves: a value the hook cannot read is a hint printed on every
# session for a user who is fully set up.


def test_value_containing_equals_survives(tmp_path):
    """awk -F= truncated `GROQ_API_KEY=abc==def` at the first '='; base64
    padding in real keys makes this a real shape."""
    home = tmp_path / "home"
    _write_env(home, b"GROQ_API_KEY=abc==def\nSETUP_COMPLETE=true\n")
    assert _run(home, _stub_bin(tmp_path)).stdout == ""


def test_export_prefixed_lines_are_read(tmp_path):
    home = tmp_path / "home"
    _write_env(home, b"export SETUP_COMPLETE=true\nexport GROQ_API_KEY=abc\n")
    assert _run(home, _stub_bin(tmp_path)).stdout == ""


def test_key_without_marker_is_silent(tmp_path):
    """A key alone (e.g. set in the shell profile) is a configured user. The
    old rule required SETUP_COMPLETE specifically and printed '/watch: ready.'
    on EVERY session — the exact spam the script's header promises not to emit."""
    home = tmp_path / "home"
    _write_env(home, b"GROQ_API_KEY=abc\n")
    result = _run(home, _stub_bin(tmp_path))
    assert result.stdout == "", result.stdout


def test_endpoint_only_user_is_silent(tmp_path):
    """A self-hosted WATCH_WHISPER_ENDPOINT needs no key; the hook used to nag
    that user for a cloud key on every session because it never read the var."""
    home = tmp_path / "home"
    _write_env(home, b"WATCH_WHISPER_ENDPOINT=http://127.0.0.1:8080/v1/audio/transcriptions\n")
    assert _run(home, _stub_bin(tmp_path)).stdout == ""


def test_project_env_key_is_honored(tmp_path):
    """Same ./.env fallback config.read_setting has — run the hook from a
    project directory whose .env carries the key."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("GROQ_API_KEY=abc\n", encoding="utf-8")
    stub = _stub_bin(tmp_path)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=project,
    )
    assert result.stdout == "", result.stdout


def test_project_env_cannot_mark_setup_complete(tmp_path):
    """SETUP_COMPLETE from a project .env must NOT silence the first-run hint."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SETUP_COMPLETE=true\n", encoding="utf-8")
    stub = _stub_bin(tmp_path)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": f"{stub}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True, env=env, cwd=project,
    )
    assert "GROQ_API_KEY" in result.stdout


def test_remediation_hint_names_a_real_path(tmp_path):
    """The old hint printed a literal `$CLAUDE_PLUGIN_ROOT` the user's shell
    cannot expand, so copy-pasting it ran `python3 /skills/...`. The printed
    path must exist (resolved from the script's own location as fallback)."""
    home = tmp_path / "home"
    empty = tmp_path / "empty"
    empty.mkdir()
    result = subprocess.run(
        ["bash", str(HOOK)], capture_output=True, text=True,
        env={"HOME": str(home), "USERPROFILE": str(home),
             "PATH": f"{empty}:/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert "needs ffmpeg + yt-dlp" in result.stdout
    assert "$CLAUDE_PLUGIN_ROOT" not in result.stdout
    match = re.search(r"`python3 (\S+/setup\.py)`", result.stdout)
    assert match, result.stdout
    assert Path(match.group(1)).is_file(), match.group(1)


def test_hooks_json_points_at_the_script_this_suite_tests():
    """A manifest typo silently disables the hook on every install while all
    the script tests above stay green."""
    import json
    import shlex

    repo_root = HOOK.parent.parent.parent
    manifest = json.loads((repo_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    entries = manifest["hooks"]["SessionStart"]
    commands = [
        h["command"]
        for entry in entries
        for h in entry["hooks"]
        if h.get("type") == "command"
    ]
    assert len(commands) == 1, manifest
    resolved = commands[0].replace("${CLAUDE_PLUGIN_ROOT}", str(repo_root))
    script = Path(shlex.split(resolved)[-1])
    assert script == HOOK
    assert script.is_file()


def test_unquoted_inline_comment_is_stripped(tmp_path):
    """`SETUP_COMPLETE=true  # done` must read as "true" — the same rule
    config.read_env_file applies — or the marker never matches and the hook
    nags every session."""
    home = tmp_path / "home"
    _write_env(home, b"GROQ_API_KEY=abc\nSETUP_COMPLETE=true  # done via wizard\n")
    assert _run(home, _stub_bin(tmp_path)).stdout == ""


def test_missing_ffprobe_gets_the_binaries_hint(tmp_path):
    """ffprobe is in setup.py's REQUIRED_BINARIES; a minimal ffmpeg install
    without it must not read as ready when --check would exit 2."""
    home = tmp_path / "home"
    _write_env(home, READY_ENV.encode("utf-8"))
    d = tmp_path / "bin"
    d.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "yt-dlp"):          # deliberately no ffprobe
        p = d / name
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    # System dirs kept for bash/tr/awk; ffprobe lives in Homebrew's prefix on
    # macOS, so leaving that prefix out is what makes it "missing" here.
    env = {
        "HOME": str(home), "USERPROFILE": str(home),
        "PATH": f"{d}:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    result = subprocess.run(["bash", str(HOOK)], capture_output=True, text=True, env=env)
    assert "needs ffmpeg + yt-dlp" in result.stdout
