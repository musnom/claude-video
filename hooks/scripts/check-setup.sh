#!/usr/bin/env bash
# SessionStart hook for /watch — one-line status so users know what's wired up.
# Silent on ready state to avoid spam. Points at the installer when something
# is missing.
set -euo pipefail

CONFIG_FILE="$HOME/.config/watch/.env"

# Windows filesystems synthesise POSIX modes (MSYS/Cygwin report 644/666/777
# regardless of the real ACL), so the permission check below can only ever fail
# there and `chmod 600` cannot clear it — an unfixable warning on every single
# session. Detect via `uname -s` rather than $OSTYPE alone: uname is resolved
# from PATH, which makes this branch testable by shadowing it with a stub.
_is_windows_shell() {
  case "${OSTYPE:-}" in msys*|cygwin*|win32) return 0 ;; esac
  case "$(uname -s 2>/dev/null || echo unknown)" in MINGW*|MSYS*|CYGWIN*) return 0 ;; esac
  return 1
}

# Warn if the secrets file has loose permissions.
if [[ -f "$CONFIG_FILE" ]] && ! _is_windows_shell; then
  perms=$(stat -c '%a' "$CONFIG_FILE" 2>/dev/null || stat -f '%Lp' "$CONFIG_FILE" 2>/dev/null || echo "")
  if [[ -n "$perms" && "$perms" != "600" && "$perms" != "400" ]]; then
    echo "/watch: WARNING — $CONFIG_FILE has permissions $perms (should be 600)."
    echo "  Fix: chmod 600 $CONFIG_FILE"
  fi
fi

# Read one value out of a .env file, tolerating the encodings Windows tooling
# writes and the shapes users write.
read_env_from() {
  local file="$1" name="$2"
  # Windows tooling writes this file in encodings awk cannot read: PowerShell's
  # Out-File defaults to UTF-16LE with a BOM, Notepad adds a UTF-8 BOM. Strip
  # NUL and BOM bytes first so the keys — all ASCII — survive; without this
  # SETUP_COMPLETE never matches and the hook nags on every session.
  #
  # LC_ALL=C is required, not cosmetic: in a UTF-8 locale BSD tr errors with
  # "Illegal byte sequence" on the UTF-16 BOM and silently passes the UTF-8
  # BOM straight through. \015 is belt-and-braces for CRLF.
  #
  # index/substr rather than -F=: an API key containing '=' (base64 padding is
  # real) was silently truncated at the first one by field splitting. A leading
  # `export ` — a user who writes their .env to be source-able — is stripped so
  # the key still matches.
  LC_ALL=C tr -d '\000\377\376\357\273\277\015' < "$file" | awk -v k="$name" '
    /^[[:space:]]*#/ { next }
    {
      line = $0
      sub(/^[[:space:]]*export[[:space:]]+/, "", line)
      eq = index(line, "=")
      if (eq == 0) next
      key = substr(line, 1, eq - 1)
      gsub(/[[:space:]]/, "", key)
      if (key != k) next
      val = substr(line, eq + 1)
      sub(/^[[:space:]]*/, "", val); sub(/[[:space:]]*$/, "", val)
      # Same rule as config.read_env_file: quotes protect everything inside
      # them; an UNQUOTED value loses a trailing inline comment, or
      # `SETUP_COMPLETE=true  # done` reads as "true  # done" and fails the
      # == "true" compare — a nag on every session.
      if (val ~ /^".*"$/ || val ~ /^\x27.*\x27$/) {
        val = substr(val, 2, length(val) - 2)
      } else {
        sub(/[[:space:]]+#.*$/, "", val)
        sub(/[[:space:]]*$/, "", val)
      }
      print val; exit
    }
  '
}

# Resolution order mirrors config.read_setting: process env, then the machine
# config, then the project .env — except SETUP_COMPLETE, which is machine-config
# only (a cloned repo shipping the marker must not silence another user's
# first-run flow). The hook must agree with `setup.py --check` about what is
# configured, or the nag mismatch just moves.
read_key() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    echo "${!name}"
    return
  fi
  local file value
  for file in "$CONFIG_FILE" "$PWD/.env"; do
    if [[ "$name" == "SETUP_COMPLETE" && "$file" != "$CONFIG_FILE" ]]; then
      continue
    fi
    [[ -f "$file" ]] || continue
    value="$(read_env_from "$file" "$name")"
    if [[ -n "$value" ]]; then
      echo "$value"
      return
    fi
  done
}

HAS_FFMPEG=""
HAS_FFPROBE=""
HAS_YTDLP=""
command -v ffmpeg >/dev/null 2>&1 && HAS_FFMPEG="yes"
# ffprobe is in setup.py's REQUIRED_BINARIES too — minimal/static ffmpeg
# installs can carry ffmpeg without it, and the hook must agree with --check.
command -v ffprobe >/dev/null 2>&1 && HAS_FFPROBE="yes"
command -v yt-dlp >/dev/null 2>&1 && HAS_YTDLP="yes"

HAS_GROQ="$(read_key GROQ_API_KEY)"
HAS_OPENAI="$(read_key OPENAI_API_KEY)"
HAS_ENDPOINT="$(read_key WATCH_WHISPER_ENDPOINT)"
SETUP_COMPLETE="$(read_key SETUP_COMPLETE)"

# Ready → silent. "Ready" mirrors setup.py's can_proceed: binaries present AND
# (setup completed OR any transcription backend configured — including a
# self-hosted endpoint, which needs no key). The old rule required
# SETUP_COMPLETE specifically, so a user whose key lives in the shell profile
# got a "/watch: ready." line on EVERY session — the exact spam the header
# above promises not to emit.
if [[ -n "$HAS_FFMPEG" && -n "$HAS_FFPROBE" && -n "$HAS_YTDLP" ]] && \
   [[ "$SETUP_COMPLETE" == "true" || -n "$HAS_GROQ" || -n "$HAS_OPENAI" || -n "$HAS_ENDPOINT" ]]; then
  exit 0
fi

# First-run / partially-configured → one-line hint.
if [[ -z "$HAS_FFMPEG" || -z "$HAS_FFPROBE" || -z "$HAS_YTDLP" ]]; then
  # A real path, not a literal "$CLAUDE_PLUGIN_ROOT" the user's shell cannot
  # expand: resolve the plugin root from this script's own location, with the
  # env var (set when the harness runs the hook) as first choice.
  PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
  echo "/watch: needs ffmpeg + yt-dlp. Run \`python3 $PLUGIN_ROOT/skills/watch/scripts/setup.py\` once to install and scaffold config."
else
  echo "/watch: ready for videos with native captions. Add GROQ_API_KEY (preferred) or OPENAI_API_KEY to ~/.config/watch/.env — or point WATCH_WHISPER_ENDPOINT at a local server — to unlock the Whisper fallback."
fi
