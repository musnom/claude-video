#!/usr/bin/env python3
"""Shared /watch configuration helpers."""
from __future__ import annotations

import codecs
import os
import sys
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "watch"
CONFIG_FILE = CONFIG_DIR / ".env"
_IMPORT_TIME_CONFIG_FILE = CONFIG_FILE

DEFAULT_DETAIL = "balanced"

DETAILS = {"transcript", "efficient", "balanced", "token-burner"}

# Byte-order marks, longest first so the 2-byte UTF-16 marks cannot shadow the
# 4-byte UTF-32 ones (BOM_UTF32_LE starts with BOM_UTF16_LE).
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# Last-resort legacy codecs, tried before the lossy fallback. Kept as a module
# constant rather than read from `locale` so tests can pin it — on macOS
# getpreferredencoding() returns UTF-8, which would leave this branch
# unreachable and therefore untested.
_ANSI_ENCODINGS: tuple[str, ...] = ("cp1252",)

_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Emit a diagnostic at most once per key, and never let it raise.

    This runs on paths where the console may not have been reconfigured yet
    (`python whisper.py ...` never calls ensure_utf8_console), so a warning that
    itself hits an encoding error would replace a recoverable problem with a
    crash. The message text is deliberately ASCII-only for the same reason.
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        sys.stderr.write(message)
        sys.stderr.flush()
    except Exception:
        pass


def ensure_utf8_console() -> None:
    """Force stdout/stderr to UTF-8.

    A no-op on POSIX. On Windows the streams default to the ANSI code page
    whenever they are piped — which is always, under an agent harness — so
    printing the report's arrow, an em dash, or a CJK/emoji video title raises
    UnicodeEncodeError. That happens after the download and every frame
    extraction have already succeeded, so the whole run's output is lost at the
    last step. (Attached to a real console Python uses the console API and
    never raises, which is why this only bites under an agent.)

    errors="backslashreplace" is load-bearing even under UTF-8: a path decoded
    with surrogateescape carries lone surrogates that strict UTF-8 rejects, and
    watch.py prints the work dir, every frame path, and the source argument.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


def _sniff_env_encoding(data: bytes) -> str:
    """Best-guess codec for a .env file's bytes."""
    for bom, encoding in _BOM_ENCODINGS:
        if data.startswith(bom):
            return encoding
    # Bare UTF-16 has no BOM but is half NUL bytes. Which half tells us the
    # endianness: ASCII text little-endian puts the NUL at odd offsets.
    head = data[:128]
    if b"\x00" in head:
        odd = sum(1 for i, b in enumerate(head) if b == 0 and i % 2)
        even = sum(1 for i, b in enumerate(head) if b == 0 and not i % 2)
        return "utf-16-le" if odd >= even else "utf-16-be"
    return "utf-8"


def decode_env_bytes(data: bytes, path: Path | None = None) -> str:
    """Decode a .env file written by any tool a user is likely to reach for.

    PowerShell's Out-File defaults to UTF-16LE with a BOM and Notepad adds a
    UTF-8 BOM; both used to escape the caller's `except OSError` as a
    UnicodeDecodeError (it subclasses ValueError), taking down the whole run —
    or, worse, decode into keys prefixed with a BOM that silently never match.
    """
    try:
        return data.decode(_sniff_env_encoding(data))
    except (UnicodeDecodeError, LookupError):
        pass
    for encoding in _ANSI_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    _warn_once(
        f"decode:{path}",
        f"[watch] WARNING: could not decode {path} as UTF-8; "
        "reading it with replacement characters. Re-save it as UTF-8.\n",
    )
    # Cannot raise, so this is always a terminating link in the chain.
    return data.decode("utf-8", errors="replace")


def _config_file() -> Path:
    """The user config path, resolved at call time.

    ``Path.home()`` is re-resolved per call rather than frozen at import: the
    test suite (and any long-lived host) changes HOME after this module is
    imported, and whisper.py's historical private path list resolved at call
    time — that behavior is the compatibility anchor. A test that monkeypatches
    ``config.CONFIG_FILE`` still wins: an attribute that no longer matches its
    import-time value was changed deliberately.
    """
    if CONFIG_FILE is not _IMPORT_TIME_CONFIG_FILE:
        return CONFIG_FILE
    return Path.home() / ".config" / "watch" / ".env"


def env_file_paths(include_cwd: bool = True) -> list[Path]:
    """Where settings may live, in precedence order after the process env."""
    paths = [_config_file()]
    if include_cwd:
        paths.append(Path.cwd() / ".env")
    return paths


def read_setting(name: str, include_cwd: bool = True) -> str | None:
    """One resolution order for every consumer: process env, then
    ``~/.config/watch/.env``, then ``./.env``.

    This is the order whisper.py always used; config and setup used to stop at
    the config file, so a project-local ``GROQ_API_KEY`` transcribed fine while
    ``setup.py --check`` reported ``needs_key`` and the session hook nagged.
    SKILL.md promises the cwd fallback for all of them.

    ``include_cwd=False`` exists for ``SETUP_COMPLETE``: keys are credentials
    the user placed for a project, but the setup marker is a statement about
    this *machine* — honoring it from a project ``.env`` would let a cloned
    repo suppress another user's first-run flow.
    """
    value = os.environ.get(name)
    if value and value.strip():
        return value.strip()
    for path in env_file_paths(include_cwd):
        value = read_env_file(path).get(name)
        if value:
            return value
    return None


def read_env_file(path: Path | None = None) -> dict[str, str]:
    if path is None:
        path = _config_file()
    values: dict[str, str] = {}
    if not path.exists():
        return values
    try:
        lines = decode_env_bytes(path.read_bytes(), path).splitlines()
    except OSError:
        return values
    for line in lines:
        raw = line.strip()
        # `export KEY=value` — a user who writes their .env to be
        # `source`-able — used to parse into the key "export KEY", which
        # silently never matched anything.
        if raw.startswith("export ") or raw.startswith("export\t"):
            raw = raw[7:].lstrip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, _, value = raw.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]
        else:
            # Strip an inline comment (a '#' preceded by whitespace) from an
            # unquoted value. Without this, `WATCH_DETAIL=balanced  # note`
            # parses as "balanced  # note", fails validation, and silently
            # falls back to the default. Keeps '#' inside quotes / API keys.
            for i, ch in enumerate(value):
                if ch == "#" and i > 0 and value[i - 1] in " \t":
                    value = value[:i].rstrip()
                    break
        values[key.strip()] = value
    return values


def get_config() -> dict[str, object]:
    detail = read_setting("WATCH_DETAIL") or DEFAULT_DETAIL
    if detail not in DETAILS:
        detail = DEFAULT_DETAIL

    return {
        "detail": detail,
        "config_file": str(_config_file()),
    }


def frame_cap(detail: str) -> int | None:
    if detail == "efficient":
        return 50
    if detail == "balanced":
        return 100
    if detail == "token-burner":
        return None
    if detail == "transcript":
        return None
    return 100
