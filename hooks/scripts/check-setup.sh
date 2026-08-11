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

# Load API keys from the config file without exporting them.
read_key() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    echo "${!name}"
    return
  fi
  if [[ -f "$CONFIG_FILE" ]]; then
    # Windows tooling writes this file in encodings awk cannot read: PowerShell's
    # Out-File defaults to UTF-16LE with a BOM, Notepad adds a UTF-8 BOM. Strip
    # NUL and BOM bytes first so the keys — all ASCII — survive; without this
    # SETUP_COMPLETE never matches and the hook nags on every session.
    #
    # LC_ALL=C is required, not cosmetic: in a UTF-8 locale BSD tr errors with
    # "Illegal byte sequence" on the UTF-16 BOM and silently passes the UTF-8
    # BOM straight through. \015 is belt-and-braces for CRLF (POSIX awk's
    # [:space:] already strips a trailing CR).
    LC_ALL=C tr -d '\000\377\376\357\273\277\015' < "$CONFIG_FILE" | awk -F= -v k="$name" '
      /^[[:space:]]*#/ { next }
      $1 == k {
        sub(/^[[:space:]]*/, "", $2); sub(/[[:space:]]*$/, "", $2);
        gsub(/^["'\'']|["'\'']$/, "", $2);
        print $2; exit
      }
    '
  fi
}

HAS_FFMPEG=""
HAS_YTDLP=""
command -v ffmpeg >/dev/null 2>&1 && HAS_FFMPEG="yes"
command -v yt-dlp >/dev/null 2>&1 && HAS_YTDLP="yes"

HAS_GROQ="$(read_key GROQ_API_KEY)"
HAS_OPENAI="$(read_key OPENAI_API_KEY)"
SETUP_COMPLETE="$(read_key SETUP_COMPLETE)"

# Fully configured → silent (Claude can surface status on demand via --check).
if [[ "$SETUP_COMPLETE" == "true" && -n "$HAS_FFMPEG" && -n "$HAS_YTDLP" ]]; then
  exit 0
fi

# First-run / partially-configured → one-line hint.
if [[ -z "$HAS_FFMPEG" || -z "$HAS_YTDLP" ]]; then
  echo "/watch: needs ffmpeg + yt-dlp. Run \`python3 \$CLAUDE_PLUGIN_ROOT/skills/watch/scripts/setup.py\` once to install and scaffold config."
elif [[ -z "$HAS_GROQ" && -z "$HAS_OPENAI" ]]; then
  echo "/watch: ready for videos with native captions. Add GROQ_API_KEY (preferred) or OPENAI_API_KEY to ~/.config/watch/.env to unlock Whisper fallback."
else
  echo "/watch: ready."
fi
