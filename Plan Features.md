# What needs fixing

Rewritten 2026-08-11. Replaces two proposed frame-strategy features that were measured and
dropped (see **Dropped, with evidence**).

Source: a 45-agent audit of the three real use cases, where every finding was
adversarially re-verified against ground truth — 29 confirmed, 10 refuted. Refuted items are
listed at the bottom so they don't get re-proposed. Severities below are the *verifiers'*,
not the finders'.

**UC1** general video understanding · **UC2** screen recordings / web-UI animation ·
**UC3** motion graphics + editing-style reference.

Ten of the confirmed findings collapse into **four root causes**. Fix those four and most of
the list goes away.

---

## Root cause A — motion uses an absolute per-frame delta — high

`motion_envelope` flags frames whose `peak_delta` clears an absolute **6.0**
(`frames.py:232-257`), and defines the animation as first-to-last flagged frame. That single
choice produces four separate confirmed failures in UC2's flagship feature:

**A1. Light-mode UI is invisible.** Contrast sweep, identical 400 ms card fade-in on white:

| card colour | contrast | result |
|---|---:|---|
| `#E8E8ED` | 23 | **no change detected above the noise floor** |
| `#C8C8CD` | 55 | **no change detected** |
| `#909095` | 110 | 367 ms |
| `#202025` | 222 | detected |

Light-mode web UI — the common case — reads as "nothing happened."

**A2. Cross-dissolves and fades are invisible.** A 1 s cross-dissolve peaks at 4.0 < 6.0 →
"no change detected", while `motion.json` shows it spanning exactly 3.000–4.000 s. SKILL.md
:115/:162 route *"how long is this cross-dissolve"* straight here.

**A3. Eased durations are under-reported by 13–33 %.** Ground truth 500 ms, box 200→900 px:

| easing | measured | error |
|---|---:|---:|
| cubic ease-out @60fps | 433 ms | −13.4 % |
| cubic ease-out @30fps | 433 ms | −13.4 % |
| cubic ease-out @15fps | 467 ms | −6.6 % |
| quintic ease-out @60fps | **333 ms** | **−33.4 %** |

Coarser sampling being *more* accurate is the tell: the metric is wrong, not the sampling.
The tail of an ease-out moves slowly, so its per-frame delta drops under the floor and the
animation is declared over early. SKILL.md:204 tells the model to "always state the measured
numbers" — so it states these, confidently.

**A4. The envelope silently clips at the window edge.** Same 300 ms slide:

```
--start 0.90 --end 1.50  ->  300 ms   correct
--start 0.95 --end 1.30  ->  283 ms
--start 1.00 --end 1.30  ->  183 ms
```

The docs tell you to use a tight window, and a tight window silently truncates the answer.

**One fix for all four.** Measure change *cumulatively* rather than frame-to-frame:
`measure_motion` already holds every thumbnail, so add `cum_delta = _cell_deltas(thumbs[i],
thumbs[0])` and define the envelope as the span where the cumulative curve moves between
~2 % and ~98 % of its final value. That finds the slow tail, sees low-contrast ramps, and
handles dissolves. Separately, set `clipped_start`/`clipped_end` when the envelope touches
frame 0 or n−1, surface both in `motion.json` and the report.

## Root cause B — dedup thresholds on the mean, so real UI changes are deleted — high

`DEDUP_THRESHOLD = 2.0` on the **mean** per-pixel delta (`frames.py:39`). A localised change
barely moves a full-frame mean. Measured on 1920×1080 slide states, each adding one 620×34
text line, through watch.py's own 512 px pipeline:

```
deltas: [1.672, 1.816, 1.543, 1.703]   all < 2.0
```

End-to-end on a 50 s deck with 5 distinct build states: **3 frames kept, states 2 and 4
deleted**, reported as "37 near-duplicates dropped". `--no-dedup` returns all 40. The
docstring at `frames.py:32-39` explicitly claims "a slide-gaining-a-bullet survives." It does
not.

This hits UC1 (lectures, slide decks) and UC2 (a screen recording where one control changes
state is exactly the frame you needed).

**Fix.** Keep a frame when the mean clears 2.0 **or** the peak single-cell delta clears a
small threshold. `_cell_deltas` (`frames.py:183`) already computes both and motion mode
already uses it.

## Root cause C — scene detection is mistuned in both directions — high

**C1. Threshold too high for graphic cuts.** `SCENE_THRESHOLD = 0.20`; punch-ins and graphic
slide changes score 0.05–0.10:

| clip (ground truth) | th=0.20 | th=0.10 | th=0.05 |
|---|---:|---:|---:|
| 3 punch-in cuts | **0** | 0 | 3 |
| 8-shot infographic (7 cuts) | **0** | 0 | 7 |

The infographic clip reports `uniform fallback, too few shots (1)` — the tool tells the model
a motion-graphics piece has one shot. That is UC3's primary input.

**C2. No top-up to the budget.** When detection *does* work, the engine returns one frame per
shot and stops. A 12-minute, 12-shot clip: `12 selected from 12 candidates (scene, cap 100)`
— 88 frames of budget unused, one frame per 58 s, silently. The same clip at `--detail
efficient` returns 50. The cheap mode beats the default.

**Fix.** Default the threshold to ~0.05 (all three cases resolve there, no false positives on
a solid-colour control) and thread a `--scene-threshold` flag to the parameter
`extract_scene_candidates` already accepts (`frames.py:658`) but that `extract_scene_or_uniform`
never passes. Then, when `len(deduped)` is far under budget, fill the largest time gaps with
uniform samples tagged `reason=fill`, keeping scene frames pinned.

## Root cause D — cut timing is computed, then discarded or rounded away — medium

**D1. Sampled away.** Every cut is timestamped (`frames.py:702-714`); `_even_sample` keeps
~100 *by list index* and unlinks the rest with their timestamps (`frames.py:841-847`). On a
clip cutting every 0.5 s (840 cuts / 600 s), report gaps read median 6 s → **~10 cuts/min vs
a true 120 — 12× wrong**, stated confidently because timestamps look authoritative.

**D2. Rounded.** Scene/keyframe/uniform labels are whole seconds. On a 120-shot clip that fit
*under* the cap, 20 % of consecutive pairs print the same timestamp. Fast cutting lives in
the 0.3–1.0 s band that whole seconds cannot represent — the band that defines Vox-style and
TikTok pacing.

**Fix.** Emit a shot block from the full timestamp list, independent of which frames survived
— already in memory, no extra decoding:

```
- **Shots:** 840 cuts, 84.0/min — median 0.50s, p10 0.47s, p90 0.53s
```

Use `format_time_ms` (or tenths) for scene/keyframe labels. Full cut list under
`token-burner`. Note dedup can drop a cut between similar shots and bias the distribution.

---

## Standalone

| # | finding | sev | UC |
|---|---|---|---|
| 1 | **`--motion` has no frame-count or token guard.** No window, 60 s @30fps → 1800 frames, no warning. At 512 px/16:9 = 197 tok/frame, a 33 s window @60fps hits the 2000 cap ≈ **394k image tokens**. The existing warning fires only when it *thins* — i.e. when the run got cheaper. | med | UC2 |
| 2 | **SKILL.md's token figure is 3–5× too high.** ":330 claims 50–80k for 80 frames; real is 16k (16:9) / 21k (4:3) / 28k (1:1) via `(w×h)/750`. It is the only cost number the model has, so it suppresses the cheap second passes the two-pass design depends on. | med | all |
| 3 | **A focused `--start/--end` run uploads the *entire* video's audio to Whisper.** 10-min clip, 20 s window: 4689 kB uploaded, ~156 kB needed. `extract_audio` takes no `-ss`/`-to` (`whisper.py:146-170`); `shift_segments` already exists at `whisper.py:366`. | med | all |
| 4 | **No editing-style workflow in SKILL.md.** There is a precise motion/recreation workflow (:159-215) and nothing equivalent for UC3, so "characterize this reference" silently runs the worst possible config for that question (`balanced`, 512 px, dedup on, threshold 0.20 — i.e. root causes B, C and D all at once). | med | UC3 |
| 5 | **Uniform labels are early by up to half the sampling period** — a frame labelled 3.75 s can show 4.0 s content. | med | UC3 |
| 6 | **No numeric colour anywhere.** Grade questions want black point, white point, palette. Fidelity is fine (measured `#1E6F5C` → `RGB(31,111,93)`); nothing quantifies it. A `signalstats` pass over selected frames would do it. | med | UC3 |
| 7 | **`token-burner` returns the identical frame set to `balanced`** in the case the report's own long-video warning recommends it for. | med | UC1 |
| 8 | **The documented second pass guesses the filename.** SKILL.md hardcodes `download/video.mp4` (:169/:253/:257); `download.py:151` writes `video.%(ext)s` and `_pick_video` exists because the extension is unpredictable. Breaks Step 3 — the default flow. | — | UC1/2 |
| 9 | **Source fps printed only under `--motion`.** 24p/30p/60p is a first-order style attribute and `get_metadata` already has it. | low | UC3 |
| 10 | **Audio-only input aborts with a raw ffmpeg dump**, zero stdout, exit 1 — when the transcript path would have worked. A `.png` fails identically. | low | UC1 |
| 11 | **A container with no duration header** (OBS/browser `.webm`, piped output) yields **one frame** for the whole video. | low | UC2 |
| 12 | **`--fps` still silently ignored on the keyframe→uniform fallback** — the warning added this pass doesn't cover that path. | low | — |
| 13 | **Frames are 3.75× smaller than source** (16 px body → 4.3 px). `--resolution 1024` and `--crop` fix it; `--crop` is documented only as a motion tool. | low | UC2/3 |

---

## Decisions

**Login-walled sources.** Take upstream **PR #21** (`--cookies-from-browser`, +38/−10, opt-in,
flag-injection guard, correctly rewrites the Security section). Reject **#94**'s auto-probe
across five browsers on failure — silently reading a cookie jar because a download failed is
a privacy surprise, and each probe can raise a macOS Keychain prompt. Lift **#64**'s generic
`--cookies FILE`; skip its 529-line Fathom resolver. Same flag is the standard mitigation for
YouTube's bot gate — seven upstream PRs are about that failure and this repo has no
mitigation for it today.

**Live streams — guard, don't build.** `is_live` is never read (`grep` over `scripts/`), so a
live URL makes yt-dlp record indefinitely and the run hangs. `fetch_captions` already runs
`--skip-download --write-info-json` (`download.py:99-116`), so `is_live` is free *before* the
download. Refuse unless an explicit window is given: ~4 lines, a hang fix not a feature. Do
not build live monitoring — no stated use case needs it, and watch's contract ("bounded
artifact → complete report") does not survive an unbounded input.

**Don't split into multiple skills.** All three use cases share one pipeline, and the
packaging model requires SKILL.md and `scripts/` to stay siblings. You also usually can't
tell which mode you need until after pass one. The real problem is SKILL.md's size (357 lines,
plus the missing UC3 workflow) — solve it with progressive disclosure: keep triggers and the
common path in SKILL.md, move motion and editing-style workflows into `references/*.md` read
on demand. `build-skill.sh` allows this (≤200 files, exactly one SKILL.md).

---

## Fixed in this pass

The "report describes a code path that did not run" family:

- **`budget {target}` on every run** — `target`/`fps` reach only the uniform fallback. Each
  engine now reports the cap and budget it enforced (`effective_cap`/`budget` in `frame_meta`).
- **`token-burner` printed `cap unlimited` while capping at ≤100** (`frames.py:997`).
- **`"1 selected from 1 candidates, 59 near-duplicates dropped"`** — arithmetically
  impossible; the fallback reported scene count instead of what dedup compared.
- **`"uniform with uniform fallback"`** → `uniform fallback, too few shots (1)`.
- **`--fps` was a silent no-op** on the scene and keyframe engines (see #12 for the remaining
  path).

Six regression tests in `tests/test_frames.py`; 266 passing.

---

## Dropped, with evidence

**Transcript-driven frame placement.** Built the doc's own scenario (content in bursts, ~67 %
dead air): **90–98 % of frames landed in content**, not the predicted 45 %. SKILL.md Step 3
already does this better — the model picks cue timestamps knowing the question.

**Content-type-aware `--detail`.** The gating signal costs the decode you're avoiding, and on
a 600 s near-static screencast `balanced` and `efficient` returned the same 10 frames (dedup
runs before the cap). Identical answer; only wall clock differs.

**Restructuring the write-then-delete path.** Measured: current single pass **5.76 s / 5.4 MB**;
null-output detection alone **6.24 s** (*slower*); detect + seek-extract **11.1 s**; dedup
read-back 0.10 s; unlinking 740 files 0.06 s. The rewrite is **~2× slower** to save 0.16 s.
`frames.py:22-27` is right.

**Refuted by verification** (finder claimed, verifier disproved — do not re-raise):
report omitting actual frame dimensions; report omitting total frame count / token estimate;
absolute frame paths being wasteful; `--crop` coordinate discovery being impossible; motion
signal zeroing on *cap* thinning (the bad-probe variant is real, the cap variant is not);
`get_metadata`'s fps fallback being unreachable; cue frames vanishing silently; `--resolution
0` misreporting; the two-yt-dlp-pass claim (accurate, but not the waste it was framed as).

**Cue-frame timestamp drift.** Tested with keyframes forced 5 s apart and a grey level
encoding the second: requested 7.5 s, got second 7. No bug.
