# Characterizing editing style and motion graphics

Read this when the question is about **how a video is cut and composed** rather than what it
says: "characterize this reference", "match this editing style", "how fast does this cut",
"what's the pacing", "how is this motion graphic built", "make mine look like this".

The default run answers this badly if you let it. `balanced` at 512 px with dedup on is tuned
for "what happens in this video", and every one of those defaults works against a pacing
question.

## The run

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<source>" \
  --detail token-burner --no-dedup --resolution 1024
```

- **`--detail token-burner`** keeps every detected cut instead of even-sampling down to 100,
  and prints the full cut list. Pacing is the distribution, not a sample of it.
- **`--no-dedup`** matters more here than anywhere else. Dedup drops a frame that looks like
  the previous *kept* one, which on a piece that returns to a title card or a held colour
  means it drops the return — and the rhythm is exactly what you are measuring.
- **`--resolution 1024`** if there is any on-screen type you need to read. See "Typography"
  below for why the default cannot.
- Add `--start`/`--end` to sample a representative minute of anything long. Style is usually
  consistent; you rarely need the whole piece.

## Read the shot block first, before any frame

The report prints, independently of which frames survived sampling:

```
- **Shots:** 840 cuts, 84.0/min — median 0.50s, p10 0.47s, p90 3.20s (shortest 0.40s, longest 6.10s)
```

These come from the full detected-cut list, so they describe the video rather than the frame
budget. **Do not compute pacing from the gaps between the frame paths** — those gaps are the
sampling interval, and reading them as shot lengths under-reports a fast-cut piece by an
order of magnitude.

How to read it:

- **cuts/min** is the headline pacing number. Roughly: under 10 is a long-take/interview
  feel, 20–40 is standard explainer, 60+ is Vox/TikTok-style rapid cutting.
- **median vs p90** is the *texture*. A median of 0.5 s with a p90 of 3.2 s is a piece that
  cuts fast in bursts and then sits on a shot — a completely different feel from a median of
  0.5 s with a p90 of 0.6 s, which is relentless. Say which one it is.
- **p10** near the median means machine-regular cutting; well below it means punctuation cuts.

If a `Counted before near-duplicate removal` note follows the line, some of those cuts were
between visually similar shots and the distribution is slightly optimistic.

## If the report says "uniform fallback, too few shots"

The piece genuinely has no detectable cuts — a single continuous take, a screen recording, or
a slow graphic build. Before concluding that, try `--scene-threshold 0.03`: the default of
0.05 is already tuned for graphic cuts, but a very low-contrast piece can sit under it. If
that finds nothing either, the answer really is "one shot", and the interesting questions
move to motion (see `motion.md`) rather than to cutting.

## Typography: why the default frames cannot be read

Frames are 512 px wide by default. A 1920 px source is therefore scaled by **3.75×**, so
16 px body text arrives 4.3 px tall — under the ~8–10 px floor where glyphs are legible at
all. If you are asked about fonts, weights, kerning, or what a lower-third says:

- **`--crop x,y,w,h`** in source pixels around the text. This is the strongest tool and it is
  not just a motion tool: cropping a 600×200 title out of a 1920×1080 frame delivers it at
  1:1, which is both legible and *cheaper* than the full frame.
- **`--resolution 1024`** halves the downscale to 1.9×. It roughly quadruples image tokens per
  frame, so pair it with a tight `--start`/`--end` or a small `--max-frames`.

## What to state in your answer

State measured numbers, not impressions:

- **cuts/min and the shot-length percentiles**, quoted from the shot block.
- **The source frame rate**, from the report's Resolution line context — 24p, 30p and 60p are
  first-order style attributes and the answer "it's smooth" is not a substitute.
- **Source dimensions and aspect**, since a 9:16 piece is a different craft from 16:9.
- **What the cuts land on** — read the frames at the cut times from the `token-burner` cut
  list and say whether cuts land on beats, on motion, or on speech.

Then describe the style. The numbers are what make the description checkable.
