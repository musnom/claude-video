---
name: watch
version: "0.4.0"
description: Watch a video (URL or local path). Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, pulls the transcript from captions (or Whisper API fallback), and hands the result to Claude so it can answer questions about what's in the video.
argument-hint: "<video-url-or-path> [question]"
allowed-tools: Bash, Read, AskUserQuestion
homepage: https://github.com/mustafa-nom/claude-video
repository: https://github.com/mustafa-nom/claude-video
author: mustafa-nom
license: MIT
user-invocable: true
---

# /watch

You don't have a video input; this skill gives you one. A Python script gets captions first, optionally downloads the video, extracts frames as JPEGs (scene-aware, or fast keyframes at `efficient` detail), gets a timestamped transcript (native captions first, then Whisper API as fallback), and prints frame paths. You then `Read` each frame path to see the images and combine them with the transcript to answer the user.

Visual frame selection alone misses moments the speaker *points at* ("look here", "as you can see") because those are often low visual change. So the normal run is **two passes**: pass one gets frames + transcript, then you scan the transcript for those cues and make a second, cheap pass that grabs a frame at each one. Step 3 below is that pass — it is part of the default flow, not an optional extra.

## Resolve `SKILL_DIR` (do this before any command)

Every `python3 ...` command below runs a bundled script under `SKILL_DIR/scripts/`. Set `SKILL_DIR` to the **absolute path of the directory containing THIS SKILL.md you just Read** — your harness told you that path in the Read result. The scripts are always a direct sibling of this file (`SKILL_DIR/scripts/watch.py`), in every install layout:

```
Read ~/.claude/plugins/cache/claude-video/watch/<ver>/skills/watch/SKILL.md → SKILL_DIR=…/skills/watch
Read ~/.codex/skills/watch/SKILL.md                                          → SKILL_DIR=~/.codex/skills/watch
Read ~/.agents/skills/watch/SKILL.md                                         → SKILL_DIR=~/.agents/skills/watch
```

Substitute that literal path for `${SKILL_DIR}` in every command. This works on every harness (Claude Code, Codex, Cursor, Gemini CLI, …) without relying on any harness-specific environment variable. Guard once at the start of a run:

```bash
SKILL_DIR="<absolute path of the directory containing the SKILL.md you Read>"
if [ ! -f "$SKILL_DIR/scripts/watch.py" ]; then
  echo "ERROR: scripts/watch.py not found under SKILL_DIR=$SKILL_DIR" >&2
  echo "Re-check the directory of the SKILL.md you Read and substitute it as SKILL_DIR." >&2
  exit 1
fi
```

## Step 0 — Setup preflight (runs every `/watch` invocation, silent on success)

**Python interpreter:** every `python3 ...` command in this skill is for macOS/Linux. On **Windows**, substitute `python` — the `python3` command on Windows is the Microsoft Store stub and will not run the script.

On the first `/watch` invocation in a session, use structured preflight so you can detect first-run setup:

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --json
```

Branch on two fields:

- **`can_proceed: true` and `first_run: false`** → setup is already done (the user may have deliberately skipped a Whisper key — that's allowed). Proceed to Step 1 without comment.
- **`first_run: true`** → genuine first-time setup. Do these in order:
  1. If `missing_binaries` is non-empty, run the installer first (it auto-installs on macOS / prints commands elsewhere — see below) and confirm the binaries land. **Do not skip this and jump to preferences.**
  2. Run the installer once more if needed so it scaffolds `~/.config/watch/.env` (it only writes the template when the file is absent, so let it create the file *before* you write any values into it).
  3. Encourage a Whisper API key and ask the watch-preference questions below, then write the selected values into `~/.config/watch/.env` and set `SETUP_COMPLETE=true`.
- **`can_proceed: false` and `first_run: false`** → setup was finished before but the environment regressed (e.g. `missing_binaries` after an OS change). Run the installer to remediate, then proceed. Don't re-ask preferences.

A missing Whisper key is *encouraged to fix, not required*: on a genuine first run `status` will read `needs_key` even when binaries are present — that's your cue to encourage a key, not a blocker.

On follow-up `/watch` calls in the same session, use the silent check:

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --check
```

This is a <100ms lookup. Exit 0 means /watch can run — this **includes a user who finished setup without a Whisper key** (keyless is allowed). On exit 0 the script emits **nothing** — proceed to Step 1 without comment. **Do NOT announce "setup is complete" to the user** — they don't need a status message on every turn. The only acceptable user-visible output from Step 0 is when remediation is required.

On non-zero exit, follow the table:

| Exit | Meaning | Action |
|------|---------|--------|
| `2` | Missing binaries (`ffmpeg` / `ffprobe` / `yt-dlp`) | Run installer |
| `3` | Genuine first run with no Whisper API key | Run installer to scaffold `.env`, then encourage a key (the user may decline — proceed with `--no-whisper`) |
| `4` | Both missing | Run installer, then encourage a key |

Exit `3` only fires before the user has completed setup. Once `SETUP_COMPLETE=true` is written, a keyless install returns exit 0 and is never nagged again.

The installer is idempotent — safe to re-run:

```bash
python3 "${SKILL_DIR}/scripts/setup.py"
```

On macOS with Homebrew, it auto-installs `ffmpeg` and `yt-dlp`. On Linux/Windows, it prints the exact install commands for the user to run. It scaffolds `~/.config/watch/.env` with commented placeholders and default watch settings at `0600` perms.

**If an API key is still missing after install:** use `AskUserQuestion` to ask the user whether they have a Groq API key (preferred — cheaper, faster) or an OpenAI key. Then write it into `~/.config/watch/.env` — set the matching `GROQ_API_KEY=...` or `OPENAI_API_KEY=...` line. If they don't want to set up Whisper, proceed with `--no-whisper` and tell them videos without native captions will come back frames-only.

**First-run watch preference:** after the installer has scaffolded `~/.config/watch/.env`, use `AskUserQuestion` to ask one question:

- Default detail (one dial). Present these as `AskUserQuestion` options in this exact order — lightest to heaviest — and keep `(recommended)` on `balanced` even though it is not first (do **not** reorder to put the recommended option first):
  - `transcript` — no frames at all, transcript only (skips video download when captions exist).
  - `efficient` — fast keyframe pass (cap 50).
  - `balanced` (recommended) — scene-aware frames (cap 100, default).
  - `token-burner` — scene-aware, uncapped (maximum fidelity; high token cost).

Write the answer directly into `~/.config/watch/.env` by setting the bare key on its own line — **no trailing inline comment** (a `# note` after the value can break parsing):

```bash
WATCH_DETAIL=balanced
```

Use the user's selected value. If they skip the question, keep the recommended default. Once dependencies, the API-key choice, and this preference are handled, write or update `SETUP_COMPLETE=true` in the same file. Do not ask this preference question again when `SETUP_COMPLETE=true`.

**Structured mode (optional):** `python3 "${SKILL_DIR}/scripts/setup.py" --json` emits `{status, can_proceed, first_run, setup_complete, missing_binaries, whisper_backend, has_api_key, config_file, watch_detail, platform}` where `status` is one of `ready | needs_install | needs_key | needs_install_and_key`. `status` describes the *ideal* state (a key is encouraged, so a keyless first run reads `needs_key`); `can_proceed` is the operational gate (binaries present AND a key is set OR setup was already completed). Branch on `can_proceed`/`first_run` to decide whether to run; use `status` to decide what to encourage.

Within a single session, you can skip Step 0 on follow-up `/watch` calls — once `--check` returned 0, nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User types `/watch <url-or-path> [question]`.
- User asks about **motion or timing** — "how fast does this move", "how long is this transition", "what easing is this", "recreate this animation", "match this motion style". Use `--motion`; see the section below.

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Auto-mode rate cap: 2 fps.** The duration budget never samples faster than 2 fps, and `--fps` cannot raise it. This is a cap on *automatic* sampling — `--timestamps` grabs frames at whatever exact moments you name, with no rate limit.
- **The frame ceiling is set by the detail mode** (`WATCH_DETAIL` in `~/.config/watch/.env`, or `--detail`), not a single global cap:
  - `transcript` → no frames
  - `efficient` → up to **50** (keyframes)
  - `balanced` (default) → up to **100** (scene-aware)
  - `token-burner` → **uncapped** (scene-aware; a soft warning prints past 250 frames)
  - `--max-frames N` overrides whichever cap the mode would otherwise use.
- **Full-video frame budget by duration.** Token cost grows with frame count, so the script targets a budget by duration. This budget sets the fps and the uniform-sampling fallback; scene-aware selection can fill up to the detail cap above, whichever is lower:
  - ≤30s → ~12-30 frames
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → up to the detail cap, sparsely spaced (warning printed)
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a sparse scan.

## How to invoke

**Step 1 — parse the user input.** Separate the video source (URL or path) from any question the user asked. Example: `/watch https://youtu.be/abc what language is this in?` → source = `https://youtu.be/abc`, question = `what language is this in?`.

**Step 2 — run the watch script.** Pass the source verbatim. Do not shell-escape it yourself beyond normal quoting:

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<source>"
```

Optional flags:
- `--detail transcript|efficient|balanced|token-burner` — fidelity/speed dial. `transcript` = no frames (transcript only, skips video download when captions exist); `efficient` = fast keyframes (cap 50); `balanced` = scene-aware frames (cap 100); `token-burner` = scene-aware, uncapped.
- `--start T` / `--end T` — focus on a section. Accepts `SS`, `MM:SS`, or `HH:MM:SS`. When either is set, fps auto-scales denser (see "Focusing on a section" below).
- `--timestamps T1,T2,…` — grab a frame at each of these absolute timestamps (`SS`, `MM:SS`, or `HH:MM:SS`). This is what Step 3 uses to capture deictic moments the presenter flags ("look here", "as you can see", "notice this") that visual selection alone misses. See "Transcript-cue frames" below for the mechanics.
- `--max-frames N` — override the preset cap for tighter token budget (e.g. `--max-frames 40`)
- `--resolution W` — change frame width in px (default 512; bump to 1024 only if the user needs to read on-screen text)
- `--fps F` — nudge the uniform sampler's rate (clamped to 2 fps max). **Narrow in scope:** it only reaches the uniform-sampling fallback, so it does nothing under `--detail efficient` or on any clip the scene engine handles. It is not the way to measure motion or animation timing.
- `--out-dir DIR` — keep working files somewhere specific (default: an auto-generated tmp dir)
- `--whisper groq|openai|custom` — force a specific Whisper backend (default: a self-hosted endpoint if `WATCH_WHISPER_ENDPOINT` is set, else Groq, else OpenAI)
- `--no-whisper` — disable the Whisper fallback entirely (frames-only if no captions)
- `--motion` — frame-by-frame motion/timing analysis. Samples the source's own frames (no resampling), labels each to the millisecond, never dedups, overrides `--detail`. See "Measuring and recreating motion" below.
- `--crop x,y,w,h` — crop to a region in **source** pixels before scaling. Makes a small UI component fill the frame so its position is measurable, and costs fewer tokens. Works in every mode.
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the previous kept one (held slides, static screen recordings, paused video) so the frame budget goes to distinct content; the report's **Frames** line notes how many were dropped. Pass this only if the user needs every sampled frame (e.g. judging subtle frame-to-frame motion).

## Measuring and recreating motion (`--motion`)

Reach for this when the question is about **timing or movement** rather than content:
"how fast does this move", "how long is that transition", "is this animation janky", "what easing is
this", "recreate this animation", "match this motion style".

`--motion` samples the source's **own** frames — no resampling — and labels each with its measured
presentation time to the millisecond. It never dedups, and it overrides `--detail`.

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<work-dir>/download/video.mp4" \
  --motion --start 0:12 --end 0:14 --crop 320,180,400,120 --no-whisper
```

- **Point it at the downloaded file**, not the URL, on a second pass — the work dir is printed at the
  bottom of the first report.
- **Keep the window tight.** One transition, not the scene containing it. Every source frame in the
  window is extracted.
- **`--crop x,y,w,h` in source pixels is the single biggest accuracy win** for a UI component. A
  160×120 button cropped out of a 1920×1080 frame arrives at 1:1 instead of ~8% of the width, so its
  position is actually measurable — and it costs *fewer* tokens, not more.
- **Do not reach for `--fps`.** It is capped at 2 fps, reaches only the uniform sampler, and its frames
  go through dedup. It cannot do this.

### What you get back

Frame labels carry milliseconds (`t=00:12.317`), and the report states the envelope — first change,
last change, duration, peak. Alongside the frames the script writes **`motion.json`** in the work dir:
source dimensions and frame rate, the crop rect, the window, sampling stats, and a per-frame series of
`{t, gap_ms, mean_delta, peak_delta}`.

`peak_delta` is the one to trust for a small element on a static background — a whole-frame average
barely registers a button sliding. Use it to find where motion starts and stops; use the frames
themselves to see *what* moved and *how far*.

### Turning that into animation code

1. **Read `motion.json`** for the envelope. That is your duration.
2. **Read the frames** and track the moving element's position across them. Frame timestamps are
   absolute source time, so position-vs-time comes straight out.
3. **Fit the easing** from the shape of that curve — even spacing is linear, front-loaded is ease-out,
   an overshoot-and-settle is a spring. Say which you concluded and why.
4. **Emit for the user's stack, and only theirs.** Ask or infer whether they want CSS keyframes,
   Framer Motion, GSAP, Tailwind, or something else — never assume one. `motion.json` is deliberately
   stack-agnostic so the same measurements serve any of them.
5. **Always state the measured numbers** (duration in ms, start and end positions in source pixels,
   the source dimensions) next to the generated code, so the user can check your work rather than
   trust it.

Convert pixels to layout units using the **source** dimensions from the report, not the frame
dimensions — the frames are scaled and, when cropped, offset by the crop origin.

### Getting a good recording to measure

If the user is capturing the clip themselves: record at 2×/retina so small movements survive; leave a
beat of stillness before and after the animation so its start and end are unambiguous; and keep the
clip short.

### Focusing on a section (higher frame rate)

When the user asks about a specific moment — "what happens at the 2 minute mark?", "zoom into 0:45 to 1:00", "the first 10 seconds" — pass `--start` and/or `--end`. The script switches to focused-mode budgets, which are denser than full-video budgets (still capped at 2 fps, and still bounded by the detail-mode cap — the counts below assume the default `balanced` cap of 100; `efficient` tops out at 50):

- ≤5s → 2 fps (up to 10 frames)
- 5-15s → 2 fps (up to 30 frames)
- 15-30s → ~2 fps (up to 60 frames)
- 30-60s → ~1.3 fps (up to 80 frames)
- 60-180s → ~0.6 fps (100 frames, capped)

Focused mode is the right call for:
- Any moment/range the user names explicitly ("around 2:30", "the intro", "the last 30 seconds").
- Any video longer than ~10 minutes where the user's question is about a specific part — running focused on the relevant section is far more useful than a sparse scan of the whole thing.
- Re-runs after a full scan didn't have enough detail in some region.

Transcript is auto-filtered to the same range. Frame timestamps are absolute (real video timeline, not offset-from-start).

Examples:
```bash
# Last 10 seconds of a 1 minute video
python3 "${SKILL_DIR}/scripts/watch.py" video.mp4 --start 50 --end 60

# Zoom into 2:15 → 2:45 at 2 fps (60 frames)
python3 "${SKILL_DIR}/scripts/watch.py" "$URL" --start 2:15 --end 2:45 --fps 2

# From 1h12m to the end of the video
python3 "${SKILL_DIR}/scripts/watch.py" "$URL" --start 1:12:00
```

**Step 3 — transcript-cue pass (do this by default).** Pass one gave you a timestamped transcript. Read it now, *before* reading the frames, and scan for **deictic cues** — moments where the speaker directs attention at something on screen: "look here", "as you can see", "notice this", "watch what happens", "right here", "this bit". These are exactly the frames scene selection misses, because pointing at a slide barely changes the picture.

This is a judgment call, which is why you do it and not a regex — ignore rhetorical uses ("look, the point is…") and cues that land on a frame you already have.

If you find any, make a second **cue-only** pass. It extracts *just* those frames and nothing else, so it is cheap:

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<work-dir>/download/video.mp4" \
  --detail transcript --timestamps 4:32,7:10,9:55 --no-whisper
```

- **Point it at the downloaded file, not the URL** — `<work-dir>` is the directory the pass-one report printed at the bottom; the video lands at `download/video.*` inside it. Re-passing the URL would download the whole thing again.
- **`--detail transcript` makes it cue-only** — it skips scene/keyframe sampling entirely and returns only your timestamps, so you get no duplicate frames from pass one.
- **`--no-whisper`** — you already have the transcript; this stops a redundant transcription attempt.
- Keep it to roughly the **10 strongest cues**. Every cue frame is a real token cost, and they are pinned against the frame cap.
- Timestamps are absolute source times, in the same coordinates the transcript uses.

**Skip this step when** any of these hold — say nothing about skipping, just move on:
- The transcript came back "none available" (nothing to scan).
- The user explicitly asked for `--detail transcript` (they asked for no frames; don't hand them frames anyway).
- You scanned and found no genuine deictic cues.
- The user asked a narrow question that the pass-one frames already answer.

**Step 4 — Read every frame path the script lists.** The Read tool renders JPEGs directly as images for you. Read all frames — from both passes — in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS` timestamp so you can align them to the transcript; cue frames are marked `reason=transcript-cue`.

**Step 5 — answer the user.** You now have two streams of evidence:
- **Frames** — what's on screen at each timestamp
- **Transcript** — what's said at each timestamp. The report's header shows the source (`captions` = yt-dlp pulled native subs; `whisper (groq)`, `whisper (openai)` or `whisper (custom)` = transcribed by an API, where `custom` is a self-hosted server).

If the user asked a specific question, answer it directly citing timestamps. If they didn't ask anything, summarize what happens in the video — structure, key moments, notable visuals, spoken content.

This holds for `transcript` detail too: even with no frames, produce a **summary** like the other modes — do not paste the full transcript into chat. Synthesize structure, key moments, and spoken content with timestamps; quote only the lines that matter. Offer the raw transcript only if the user explicitly asks for it.

**Step 6 — clean up.** Each pass prints a working directory at the end. If the user isn't going to ask follow-ups about this video, delete them with `rm -rf <dir>`. If they might, leave them in place.

## Detail and frames

Default behavior comes from `~/.config/watch/.env`:

- `WATCH_DETAIL=transcript|efficient|balanced|token-burner` (default: `balanced`)

At `transcript` detail, captions are enough to return a report without downloading video. If captions are missing, the script downloads audio only and tries Whisper. If no transcript can be produced, it reports the limitation clearly; re-run with `--detail balanced` for frames.

At `efficient` detail, the script downloads the video and extracts **keyframes only** (`ffmpeg -skip_frame nokey`) — a near-instant pass that lands frames on scene cuts. If a clip has fewer than 4 keyframes it falls back to uniform sampling.

At `balanced` / `token-burner` detail, the script extracts **scene-aware** frames: ffmpeg scene-change selection first, falling back to uniform sampling only when the video is effectively static. `balanced` caps at 100 frames; `token-burner` is uncapped. Frame report lines include both timestamp and selection reason. Extracted images are clamped to a maximum 1998px height for Claude Read compatibility.

## Transcript-cue frames

Reference for the mechanics behind **Step 3** — which runs by default on every watch that produces a transcript, not only when the user asks for it.

Visual frame selection (scene/keyframe) misses the moments a presenter explicitly flags — "look here", "as you can see", "notice this", "watch what happens" — because pointing at a slide is often a *low* visual change. `--timestamps` forces a frame at those exact moments. **You** decide which moments matter, by reading the transcript; see Step 3 for the flow and the skip conditions.

Behavior:
- **Additive by default.** Cue frames (`reason=transcript-cue`) are merged into whatever `--detail` already selected, in chronological order.
- **Pinned and counted first.** Cue frames are reserved against the frame cap before the detail engine runs, so they're never evicted by even-sampling.
- **Honors focus mode.** With `--start/--end`, any cue timestamp outside the window is dropped (reported in the summary). Coordinates are always absolute source time.
- **Cue-only frames.** `--detail transcript --timestamps …` skips scene/keyframe sampling and returns *only* the cue frames (it will download the video to do so, since frames need pixels).

## Transcription

The script gets a timestamped transcript in one of two ways:

1. **Native captions (free, preferred).** yt-dlp pulls manual or auto-generated subtitles from the source platform if available.
2. **Whisper fallback.** If no captions came back (or the source is a local file), the script extracts audio (`ffmpeg -vn -ac 1 -ar 16000 -b:a 64k`, ~0.5 MB/min) and uploads it to whichever backend is configured:
   - **Self-hosted** — any OpenAI-compatible `/v1/audio/transcriptions` server (whisper.cpp's `whisper-server`, speaches, LocalAI, vLLM). Set `WATCH_WHISPER_ENDPOINT` to its URL. **No API key and no data leaves the machine.** Takes priority when set, since pointing this at localhost is a deliberate act. Model defaults to `whisper-1`; override with `WATCH_WHISPER_MODEL`.
   - **Groq** — `whisper-large-v3`. Preferred cloud default: cheaper, faster. Get a key at console.groq.com/keys.
   - **OpenAI** — `whisper-1`. Cloud fallback. Get a key at platform.openai.com/api-keys.

All three settings live in `~/.config/watch/.env`. Override the automatic choice with `--whisper groq|openai|custom`, or use `--no-whisper` to skip the fallback entirely.

**If the user wants local-only transcription**, point them at `WATCH_WHISPER_ENDPOINT` rather than suggesting a Python package — the existing client already speaks the protocol every local Whisper server exposes, so nothing needs installing beyond the server itself.

## Failure modes and handling

- **Setup preflight failed** → run `python3 "${SKILL_DIR}/scripts/setup.py"` (auto-installs ffmpeg/yt-dlp via brew on macOS, scaffolds the `.env`). For API key, ask the user via `AskUserQuestion` and write it to `~/.config/watch/.env`.
- **No transcript available** → captions missing AND (no Whisper key OR Whisper API failed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → yt-dlp's error goes to stderr. If it's a login-required or region-locked video, tell the user plainly; do not keep retrying.
- **Whisper request fails** → the error is printed to stderr (likely: invalid key or rate limit). Audio over the API's 25 MB upload cap is split into chunks and transcribed automatically, so length alone won't fail it; if some chunks fail the transcript is partial and the dropped chunks are noted on stderr. The report will say "none available" only if every chunk fails. You can retry with `--whisper openai` if Groq failed (or vice versa).

## Token efficiency

This skill burns tokens primarily on frames. Order of magnitude:
- 80 frames at 512px wide is roughly 50-80k image tokens depending on aspect ratio.
- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- Bumping `--resolution` to 1024 roughly quadruples the image tokens per frame. Only do it when necessary.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when Whisper is needed, a mono 16 kHz audio clip
- Sends the extracted audio clip to the URL in `WATCH_WHISPER_ENDPOINT` when that is set — and to nowhere else. This is the user's own server; if it is on localhost, no audio leaves the machine. No `Authorization` header is sent unless a key is configured
- Otherwise sends the extracted audio clip to Groq's Whisper API (`api.groq.com/openai/v1/audio/transcriptions`) when `GROQ_API_KEY` is set (preferred — cheaper, faster)
- Otherwise sends the extracted audio clip to OpenAI's audio transcription API (`api.openai.com/v1/audio/transcriptions`) when `OPENAI_API_KEY` is set, or when `--whisper openai` is forced
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory under the system temp dir (or `--out-dir` if specified) so Claude can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store the Whisper API key(s), an optional self-hosted endpoint, and a `SETUP_COMPLETE` marker. As a fallback, also reads `.env` in the current working directory

**What this skill does NOT do:**
- Does not upload the video itself to any API — only the extracted audio goes out, and only when native captions are missing AND Whisper is not disabled with `--no-whisper`
- Does not access any platform account (no login, no session cookies, no posting) — yt-dlp only ever requests public data
- Does not share API keys between providers (Groq key only goes to `api.groq.com`, OpenAI key only goes to `api.openai.com`, and a self-hosted endpoint is sent no key at all)
- Does not contact any third party when `WATCH_WHISPER_ENDPOINT` is set — that setting replaces the cloud backends rather than supplementing them
- Does not log, cache, or write API keys to stdout, stderr, or output files
- Does not persist anything outside the working directory and `~/.config/watch/.env` — clean up the working directory when you're done (Step 6)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (caption selection + Whisper orchestration), `scripts/whisper.py` (Groq / OpenAI clients), `scripts/setup.py` (preflight + installer)

Review scripts before first use to verify behavior.
