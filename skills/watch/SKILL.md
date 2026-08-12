---
name: watch
version: "0.5.0"
description: Watch and analyze a video, screen recording, or audio file — from a URL (YouTube, Vimeo, TikTok, X, Twitch, anything yt-dlp supports) or a local file. Downloads with yt-dlp, extracts auto-scaled frames with ffmpeg, and pulls a timestamped transcript from native captions or the Whisper API, so answers come from what is actually on screen and in the audio. Also measures motion and animation timing — transition durations and easing curves via --motion — characterizes editing style and pacing (cuts per minute, shot lengths), crops regions so small on-screen text stays legible, and has a transcript-only mode. Use it when the user shares a video URL or file and asks what happens in it, wants a summary, is debugging a screen recording, asks how fast something moves or what easing an animation uses, wants an editing style characterized or matched, or needs an audio file transcribed.
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

## Step 0 — Setup preflight (silent on success)

**Windows:** substitute `python` for `python3` in every command in this skill (`python3` on Windows is the Microsoft Store stub and will not run the scripts).

On the **first `/watch` of a session**, run the structured preflight:

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --json
```

If `can_proceed` is `true` and `first_run` is `false`, setup is already done (the user may have deliberately skipped transcription — that's allowed) — proceed to Step 1 without comment. Anything else — `first_run: true` or `can_proceed: false` — read **`${SKILL_DIR}/references/setup.md`** and follow the first-run / remediation flow there before proceeding.

On **follow-up `/watch` calls** in the same session, use the silent check (<100ms):

```bash
python3 "${SKILL_DIR}/scripts/setup.py" --check
```

Exit 0 emits nothing — proceed without comment, and do **NOT** announce "setup is complete"; the only acceptable user-visible output from Step 0 is when remediation is required. On a non-zero exit, read `references/setup.md` (the exit codes and their fixes live there). Once `--check` has returned 0 in a session you can skip Step 0 entirely on later calls — nothing about the environment changes between turns.

## When to use

- User pastes a video URL (YouTube, Vimeo, X, TikTok, Twitch clip, most yt-dlp-supported sites) and asks about it.
- User points at a local video file (`.mp4`, `.mov`, `.mkv`, `.webm`, etc.) and asks about it.
- User types `/watch <url-or-path> [question]`.
- User asks about **motion or timing** — "how fast does this move", "how long is this transition", "what easing is this", "recreate this animation", "match this motion style". Use `--motion` and read **`references/motion.md`** first.
- User asks about **editing style, pacing or motion graphics** — "characterize this reference", "match this editing style", "how fast does this cut", "how is this built". Read **`references/editing-style.md`** first; the default flags are actively wrong for this question.

## Deeper references (read on demand, not up front)

Two workflows live outside this file because they need real detail and most runs do not touch them. Read the whole file before running anything in it — they are short.

| File | Read it when |
|---|---|
| `references/motion.md` | The question is about timing, movement, easing, or recreating an animation (`--motion`). |
| `references/editing-style.md` | The question is about cutting rhythm, pacing, motion-graphics construction, or on-screen typography. |
| `references/setup.md` | Step 0's preflight reported `first_run: true` or a non-zero `--check` exit. |

Resolve them the same way as the scripts: `${SKILL_DIR}/references/<name>.md`.

## Recommended limits

- **Best accuracy: videos under 10 minutes.** Frame coverage scales inversely with duration.
- **Auto-mode rate cap: 2 fps.** The duration budget never samples faster than 2 fps, and `--fps` cannot raise it (asking for more prints a clamp notice and samples at 2). This is a cap on *automatic* sampling — `--timestamps` grabs frames at whatever exact moments you name, with no rate limit between them, though cue frames still count against the detail-mode frame cap (they are reserved out of it first; see Step 3).
- **The frame ceiling is set by the detail mode** (`WATCH_DETAIL` in `~/.config/watch/.env`, or `--detail`), not a single global cap:
  - `transcript` → no frames
  - `efficient` → up to **50** (keyframes)
  - `balanced` (default) → up to **100** (scene-aware)
  - `token-burner` → **uncapped** (scene-aware; a soft warning prints past 250 frames)
  - `--max-frames N` overrides whichever cap the mode would otherwise use.
- **Full-video frame budget by duration.** This budget sets the fps for the *uniform-sampling fallback* — the path taken only when a clip has too few detectable shots:
  - ≤30s → ~12-30 frames
  - 30s-1min → ~40 frames
  - 1-3min → ~60 frames
  - 3-10min → ~80 frames
  - \>10min → up to the detail cap (warning printed)
- **The scene engine spends the cap on distinct content.** It takes one frame per detected cut, then fills the widest remaining time gaps — but each fill is checked against its neighbouring frames with the same rule dedup uses, and filling stops when the remaining candidates are near-duplicates. So a long video with few cuts but *changing* content (screen recordings, slow pans) comes back near the cap, while a long video of genuinely static shots comes back with roughly its detected shots and the **Frames** line says `fill stopped early: N near-duplicate candidates rejected`. Expect `balanced` to return close to 100 frames on inputs whose long shots actually change; a much smaller count with that note is the tool saving your tokens, not missing content.
- If the user hands you a long video, consider asking whether they want a specific section before burning tokens on a full scan.

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
- `--transcribe-anyway` — proceed with a cloud Whisper upload past the ~60-minute duration guard. When a run is refused by that guard, relay the printed estimate to the user with `AskUserQuestion` and re-run with this flag only on a yes — it is their API bill.
- `--sub-langs LIST` — override the caption languages requested from the source (also settable as `WATCH_SUB_LANGS` in the config). Use when a video's only track is a regional variant (`en-CA`, `en-IN`) or a non-English language with no `-orig` track. Prefer exact codes: broad regexes fan out across YouTube's dozens of auto-translated tracks and collect rate-limit rejections.
- `--motion` — frame-by-frame motion/timing analysis. Samples the source's own frames (no resampling), labels each to the millisecond, never dedups, overrides `--detail`. **Read `references/motion.md` before using it.**
- `--crop x,y,w,h` — crop to a region in **source** pixels before scaling. Two uses, both large: it makes a small UI component fill the frame so its position is measurable, and it is the only way to make small on-screen **text legible** — a 512px frame scales a 1920px source by 3.75×, so 16px body text arrives 4.3px tall, below the ~8–10px floor where glyphs read at all. Costs fewer tokens, not more. Works in every mode.
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the previous kept one (held slides, static screen recordings, paused video) so the frame budget goes to distinct content; the report's **Frames** line notes how many were dropped. Pass this when the user needs every sampled frame — judging subtle frame-to-frame motion, or measuring cutting rhythm.
- `--scene-threshold T` — how different two frames must be to count as a cut, 0–1 (default 0.05). Lower finds more cuts. Only affects `--detail balanced` / `token-burner`; the script says so on stderr if you pass it anywhere else. The default already catches motion-graphics cuts (which score ~0.05–0.10, against 0.8+ for a camera cut); reach for `0.03` only when a piece you can see cutting reports "too few shots".
- `--cookies-from-browser BROWSER[:PROFILE]` — read cookies from a local browser so yt-dlp can reach a login-walled, age-gated or members-only video (e.g. `chrome`, `firefox:default`). **Opt-in: nothing is read unless the user asks for it.** Suggest it when a download fails with a login/403 error; don't use it speculatively.
- `--cookies FILE` — same purpose, from an already-exported Netscape-format cookie file.

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
python3 "${SKILL_DIR}/scripts/watch.py" "<video-file-from-the-report-footer>" \
  --detail transcript --timestamps 4:32,7:10,9:55 --no-whisper
```

- **Point it at the downloaded file, not the URL.** Pass one's report ends with `_Video file: `/path/to/video.webm`_` — copy that path verbatim. **Do not construct it**: yt-dlp names the file after whatever the site served, so `.webm` and `.mkv` are as likely as `.mp4`, and a guessed `download/video.mp4` fails on exactly those. Re-passing the URL would download the whole thing again.
- **`--detail transcript` makes it cue-only** — it skips scene/keyframe sampling entirely and returns only your timestamps, so you get no duplicate frames from pass one.
- **`--no-whisper`** — you already have the transcript; this stops a redundant transcription attempt.
- Keep it to roughly the **10 strongest cues**. Every cue frame is a real token cost, and they are pinned against the frame cap.
- Timestamps are absolute source times, in the same coordinates the transcript uses.

**Skip this step when** any of these hold — say nothing about skipping, just move on:
- The transcript came back "none available" (nothing to scan).
- The user explicitly asked for `--detail transcript` (they asked for no frames; don't hand them frames anyway).
- You scanned and found no genuine deictic cues.
- The user asked a narrow question that the pass-one frames already answer.

**Step 4 — Read every frame path the script lists.** The Read tool renders JPEGs directly as images for you. Read all frames — from both passes — in a single message (parallel tool calls) so you see them together. The frames are in chronological order with a `t=MM:SS.mmm` timestamp so you can align them to the transcript; the `reason=` on each says how it was chosen (see "Reading the report's numbers" below — `gap-fill` frames are not cuts).

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

### Reading the report's numbers

Every frame line is `t=MM:SS.mmm` — millisecond precision, because a clip that cuts more than once a second cannot be described in whole seconds. Scene, keyframe, uniform and motion frames all carry ffmpeg's *measured* presentation time, so the label is where the pixels are rather than where the sampler asked. Cue and gap-fill frames carry the time that was requested; the decoder returns the nearest frame at or after it.

`reason=` tells you where a frame came from, and they are not interchangeable:

| reason | meaning |
|---|---|
| `first-frame` / `scene-change` | a detected cut — these are the shot boundaries |
| `gap-fill` | placed by the tool to cover a long stretch with no cut, not a cut itself |
| `keyframe` | an I-frame (`--detail efficient`); approximates cuts, but encoders also emit them on a timer |
| `uniform` | fixed-rate sampling, used when too few shots were detected |
| `transcript-cue` | a timestamp you asked for with `--timestamps` |
| `motion` | `--motion`; every source frame in the window |

**`- **Shots:** …`** appears whenever scene detection ran. It is computed from the *full* detected-cut list, independently of which frames survived the cap — so it is the number to quote for pacing. Do **not** compute cutting rate from the gaps between the frame paths; those describe the sampling, not the video.

The count is a **lower bound** — detection misses cuts below the scene threshold (measured: −16% on ordinary footage, −65% on low-contrast material) — which is why the line reads `at least N cuts (detected at scene threshold T)`. Quote it that way: "at least N cuts, R/min at the detection threshold". If the frames visibly cut faster than the number implies, re-run with `--scene-threshold 0.03`.

If a `Counted before near-duplicate removal` note follows it, some of those cuts were between visually similar shots.

A frame marked `estimated` carries a requested time, not a measured one (its measured timestamp was unavailable); such frames are excluded from the Shots statistics automatically.

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

- **Setup preflight failed** → read `references/setup.md` and follow its flow (installer, transcription question, preferences).
- **No transcript available** → captions missing AND (no Whisper key OR Whisper API failed). Script prints a hint pointing to setup. Proceed frames-only and tell the user.
- **Long video warning printed** → acknowledge it in your answer. Offer to re-run focused on a specific section via `--start`/`--end` rather than a sparse full-video scan.
- **Download fails** → the script *classifies* the failure on stderr — follow its message. **Login-walled / age-gated / members-only** → offer `--cookies-from-browser chrome` (or the user's browser); do not pass it without asking, and do not keep retrying. **Region-locked** → say so; cookies will not help. **HTTP 403 / SABR** → a retry with the Android player client already ran automatically; if the message names a stale yt-dlp, relay its upgrade command — that is the most common fix.
- **"yt-dlp ... is N days old" warning** → an outdated yt-dlp is the top cause of YouTube failures. Relay the printed upgrade command if the download then fails.
- **"This URL is a playlist or channel"** → refused: /watch reads one video at a time. Ask the user which video from it they mean and re-run with that URL.
- **"This URL is a live broadcast"** → refused before downloading, because yt-dlp would record until the stream ends. Tell the user to re-run once it has finished; the same URL then resolves to a normal recording. (When the metadata fetch failed, a stderr note says the pre-check was blind — the download itself still refuses an ongoing broadcast.)
- **`--motion` refused over a token ceiling** → the window is too wide for frame-by-frame sampling. Follow the message: narrow `--start`/`--end`, add `--crop`, or pass `--max-frames` deliberately. See `references/motion.md`.
- **"no video stream"** → an audio-only file. The run still succeeds and returns a transcript; say that frames were not possible rather than treating it as an error.
- **"is a still image"** → someone pointed /watch at a `.png`/`.jpg`. Read the file directly with the Read tool instead.
- **"Whisper upload guard"** → the source exceeds ~60 minutes of audio, which is a real API bill. Relay the printed estimate to the user with `AskUserQuestion`; on a yes re-run with `--transcribe-anyway`, or offer `--start`/`--end` to transcribe just a section. Self-hosted endpoints are exempt.
- **Whisper request fails** → the error is printed to stderr (likely: invalid key or rate limit). Audio over the upload limit is split into ~24 MB chunks (the APIs cap uploads at 25 MB; the margin leaves multipart headroom) and transcribed automatically, so length alone won't fail it; if some chunks fail the transcript is partial and the dropped chunks are noted on stderr. The report will say "none available" only if every chunk fails. A 429 whose server-requested wait exceeds 60 s fails fast naming the wait — that is quota exhaustion; switch backends (`--whisper openai` if Groq failed, or vice versa) or re-run later.

## Token efficiency

This skill burns tokens primarily on frames. The cost of one frame is **(width × height) / 750** image tokens, using the frame's own pixel size — which is `--resolution` wide (default 512) with the height following the source aspect.

At the default 512px:

| aspect | frame size | per frame | 80 frames |
|---|---|---|---|
| 16:9 | 512×288 | 196 | **~16k** |
| 4:3 | 512×384 | 262 | **~21k** |
| 1:1 | 512×512 | 349 | **~28k** |

- The transcript is cheap (a few thousand tokens at most for a 10-minute video).
- `--resolution 1024` roughly quadruples the per-frame cost (~786 tokens at 16:9). Worth it to read on-screen text; pair it with `--start`/`--end` or `--max-frames`.
- `--crop` **reduces** cost — the cropped region is delivered at up to 1:1 instead of being scaled into a full-size frame.

Compute the number rather than guessing it. A second pass costs what its frames cost, and a cheap second pass is usually the right move — Step 3 below depends on that being true.

If you already watched a video this session and the user asks a follow-up, do **not** re-run the script — you already have the frames and transcript in context. Just answer from what you have.

## Security & Permissions

**What this skill does:**
- Runs `yt-dlp` locally to download the video and pull native captions when the source supports them (public data; the request goes directly to whatever host the URL points at)
- Runs `ffmpeg` / `ffprobe` locally to extract frames as JPEGs and, when Whisper is needed, a mono 16 kHz audio clip
- Sends the extracted audio clip to the URL in `WATCH_WHISPER_ENDPOINT` when that is set — and to nowhere else. This is the user's own server; if it is on localhost, no audio leaves the machine. No `Authorization` header is sent unless a key is configured
- Otherwise sends the extracted audio clip to Groq's Whisper API (`api.groq.com/openai/v1/audio/transcriptions`) when `GROQ_API_KEY` is set (preferred — cheaper, faster)
- Otherwise sends the extracted audio clip to OpenAI's audio transcription API (`api.openai.com/v1/audio/transcriptions`) when `OPENAI_API_KEY` is set, or when `--whisper openai` is forced
- Writes the downloaded video, frames, audio, and an intermediate transcript to a working directory under the system temp dir (or `--out-dir` if specified) so Claude can `Read` them
- Reads / creates `~/.config/watch/.env` (mode `0600`) to store the Whisper API key(s), an optional self-hosted endpoint, and a `SETUP_COMPLETE` marker. As a fallback, every setting is also read from `.env` in the current working directory — except `SETUP_COMPLETE`, which is machine-config only so a cloned repo cannot mark another user's setup complete

**What this skill does NOT do:**
- Does not upload the video itself to any API — only the extracted audio goes out, and only when native captions are missing AND Whisper is not disabled with `--no-whisper`
- Does not post, comment, or change anything on any platform — every request is a read
- Does not touch your browser profile or any cookie jar **unless you pass `--cookies-from-browser` or `--cookies`**, which are off by default and are never enabled automatically, including after a failed download. When you do pass one, yt-dlp reads the cookies transiently to authenticate that request; they are not written to the working directory, not logged, and not sent anywhere but the host the video URL points at
- Does not share API keys between providers (Groq key only goes to `api.groq.com`, OpenAI key only goes to `api.openai.com`, and a self-hosted endpoint is sent no key at all)
- Does not contact any third party when `WATCH_WHISPER_ENDPOINT` is set — that setting replaces the cloud backends rather than supplementing them
- Does not log, cache, or write API keys to stdout, stderr, or output files
- Does not persist anything outside the working directory and `~/.config/watch/.env` — clean up the working directory when you're done (Step 6)

**Bundled scripts:** `scripts/watch.py` (entry point), `scripts/download.py` (yt-dlp wrapper), `scripts/frames.py` (ffmpeg frame extraction), `scripts/transcribe.py` (caption selection + Whisper orchestration), `scripts/whisper.py` (Groq / OpenAI clients), `scripts/setup.py` (preflight + installer)

Review scripts before first use to verify behavior.
