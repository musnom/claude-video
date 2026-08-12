# Changelog

All notable changes to `/watch` are documented here.

## [0.5.0] — 2026-08-11

The close-the-gaps release: a full audit of the pipeline (recorded at commit `7db0aba`,
where the audit snapshots live in history) followed by fixes for everything it found.

### Added
- **YouTube download resilience.** yt-dlp's stderr is captured and *classified*: a login
  wall / age gate routes to `--cookies-from-browser` (with consent), a region lock says
  cookies won't help, and an HTTP 403 (SABR-style blocking) triggers exactly one automatic
  retry with the Android player client — media only; captions are already fetched. A stale
  yt-dlp (>90 days old by its CalVer version, no network call needed) warns with the
  platform's upgrade command before the download starts, since it is the most common cause
  of the whole failure class.
- **Playlist/channel URLs are refused** with the actual fix ("pass one video's URL")
  instead of downloading an arbitrary entry over the shared output file; the metadata pass
  is bounded to one entry so the refusal comes in seconds.
- **A cloud-transcription cost guard.** A captionless source over ~60 minutes of audio is
  refused *before* the encode, with the minute count and rough cost; `--transcribe-anyway`
  proceeds after the user agrees. Self-hosted endpoints are exempt. Relatedly, a 429 whose
  server-requested wait exceeds 60 s fails fast naming the wait instead of sleeping through
  a multi-hour quota window.
- **`--sub-langs` / `WATCH_SUB_LANGS`** override the caption request for regional variants
  (`en-CA`) and non-English tracks without an `-orig` variant — and the requested variant is
  *selected*, not merely fetched. The default (which measured better than upstream's `en.*`)
  is unchanged.
- **`--scene-threshold`** to tune cut detection (default 0.05; scene modes only), and
  **`--cookies` / `--cookies-from-browser`** (opt-in, never automatic) for login-walled
  videos — both landed after 0.4.0's entry was written.
- **Live broadcasts are refused before download** with a clear re-run-later message — and a
  `--match-filter` belt refuses them at download time even when the metadata fetch failed,
  so a live URL can no longer make the run record until the stream ends.
- **A trailing blank-frame filter**: a solid-black end card no longer spends an image slot;
  mid-video fades and dark scenes are untouched. Reported in the Frames line.
- **`setup.py --complete`** records a deliberate keyless setup, making "never nagged again"
  true without hand-editing the config; the first-run flow now offers the self-hosted
  `WATCH_WHISPER_ENDPOINT` alongside Groq/OpenAI, and declining transcription is a
  first-class option.
- **`references/setup.md`**: the full first-run/remediation flow, read only when the
  preflight says so — the always-read SKILL.md is ~50 lines shorter for it.
- **Focused Whisper uploads**: `--start`/`--end` runs encode and upload only the window's
  audio (measured 4689 kB → ~156 kB on a 10-minute clip with a 20 s window).
- **The Shots line**: full detected-cut pacing statistics (cuts/min, shot-length
  percentiles) computed independently of the frame cap, printed as the explicit lower
  bound it is — `at least N cuts (detected at scene threshold T)`.

### Fixed
- **`token-burner` can no longer cover less than `balanced`.** The uncapped mode gap-filled
  toward the duration target (40–80 frames on a 1–10-minute clip) while balanced filled
  toward its 100-frame cap; it now fills toward at least balanced's coverage. The guard
  test that masked this could not fail by construction and was rebuilt under production
  conditions.
- **Gap-fill no longer pads with near-duplicates.** Fills are checked against their
  neighbouring frames with the production dedup rule and filling stops when candidates stop
  being distinct. Measured on a 300 s static-shot clip: 100 frames (11 distinct, 89%
  duplicates) → the 11 distinct frames, with `fill stopped early: N near-duplicate
  candidates rejected` in the report. Fill decode failures are counted, not swallowed.
- **`--fps` is bounded and honest.** Non-positive values are rejected (they used to make
  the sampler select *every* decoded frame — ~18,000 JPEGs on a 10-minute clip); clamping
  above 2 fps prints both numbers; the report states the effective rate whenever the flag
  governed sampling.
- **`--end` past the end of the video is clamped with a note** instead of budgeting frames
  against a phantom duration (which silently made focused runs *sparser* than full runs);
  a bare `--end 0` is rejected instead of producing an empty report.
- **Timestamp fabrication is disclosed.** When ffmpeg reports fewer measured timestamps
  than frames, the extras are marked `estimated`, excluded from shot statistics, and the
  report says so — they no longer collapse silently onto the window start.
- **The motion token guard cannot be bypassed.** The cost estimate prints before every
  `--motion` extraction, and an absolute 600k-token ceiling holds even with an explicit
  `--max-frames` (2000 frames at the default 512px stays permitted; `--resolution 1998` on
  a 4K source — ~6M image tokens — is refused).
- **One `.env` truth.** Settings resolve as env → `~/.config/watch/.env` → `./.env` for
  *every* consumer (config, whisper, setup, and the session hook), so a project-local key
  no longer transcribes fine while `--check` reports `needs_key`. `export KEY=` lines
  parse; values containing `=` survive the hook; `SETUP_COMPLETE` is machine-config only,
  so a cloned repo cannot suppress another user's first run.
- **The session hook stops spamming**: no more `/watch: ready.` on every session for users
  whose key lives in the shell profile, self-hosted endpoints count as configured, and the
  remediation hint prints a real path instead of a literal `$CLAUDE_PLUGIN_ROOT`.
- **Packaging ships only runtime files.** The claude.ai `.skill` bundle no longer includes
  `build-skill.sh`/`.skillignore` (subtree archives resolve `export-ignore` relative to the
  subtree, so the repo-root rules never applied), and the plugin archive no longer ships
  dev planning docs. A release-level test pins both archives' contents.
- **Releases are gated**: the `v*` tag workflow runs the full suite on macOS before
  building and attaching `watch.skill`. Previously a tag published with zero verification.
- Cue-frame and gap-fill decode failures, per-engine caps/budgets, and the effective
  uniform rate are all reported; uniform-sampled frames carry measured timestamps
  (landed after 0.4.0's entry: `effective_cap`/`budget` reporting and measured uniform
  stamps).

### Changed
- **The skill description finally names what the skill can do** — motion/easing
  measurement, editing-style and pacing analysis, screen recordings, audio files,
  crop-for-legibility, transcript-only — so hosts can route those questions to `/watch`
  autonomously. The same fix lands across `plugin.json`, the Codex manifest, and the
  marketplace listing, plus their keywords.
- The `**Shots:**` count is quoted as a lower bound with its detection threshold, in the
  report and in both reference workflows.
- `references/motion.md` ends in an answer with code in chat: the skill's tool contract
  (`Bash, Read, AskUserQuestion`) deliberately excludes file editing, and applying
  recreation code to a project goes through the host's normal tools.
- Every ffmpeg invocation carries `-nostdin`, so a child can never eat the harness's stdin.
- README: attribution reworked (maintained by Mustafa Nomair; forked from
  bradautomates/claude-video by Brad Bonanno), stale pre-gap-fill claims replaced with the
  current engine behavior, and the new flags documented.

## [0.4.0] — 2026-08-11

### Added
- **`--motion`: frame-by-frame motion and animation-timing analysis.** Samples the source's *own* frames rather than resampling to a rate, labels each with its measured presentation time to the millisecond, and never dedups. Built for measuring how fast an animation moves, how long a transition takes, and recreating easing curves. Overrides `--detail`, so it cannot be a silent no-op the way `--fps` is.
- **`--crop x,y,w,h`** in source pixels, applied before scaling. A 160×120 component out of a 1920×1080 frame arrives at 1:1 rather than ~8% of the width, so its position is measurable — and it costs *fewer* tokens, not more (26 vs 262 on a measured example). Works in every mode.
- **`motion.json`**, written beside the frames: source dimensions and frame rate, crop rect, window, sampling stats, and a per-frame series of `{t, gap_ms, mean_delta, peak_delta}`. Deliberately stack-agnostic — the same measurements serve CSS keyframes, Framer Motion, GSAP or Tailwind, and which one is the caller's choice.
- **A measured motion envelope** in the report: first change, last change, duration, peak. Two change signals per frame, because a whole-frame average is nearly blind to the case that matters — a 120×60 element sliding on a 640×360 frame moved the mean by 2.71 on a 0–255 scale while the peak cell read 116.
- SKILL.md gains a *Measuring and recreating motion* section: the two-pass flow, how to read `motion.json`, how to get from a position/time series to an easing curve, and an explicit rule to emit for the user's stack only.

### Fixed
- **Densely sampled frames had wrong timestamps, not merely coarse ones.** Every frame label went through `format_time`, which rounds to the second. Eight cue frames at 50 ms spacing on a clip with a colour cut every 100 ms printed `00:00` then `00:01` seven times — putting the implied boundary a frame away from the real cut. Transcript-cue and motion frames now render `MM:SS.mmm`; scene, keyframe and uniform labels are unchanged.
- Cue timestamps are stored to 3 decimals rather than 2, so a 50 ms request grid survives intact.

### Changed
- `--fps`, `--no-dedup` and the "universal rate cap" are documented accurately. `--fps` reaches only the uniform-sampling fallback, so it is inert under `--detail efficient` and on any clip the scene engine handles; the 2 fps ceiling applies to automatic sampling, never to `--timestamps`. The docs previously pointed readers at a flag that silently does nothing.

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
