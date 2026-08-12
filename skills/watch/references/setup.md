# First-run setup and remediation

Read this when Step 0's preflight said so: `--json` reported `first_run: true` or
`can_proceed: false`, or the silent `--check` exited non-zero. Nothing here runs on an
already-configured install.

## The preflight's answer

`python3 "${SKILL_DIR}/scripts/setup.py" --json` emits
`{status, can_proceed, first_run, setup_complete, missing_binaries, whisper_backend,
has_api_key, config_file, watch_detail, platform}`.

- `status` describes the *ideal* state (`ready | needs_install | needs_key |
  needs_install_and_key`). A key is encouraged, so a keyless first run reads `needs_key`.
- `can_proceed` is the operational gate: binaries present AND (a transcription backend is
  configured OR setup was already completed). Branch on `can_proceed`/`first_run` to decide
  whether to run; use `status` to decide what to encourage.

The silent check's exit codes:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing binaries (`ffmpeg` / `ffprobe` / `yt-dlp`) | Run the installer |
| `3` | Genuine first run with no transcription backend | Run the installer to scaffold `.env`, then ask the transcription question below |
| `4` | Both missing | Installer, then the question |

Exit `3` only fires before setup is completed. Once `SETUP_COMPLETE=true` is recorded, a
keyless install returns exit 0 and is never nagged again.

## First-run flow (`first_run: true`)

Do these in order:

1. **If `missing_binaries` is non-empty, run the installer first** and confirm the binaries
   land. Do not skip this and jump to preferences.

   ```bash
   python3 "${SKILL_DIR}/scripts/setup.py"
   ```

   Idempotent — safe to re-run. On macOS with Homebrew it auto-installs `ffmpeg` and
   `yt-dlp`; on Linux/Windows it prints the exact install commands for the user to run. It
   scaffolds `~/.config/watch/.env` with commented placeholders at `0600` perms (it only
   writes the template when the file is absent, so let it create the file *before* you write
   any values into it).

2. **Ask the transcription question** with `AskUserQuestion` — one question, four options,
   in this order:

   - **Groq API key** (recommended — cheaper, faster). Follow up for the key, then write
     `GROQ_API_KEY=...` into `~/.config/watch/.env`. Keys: console.groq.com/keys.
   - **OpenAI API key**. Same, writing `OPENAI_API_KEY=...`. Keys: platform.openai.com/api-keys.
   - **Self-hosted server** (audio never leaves the machine; no key). Follow up for the URL
     and write `WATCH_WHISPER_ENDPOINT=<full /v1/audio/transcriptions URL>` — any
     OpenAI-compatible server works (whisper.cpp's `whisper-server`, speaches, LocalAI,
     vLLM). Optionally `WATCH_WHISPER_MODEL=...` (defaults to `whisper-1`).
   - **No transcription.** Run `python3 "${SKILL_DIR}/scripts/setup.py" --complete` to
     record the deliberate keyless choice — this is what stops every future nag — and tell
     the user videos without native captions will come back frames-only.

3. **Ask the default-detail question** with `AskUserQuestion`. Present the options in this
   exact order — lightest to heaviest — and keep `(recommended)` on `balanced` even though
   it is not first (do **not** reorder to put the recommended option first):

   - `transcript` — no frames at all, transcript only (skips video download when captions exist).
   - `efficient` — fast keyframe pass (cap 50).
   - `balanced` (recommended) — scene-aware frames (cap 100, default).
   - `token-burner` — scene-aware, uncapped (maximum fidelity; high token cost).

   Write the answer into `~/.config/watch/.env` as a bare key on its own line — **no
   trailing inline comment**:

   ```bash
   WATCH_DETAIL=balanced
   ```

   If they skip the question, keep the recommended default.

4. **Finish.** After a key/endpoint was written, re-run the installer once (it records
   `SETUP_COMPLETE=true` when a backend is present). For the no-transcription choice,
   `--complete` in step 2 already recorded it.

## Regression remediation (`can_proceed: false`, `first_run: false`)

Setup was finished before but the environment regressed (e.g. `missing_binaries` after an
OS change). Run the installer to remediate, then proceed. Don't re-ask the preference
questions.

## Where settings live

Every setting resolves as **process env → `~/.config/watch/.env` → `./.env`** (the
project-local fallback), with one exception: `SETUP_COMPLETE` is read from the machine
config only, so a cloned repo shipping the marker cannot suppress another user's first-run
flow. `export KEY=value` lines and quoted values parse fine; keep `WATCH_DETAIL` free of
trailing comments.
