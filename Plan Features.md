# Planned features — deferred

Scratch file. Two features identified while auditing `/watch`, both deliberately out of scope for the
motion-analysis work. Intended to be picked up in a separate session.

Both were checked against all **75 open pull requests** on the upstream repo
(`bradautomates/claude-video`). Neither appears in any of them — searched by keyword across every PR
title and body. Contributors have overwhelmingly filed crash fixes, platform bugs and provider
integrations; nobody has touched frame *strategy*. That is not surprising, since neither of these
crashes anything. They just quietly make the answers worse.

---

## 1. Transcript-driven frame placement

### What it is

Frame selection currently has **zero connection to the transcript**. Verified by tracing `watch.py` —
nothing in the extraction path ever reads `transcript_segments`. The two are computed independently and
only meet in the final report.

So the frame budget is spread by duration and motion alone, with no idea what is being said or when.

### Why it matters

Measured on the current code, full-video budget:

| Duration | Frames | Spacing |
|---|---|---|
| 3 min | 60 | 1 per 3 s |
| 10 min | 80 | 1 per 7.5 s |
| **2 hours** | **100** | **1 per 72 s** |

Take a two-hour recording where the speech is clustered into four ten-minute bursts with long silences
between. Today it takes 100 frames evenly across the whole thing — roughly 55 of them land in dead air,
and the parts where something is actually explained get about 11 frames each.

The information about where the content is *already exists* in the same run. It is simply never used.

### How it would be used

```bash
# Today — 100 frames smeared evenly, most of them wasted
/watch https://youtu.be/<2h-talk> summarize the demo sections

# Proposed — frames concentrated where there is speech
/watch https://youtu.be/<2h-talk> --frames-follow-transcript summarize the demo sections
```

Or, more likely, no flag at all: make it the default weighting for the scene and uniform engines when a
transcript is present, since it is strictly better on spoken content.

### Design notes

- The transcript is fetched **before** frames in `watch.py`, so the data is available at selection time
  with no reordering.
- Weight the sampling density by speech density per bucket rather than skipping silence outright —
  a silent stretch may still be visually important (a slide held while nobody talks, a demo with no
  narration).
- Needs an escape hatch for content where speech and visuals are uncorrelated: music videos, silent
  screen recordings, b-roll. A `--frames-even` override, or auto-disable when speech covers more than
  ~90% of the runtime (nothing to concentrate toward).
- Interacts with the existing `--timestamps` cue pass, which is the manual version of this same idea.
  Consider whether this makes that pass redundant for most cases, or complements it.

---

## 2. Content-type-aware detail

### What it is

`--detail` comes from static config (`WATCH_DETAIL` in `~/.config/watch/.env`, default `balanced`) or an
explicit flag. It is never influenced by the video itself.

A two-hour static screencast and a ninety-second ad both get `balanced`: scene-aware frames, cap 100.

### Why it matters

The right detail mode is largely a property of the content, and the script already has the signals to
tell them apart before it commits:

- **Scene-cut density** — already computed by `extract_scene_candidates`. A screencast produces a handful;
  an ad produces hundreds.
- **Duration** — already known from `get_metadata`.
- **Caption presence** — already known before frames are extracted.
- **Source frame rate** — available from ffprobe.

A near-static 90-minute screencast wastes most of its budget on visually identical frames (dedup catches
some of this, but only after paying to extract them). A fast-cut 60-second trailer at cap 100 under
`balanced` throws away most of its scene changes.

### How it would be used

```bash
# Today — the same dial regardless of what the video is
/watch recording.mov            # balanced, 100 frames, most near-identical

# Proposed — the script picks, and says why
/watch recording.mov
# -> Detail: efficient (auto: 1.4 cuts/min over 92 min, near-static screen recording)
```

An explicit `--detail` would always win. The auto choice must be printed, never silent.

### Design notes

- Cheapest signal first. Duration and caption presence are free; cut density costs a decode pass, so it
  cannot be the primary gate on long videos.
- Risk: a wrong auto-choice is worse than a mediocre fixed default, because the user does not know it
  happened. Printing the reasoning is not optional.
- Overlaps feature 1 — both are about spending the budget where the information is. Worth designing
  together, since a transcript-aware sampler may make the detail choice matter less.
- Probably wants to be opt-in first (`--detail auto`) and only become the default once it has been
  wrong-tested against a spread of real content.

---

## Relationship to the motion work

Neither of these overlaps `--motion`. That feature is a per-question precision tool for a tight window;
these two are about how the *default* budget gets spent across a whole video. They can be built in
either order.
