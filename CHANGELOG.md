# Changelog

All notable changes to `/watch` are documented here.

## [0.3.0] — 2026-08-10

### Fixed
- **Frame extraction no longer breaks on ffmpeg 9.** ffmpeg 9.0 removed `-vsync`, which `frames.py` passed at both extraction sites, so `balanced` and `efficient` — the only frame-producing modes — returned nothing at all. Swapping to `-fps_mode` alone would have broken the other end: it does not exist before ffmpeg 5.1, and Ubuntu 22.04 LTS ships 4.4.2 through 2027. The local binary is now probed once per run (~17 ms, memoized) and the spelling it accepts is used. Verified against simulated 4.4 and 9.0 binaries: the old code fails 21 tests on ffmpeg 9, the new code passes on both.
- **`efficient` detail no longer crashes on a range with no keyframe.** Seeking past the only keyframe on a static clip starves the mjpeg encoder, so ffmpeg failed at encoder init and the uniform fallback twelve lines below was unreachable in exactly the case it existed for — while `balanced` handled the same window fine.
- **Auto-caption transcripts no longer repeat themselves.** YouTube paints captions into a rolling two-line window, emitting every spoken line three times; the old dedupe caught exact repeats and strict prefixes but not the rolling handoff. Measured on real tracks: 6,662 → 3,368 words, 49,210 → 24,679 on a 2h13m talk, ~1.98x across the board. Hand-authored subtitles are detected and passed through untouched.
- **Transcript timestamps no longer run late.** Cue bodies were terminated on the first whitespace-only line, but WebVTT ends a cue at a genuinely *empty* line and YouTube pads with a `" "` line before the painted text — so every painted cue was dropped whole and its text attributed to the settle cue after it, up to ~7 s behind the frames.
- **Transcript and frame timestamps now use one clock.** `format_transcript` built its own stamp, rendering t=3700 as `[61:40]` where the frame at the same instant read `1:01:40`. It now shares `frames.format_time`.
- **Caption fetches are bounded.** `--sub-langs en.*` is a regex yt-dlp fullmatches against every track: on one real video that meant 33 requests, 22 rate-limit rejections and 13.7 s to pick a file it would have picked from two. Now 2.6 s.
- **Non-English videos return their own captions**, not YouTube's machine translation of their own ASR, which sat alongside the untouched original.
- **HTML entities in captions are decoded**, so a hand-authored track no longer ships `R&amp;D` into the model's context.
- **Windows: the report no longer dies at the last step.** Piped stdout falls back to the ANSI code page, so printing the report's arrow, an em dash, or a CJK/emoji title raised `UnicodeEncodeError` *after* the download and every frame extraction had already succeeded.
- **Windows: `~/.config/watch/.env` is readable however it was written.** `UnicodeDecodeError` subclasses `ValueError`, so it escaped the `except OSError` guard — a PowerShell `Out-File` or ANSI file crashed the run, and a Notepad UTF-8 BOM parsed into a key that silently never matched, leaving the user permanently told they had no Whisper key. UTF-8, UTF-8-sig, UTF-16 (either endianness, with or without BOM), UTF-32 and legacy code pages all work now.
- **Windows: child-process output is decoded as UTF-8**, not the locale code page. A CJK filename previously killed `get_metadata` — the first call of every run.
- **Windows: the SessionStart hook stops nagging.** Windows synthesizes POSIX modes, so the permission check could only ever false-positive and `chmod 600` could not clear it; and the `.env` parser could not read BOM-prefixed files.
- The three duplicate `.env` parsers are now one. The copy in `whisper.py` never received the inline-comment stripping, so `GROQ_API_KEY=abc  # prod key` went out verbatim as the bearer token and returned a 401 that looked like a bad key.

### Added
- **Self-hosted / fully local transcription.** Point `WATCH_WHISPER_ENDPOINT` at any OpenAI-compatible `/v1/audio/transcriptions` server — whisper.cpp's `whisper-server`, speaches, LocalAI, vLLM — and audio never leaves the machine. No API key is sent, no dependency is added: the existing stdlib client already spoke the protocol. Optional `WATCH_WHISPER_MODEL`; force with `--whisper custom`; reported honestly as `whisper (custom)`.
- **The transcript-cue pass is now Step 3 of the default flow** rather than an optional extra. Visual selection misses the moments a speaker points at something, because pointing at a slide is a low-visual-change event.
- **A test suite that runs.** `requirements-dev.txt` plus a `.venv` workflow, and CI across ubuntu/macOS/Windows — there was no CI before. 71 tests to 187.
- Tests are isolated from the developer's own `~/.config/watch/.env`; four failed on a clean checkout if `WATCH_DETAIL` was configured.

### Changed
- Sub-hour transcript stamps round rather than truncate, so `59.7s` reads `[01:00]`. This is what makes them agree with the frame labels.
- Genuinely repeated speech in hand-authored subtitles is preserved. The old exact-equal-consecutive-cue rule deleted it — a speaker reading numbers aloud ("Four." / "Four."), a restated instruction, a lyric refrain.
- `*.sh` and `*.py` are pinned to LF. Git for Windows defaults to `core.autocrlf=true`, which checked out the hook script as CRLF and killed it on `set -euo pipefail\r`.

## [0.2.0] — 2026-06-29

### Added
- **`--detail` dial** with four modes — `transcript` (captions only, no frames), `efficient` (fast keyframe pass, cap 50), `balanced` (scene-aware, cap 100, default), and `token-burner` (scene-aware, uncapped). Set the default with `WATCH_DETAIL` in `~/.config/watch/.env`.
- **Frame deduplication** (default on; `--no-dedup` to disable). Before the budget cap, a pass downscales each frame to a 16×16 grayscale thumbnail and drops frames whose mean per-pixel difference from the last *kept* frame is within threshold — so the budget goes to distinct content instead of held slides and static recordings. The **Frames** report line shows how many near-duplicates were dropped.
- **Whisper auto-chunking.** Audio over the 25 MB upload cap is split into evenly sized chunks, transcribed per chunk, with segment timestamps shifted back into source time. Partial failures are tolerated — transcription only fails if *every* chunk fails, so length alone no longer breaks it.
- **`--timestamps T1,T2,…`** — grab a frame at each absolute timestamp; reserved against the cap, and the only frames produced under `--detail transcript`.
- **`--no-whisper`** — disable transcription entirely (frames only).
- pytest suite covering config, dedup, download, fixtures, frames, setup, timestamps, watch, and whisper (no network; ffmpeg-synthesized clips).

### Changed
- **Restructured into a self-contained `skills/watch/` package** so `SKILL.md` and its `scripts/` runtime are siblings in one folder. This fixes installs on Codex, Cursor, Copilot, and other Agent Skills hosts: `npx skills add` now copies the skill as a working unit instead of grabbing the root `SKILL.md` without its scripts.
- **Harness-agnostic path resolution** — `SKILL.md` resolves `$SKILL_DIR` from where it was Read instead of the Claude-Code-only `${CLAUDE_SKILL_DIR}`, so script calls work on every host.
- `/watch` is now derived from `SKILL.md` frontmatter; the separate `commands/watch.md` wrapper was dropped to avoid a duplicate slash command.
- `balanced` now full-decodes to detect every scene cut across the whole video. The previous early-exit was faster but kept only the first cuts and dropped the tail of long videos.
- `token-burner` is exempt from the long-video "sparse scan" warning, since it keeps every scene-change frame.
- `--max-frames` is now an override on top of each mode's default cap, rather than a fixed default of 80.

### Fixed
- Non-Claude installs (`npx skills add`) were dead on arrival — the installer copied `SKILL.md` without the `scripts/` it shells out to. The self-contained package layout resolves this.

### Removed
- `V2_PLAN.md` and `V2_CONCERNS.md` planning docs.

## [0.1.3] — 2026-05-09

### Fixed
- Windows: `video.info.json` is read as UTF-8 (#4). Previously `Path.read_text()` defaulted to cp1252 on Windows and crashed on yt-dlp's UTF-8 output, silently dropping Title/Uploader from the report. Same fix applied to `.env` reads/writes in `whisper.py` and `setup.py`.
- `download.py` now logs info.json parse failures to stderr instead of swallowing them.

### Security
- Hardened subprocess argv against option injection (#2): inserted `--` before the URL in the yt-dlp argv, and tightened `is_url` to reject `-`-prefixed sources and require a non-empty netloc. Resolved video/audio paths to absolute via `Path.resolve()` before passing to `ffmpeg`/`ffprobe`, so a relative path starting with `-` can't be misinterpreted as a flag.

## [0.1.2] — 2026-04-24

### Fixed
- Windows console crash: removed the emoji from the long-video warning in `watch.py`; cp1252 consoles couldn't encode it.
- `setup.py` now prints `winget` / `pip` install commands on Windows instead of "unsupported platform" — matches what the README already promised.

### Changed
- `SKILL.md` notes that on Windows the scripts must be invoked with `python`, not `python3` (the latter is the Microsoft Store stub on Windows).

## [0.1.1] — 2026-04-24

### Fixed
- Added `commands/watch.md` shim so `/watch` is callable when installed as a Claude Code plugin. Without it, the plugin loaded but the skill wasn't exposed as a slash command.
- `scripts/build-skill.sh` now strips `commands/` from the claude.ai `.skill` bundle alongside `hooks/` and `.claude-plugin/`.

## [0.1.0] — 2026-04-24

Initial marketplace release.

### Added
- `/watch <url-or-path> [question]` slash command.
- yt-dlp download with native caption extraction (manual + auto-subs).
- ffmpeg frame extraction with auto-scaled fps (≤2 fps, ≤100 frames, duration-aware budget).
- `--start` / `--end` focused mode with denser frame budget and transcript range filtering.
- Whisper fallback (Groq preferred, OpenAI secondary) for videos without captions.
- `setup.py` preflight: silent `--check`, structured `--json`, and installer that auto-runs `brew install` on macOS.
- Session-start hook that prints a one-line status on first run / partial config.
- `.skill` bundle packaging for claude.ai upload via `scripts/build-skill.sh`.
