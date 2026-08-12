# /watch

**Give Claude the ability to watch any video.**

Claude Code (recommended — auto-updates via marketplace):
```
/plugin marketplace add mustafa-nom/claude-video
/plugin install watch@claude-video
```

Codex, Cursor, Copilot, Gemini CLI, or any of 50+ [Agent Skills](https://agentskills.io) hosts:
```bash
npx skills add mustafa-nom/claude-video -g
```
(`-g` installs globally for your user, available across all projects. Drop it to scope per-project.)

More install options (claude.ai web, manual) in the [Install](#install) section below.

Zero config to start — `yt-dlp` and `ffmpeg` install on first run via `brew` on macOS (Linux/Windows print exact commands). Captions cover most public videos for free. Whisper API key is only needed when a video has no captions.

---

Claude can read a webpage, run a script, browse a repo. What it can't do, out of the box, is *watch a video*. You paste a YouTube link and it has to either guess from the title or pull a transcript that's missing 90% of what's on screen.

With Claude Video `/watch` you can paste a URL or a local path, ask a question, and Claude fetches captions first, downloads only what it needs, extracts frames (scene-aware, or fast keyframes at `efficient` detail), pulls a timestamped transcript (free captions when available, Whisper API as fallback), and `Read`s every frame as an image. By the time it answers, it has *seen* the video and *heard* the audio.

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
```

## What people actually use it for

**Analyze someone else's content.** `/watch https://youtu.be/<viral-video> what hook did they open with?` Claude looks at the first frames, reads the opening transcript, breaks down the structure. Same for ad creative, competitor launches, podcast intros, anything where the *how* matters as much as the *what*.

**Diagnose a bug from a video.** Someone sends you a screen recording of something broken. `/watch bug-repro.mov what's going wrong?` Claude watches the recording, finds the frame where the issue appears, describes what's on screen, often catches the cause without you ever opening the file.

**Summarize a video.** `/watch https://youtu.be/<long-thing> summarize this` does the obvious thing — pulls the structure, the key moments, what was actually said and shown. Faster than watching at 2x.

**Cut the hype out of an update video.** `/watch https://youtu.be/<launch-video> what's actually new — skip the hype` Strip a "game-changer" feature drop down to the few things that matter, so you get the substance without ten minutes of intro and overselling.

**Turn a playlist into notes.** `/watch https://youtu.be/<video> summarize this to a note` Run it across a series and file a per-video summary, so a channel or course becomes a searchable set of notes instead of hours you have to sit through.

**Measure and recreate motion.** `/watch reference.mp4 --motion --start 1.0 --end 1.6 what easing is this?` samples every source frame across a transition, labels each to the millisecond, and writes a stack-agnostic `motion.json` — enough to state a transition's duration and easing as numbers, and to rebuild the animation for your stack. `/watch <reference> characterize the editing style` reads the full detected-cut list (cuts/min, shot-length percentiles) instead of eyeballing pacing.

## How it works

1. **You paste a video and a question.** URL (anything yt-dlp supports — YouTube, Loom, TikTok, X, Instagram, plus a few hundred more) or a local path (`.mp4`, `.mov`, `.mkv`, `.webm`).
2. **`yt-dlp` checks captions first.** At `transcript` detail, captioned URLs return without downloading video. Otherwise, or when Whisper needs audio, it downloads only what the run needs.
3. **`ffmpeg` extracts frames at the chosen detail.** `efficient` decodes keyframes only (near-instant); `balanced`/`token-burner` prefer scene-change frames and fall back to the duration-aware uniform sampler when they under-produce. JPEGs are 512px wide by default and clamped to 1998px tall for Claude Read compatibility.
4. **The transcript comes from one of two places.** First try: `yt-dlp` pulls native captions (manual or auto-generated) from the source. Free, instant, accurate-ish. Fallback: extract a mono 16 kHz 64 kbps mp3 audio clip (~480 kB/min) and ship it to Whisper — Groq's `whisper-large-v3` (preferred — cheaper and faster) or OpenAI's `whisper-1`.
5. **Frames + transcript are handed to Claude.** The script prints frame paths with `t=MM:SS` markers and the transcript with timestamps. Claude `Read`s each frame in parallel — JPEGs render directly as images in its context.
6. **Claude answers grounded in what's actually on screen and in the audio.** Not "based on the description" or "according to the title." It saw the frames. It heard the transcript. It answers the way someone who watched the video would.
7. **Cleanup.** The script prints a working directory at the end. If you're not asking follow-ups, Claude removes it.

## Frame budget — why it matters

Token cost is dominated by frames. Every frame is an image; image tokens add up fast. The script's auto-fps logic exists so you don't blow your context budget on a sparse scan of a 30-minute video that would have been better answered by a focused 30-second window.

| Duration | Default frame budget | What you get |
|----------|---------------------|--------------|
| ≤30 s | ~30 frames | Dense — basically every key moment |
| 30 s - 1 min | ~40 frames | Still dense |
| 1 - 3 min | ~60 frames | Comfortable |
| 3 - 10 min | ~80 frames | Sparse but workable |
| > 10 min | 100 frames (capped modes) | "Sparse scan" warning — re-run focused, or `--detail token-burner` for full uncapped coverage |

When the user names a moment ("around 2:30", "the last 30 seconds", "from 0:45 to 1:00"), pass `--start` / `--end`. Focused mode gets denser per-second budgets, capped at 2 fps. Far more useful than a sparse pass over the whole thing.

## Frame deduplication

Frame selection — keyframes (`efficient`), scene-change detection (`balanced`/`token-burner`), or the uniform sampler it falls back to — can still surface near-identical frames: a screen recording that holds one slide for 90 seconds produces a dozen, each billed as a separate image. A dedup pass drops them before frames reach Claude. It runs by default on every frame mode (`--no-dedup` turns it off):

1. One `ffmpeg` call scales each extracted JPEG to a 16×16 grayscale thumbnail. Everything after is pure-stdlib Python — no image libraries.
2. For each frame, compute the **mean** per-pixel difference *and* the **peak single-cell** difference against the *last frame that was kept*.
3. A frame is dropped only when **both** are under threshold (mean ≤ 2.0 AND peak ≤ 8.0). The peak rule is what keeps a localized change — a slide gaining one bullet, a control changing state — that barely moves a whole-frame average.
4. A trailing run of solid-black frames (end cards) is dropped on the same thumbnails; mid-video and leading black are kept — a fade-to-black boundary is editing information.
5. The frame-budget cap applies *after* dedup, so the budget is spent on distinct frames — and the gap-fill pass that spends leftover budget applies the same rule in reverse: a fill that would just duplicate its neighbours is never created.

Comparing against the last *kept* frame (not the previous one) catches slow fades that never trip a frame-to-frame threshold.

The **Frames** line reports everything that moved the count, e.g. `100 selected from 11 candidates (scene, 3 near-duplicates dropped, 89 added to fill gaps, full range, cap 100)` — or, on static footage, `11 selected … 0 added to fill gaps (fill stopped early: 11 near-duplicate candidates rejected)`. On always-moving footage nothing is dropped and you pay what you would have anyway.

## Detail modes

The `--detail` dial trades speed and token cost for visual fidelity:

| Mode | Engine | Cap | Character |
|------|--------|-----|-----------|
| `transcript` | none (captions) | 0 frames | Cheapest by far — captioned URLs return without downloading video at all |
| `efficient` | keyframe (`-skip_frame nokey`) | 50 | The speed tier: only reconstructs keyframes, ~40× faster than the scene modes (measured ~0.5 s vs ~21 s on a 49-minute 720p recording) |
| `balanced` (default) | scene-change | 100 | Decodes every frame to find cuts, then spends leftover budget filling the widest time gaps — with distinct content only |
| `token-burner` | scene-change | uncapped | Keeps *every* detected cut and gap-fills to at least `balanced`'s coverage — it can never return less than the default |

- **Image tokens** use Anthropic's `(width × height) / 750` — at the default 512px width a 720p frame is 512×288, **≈197 tokens/frame**; `--resolution 1024` roughly 4×s that. The transcript is surfaced in every captioned mode and on long videos is often the larger cost.
- **One sampling rule across frame modes.** Each detects all candidates across the full range, then even-samples (first + last always kept) down to its cap — so the last frame always lands at the end, not partway through. The scene modes then top up the widest gaps, checking each fill against its neighbours with the dedup rule so static stretches are never padded: a 5-minute video of held shots comes back as ~a dozen distinct frames with a `fill stopped early` note, while a screen recording whose content evolves comes back near the cap.
- **`efficient` can return *more* frames than `balanced`** on low-motion footage (encoders emit keyframes on a timer, so they can outnumber scene cuts); "efficient" means fast extraction, not fewer frames.

## Install

| Surface | Install |
|---------|---------|
| **Claude Code** | `/plugin marketplace add mustafa-nom/claude-video` then `/plugin install watch@claude-video` |
| **Codex, Cursor, Copilot, Gemini CLI, +50 more** | `npx skills add mustafa-nom/claude-video -g` |
| **claude.ai** (web) | [Download `watch.skill`](https://github.com/mustafa-nom/claude-video/releases/latest) → Settings → Capabilities → Skills → `+` |
| **Manual / dev** | `git clone` then symlink `skills/watch` into your host's skills dir (see below) |

### Claude Code

```
/plugin marketplace add mustafa-nom/claude-video
/plugin install watch@claude-video
```

Update later with `/plugin update watch@claude-video`.

### Codex, Cursor, Copilot, Gemini CLI, and 50+ other hosts

The [Agent Skills](https://agentskills.io) CLI installs the skill into whatever agents it detects:

```bash
npx skills add mustafa-nom/claude-video -g
```

`-g` installs globally for your user (`~/.codex/skills`, `~/.cursor/skills`, etc.); drop it to install into the current project instead. Useful flags:

- `-a, --agent <names…>` — target specific hosts, e.g. `-a codex -a cursor`
- `-l, --list` — list the skills in this repo without installing
- `--copy` — copy files instead of symlinking (for filesystems without symlink support)

The CLI discovers the skill from `skills/watch/SKILL.md` and copies the whole folder — `SKILL.md` plus its `scripts/` runtime — as a self-contained unit. `SKILL.md` resolves its own scripts relative to wherever it was installed, so it works the same on every host.

Update later with `npx skills update watch -g`.

### claude.ai (web)

1. [Download `watch.skill`](https://github.com/mustafa-nom/claude-video/releases/latest) from the latest release.
2. Go to Settings → Capabilities → Skills.
3. Click `+` and drop the file in.

Enable "Code execution and file creation" under Capabilities first — the skill shells out to `ffmpeg` and `yt-dlp`, so it won't run without it.

### Manual (developer)

Clone the repo and symlink the self-contained skill folder into your host's skills directory — the symlink keeps the install in sync with your working tree as you edit:

```bash
git clone https://github.com/mustafa-nom/claude-video.git
ln -s "$(pwd)/claude-video/skills/watch" ~/.claude/skills/watch   # or ~/.codex/skills/watch
```

For claude.ai, build the `.skill` bundle from source: `bash skills/watch/scripts/build-skill.sh` produces `dist/watch.skill`.

## First run

On the first `/watch` call, the skill runs `scripts/setup.py --check`. If `ffmpeg` / `yt-dlp` aren't on your PATH, or no Whisper API key is set, it walks you through fixing it:

- **macOS** — auto-runs `brew install ffmpeg yt-dlp`.
- **Linux** — prints the exact `apt` / `dnf` / `pipx` commands.
- **Windows** — prints the `winget` / `pip` commands.
- **Transcription** — scaffolds `~/.config/watch/.env` (mode `0600`) with commented placeholders for `GROQ_API_KEY` (preferred), `OPENAI_API_KEY`, and the fully-local `WATCH_WHISPER_ENDPOINT`. Declining transcription is a first-class choice: `python3 scripts/setup.py --complete` records it and you're never nagged again.

After setup, preflight is silent and `/watch` just works. The check is a sub-100ms lookup, so it doesn't slow you down on subsequent runs.

## Bring your own keys

Captions cover the majority of public videos for free. The Whisper fallback only kicks in when a video genuinely has no caption track — typically local files, TikToks, some Vimeos, and the occasional caption-less YouTube upload.

| Capability | What you need | Cost |
|------------|---------------|------|
| Download + native captions | `yt-dlp` + `ffmpeg` | Free |
| Whisper fallback, fully local | `WATCH_WHISPER_ENDPOINT` → any OpenAI-compatible server | Free, nothing leaves your machine |
| Whisper fallback (preferred cloud) | [Groq API key](https://console.groq.com/keys) — `whisper-large-v3` | Cheap, fast |
| Whisper fallback (alt cloud) | [OpenAI API key](https://platform.openai.com/api-keys) — `whisper-1` | Standard pricing |
| Disable Whisper entirely | `--no-whisper` | Free, frames-only when no captions |

### Fully local transcription

The Whisper client speaks plain OpenAI-compatible multipart, which is the same
protocol whisper.cpp's `whisper-server`, speaches, LocalAI and vLLM all expose.
So there is no package to install and no separate backend — just point it at
your own server:

```bash
# in ~/.config/watch/.env
WATCH_WHISPER_ENDPOINT=http://127.0.0.1:8080/v1/audio/transcriptions
WATCH_WHISPER_MODEL=whisper-1          # optional
```

No `Authorization` header is sent, the endpoint takes priority over any cloud
keys you have configured, and the report labels the run `whisper (custom)` so
it is obvious where the audio went.

## Usage

```
/watch https://youtu.be/dQw4w9WgXcQ what happens at the 30 second mark?
/watch https://www.tiktok.com/@user/video/123 summarize this
/watch ~/Movies/screen-recording.mp4 when does the UI break?
/watch https://vimeo.com/123 what tools does she mention?
```

Focused on a specific section — denser frame budget, lower token cost:
```
/watch https://youtu.be/abc --start 2:15 --end 2:45
/watch video.mp4 --start 50 --end 60
/watch "$URL" --start 1:12:00            # from 1h12m to end
```

Other knobs (passed to `scripts/watch.py`):

- `--detail transcript|efficient|balanced|token-burner` — fidelity/speed dial. `transcript` skips frames (transcript only); `efficient` uses fast keyframes (cap 50); `balanced` uses scene-aware frames (cap 100); `token-burner` is scene-aware and uncapped.
- `--timestamps T1,T2,…` — grab a frame at each absolute timestamp (`SS`/`MM:SS`/`HH:MM:SS`). Claude reads the transcript first, then targets the moments the presenter flags ("look here", "as you can see"). Added on top of the detail frames (reserved against the cap); out-of-window cues are dropped in focus mode; with `--detail transcript` these become the only frames.
- `--max-frames N` — lower the frame cap for a tighter token budget.
- `--resolution W` — bump frame width to 1024 px when Claude needs to read on-screen text (slides, terminals, code).
- `--fps F` — nudge the uniform sampler's rate (still capped at 2 fps; asking for more prints a clamp notice). Only affects the uniform-sampling fallback, so it has no effect under `--detail efficient` or on clips the scene engine handles.
- `--scene-threshold T` — how different two frames must be to count as a cut, 0–1 (default 0.05; lower finds more). Scene modes only. The default already catches motion-graphics cuts; reach for `0.03` when something you can see cutting reports "too few shots".
- `--whisper groq|openai|custom` — force a specific Whisper backend. Default: a self-hosted endpoint if `WATCH_WHISPER_ENDPOINT` is set, else Groq, else OpenAI.
- `--no-whisper` — disable transcription entirely; frames only.
- `--transcribe-anyway` — proceed past the ~60-minute cloud-transcription guard (a long captionless video is a real API bill; the guard prints the estimate first).
- `--sub-langs LIST` — override the caption languages requested (also `WATCH_SUB_LANGS` in the config), for videos whose only track is a regional variant (`en-CA`) or a non-English language without an `-orig` track.
- `--cookies-from-browser BROWSER[:PROFILE]` — read cookies from a local browser so yt-dlp can reach a login-walled, age-gated or members-only video (e.g. `chrome`, `firefox:default`). Strictly opt-in; nothing is read unless you pass it.
- `--cookies FILE` — same, from an exported Netscape-format cookie file.
- `--motion` — frame-by-frame motion analysis for measuring or recreating animation. Samples the source's own frames rather than resampling to a rate, labels each with its measured timestamp to the millisecond, and never dedups. Writes a stack-agnostic `motion.json` alongside the frames. Overrides `--detail`.
- `--crop x,y,w,h` — crop to a region in source pixels before scaling. A 160x120 component out of a 1920x1080 frame arrives at 1:1 rather than 8% of the width, so its position is measurable — and it costs fewer tokens, not more.
- `--no-dedup` — keep near-duplicate frames. By default a frame-delta pass drops frames that are visually near-identical to the one before them (held slides, static screen recordings, paused video), so the frame budget is spent on distinct content; this flag turns that off.
- `--out-dir DIR` — keep working files somewhere specific (default: auto-generated tmp dir).

## Limits

- **Long-video accuracy depends on the detail mode.** On the capped modes (`efficient`, default `balanced`) coverage thins out past ~10 minutes — the frame cap spreads across the whole clip, so the script prints a "sparse scan" warning and you're better off re-running focused with `--start`/`--end`. `token-burner` lifts the cap and keeps *every* scene-change frame across the full video, so it stays complete on longer clips at the cost of more image tokens. The 10-minute mark is guidance for the capped modes, not a hard ceiling.
- **Detail is one dial.** Defaults are balanced: scene-aware frames, 2 fps max, 100-frame cap. Use `--detail efficient` for a fast 50-frame keyframe pass, or `--detail token-burner` for uncapped scene candidates. Set `WATCH_DETAIL` in `~/.config/watch/.env` to change the default.

## Structure

```
.
├── skills/watch/                 # self-contained skill — copied as a unit by every installer
│   ├── SKILL.md                  # skill contract — the source of truth across all surfaces
│   ├── references/
│   │   ├── motion.md             # the --motion measurement/recreation workflow (read on demand)
│   │   ├── editing-style.md      # pacing / motion-graphics characterization workflow
│   │   └── setup.md              # first-run + remediation flow (read when preflight says so)
│   └── scripts/
│       ├── watch.py              # entry point — orchestrates download → frames → transcript
│       ├── download.py           # yt-dlp wrapper (retry, failure classification, guards)
│       ├── frames.py             # ffmpeg frame extraction + auto-fps + dedup + gap-fill
│       ├── transcribe.py         # VTT parsing + dedupe + Whisper orchestration
│       ├── whisper.py            # Groq / OpenAI / self-hosted clients (pure stdlib)
│       ├── config.py             # shared settings resolution (env → ~/.config/watch/.env → ./.env)
│       ├── setup.py              # preflight + installer (+ --complete)
│       └── build-skill.sh        # build dist/watch.skill for claude.ai upload (dev-only)
├── hooks/                        # SessionStart status hook (Claude Code only)
├── .claude-plugin/               # plugin.json + marketplace.json (Claude Code)
├── .codex-plugin/                # plugin.json — Codex/agents manifest ("skills": "./skills/")
├── .agents/plugins/              # marketplace.json — Agent Skills marketplace listing
├── CLAUDE.md → AGENTS.md         # generic-agent entry point (CLAUDE.md includes AGENTS.md)
├── tests/                        # pytest suite (ffmpeg-synthesized clips, no network)
└── .github/workflows/            # tests.yml (branch CI) + release.yml (tag → test → build watch.skill)
```

## Develop

```bash
# Run the test suite (stdlib + pytest; ffmpeg required for frame tests):
python3 -m pytest -q

# Build the claude.ai upload bundle:
bash skills/watch/scripts/build-skill.sh      # → dist/watch.skill
```

Releasing: tag `vX.Y.Z`, push the tag. The workflow builds `dist/watch.skill` and attaches it to the GitHub release. Keep the version in sync across `skills/watch/SKILL.md`, `.claude-plugin/plugin.json`, and `.codex-plugin/plugin.json`.

See [CHANGELOG.md](CHANGELOG.md) for version history.

## Open source

MIT license.

Built on `yt-dlp`, `ffmpeg`, and Claude's multimodal `Read` tool. Whisper transcription via [Groq](https://groq.com) or [OpenAI](https://openai.com).

Maintained by [Mustafa Nomair](https://github.com/mustafa-nom). Forked from [bradautomates/claude-video](https://github.com/bradautomates/claude-video) by Brad Bonanno, who makes content about building with AI on [YouTube (@bradbonanno)](https://www.youtube.com/@bradbonanno) — see the [CHANGELOG](CHANGELOG.md) for what has diverged since the fork.

## Star History

<a href="https://www.star-history.com/?repos=mustafa-nom%2Fclaude-video&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=mustafa-nom/claude-video&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=mustafa-nom/claude-video&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=mustafa-nom/claude-video&type=date&legend=top-left" />
 </picture>
</a>

---

[github.com/mustafa-nom/claude-video](https://github.com/mustafa-nom/claude-video) · [upstream](https://github.com/bradautomates/claude-video) · [LICENSE](LICENSE)
