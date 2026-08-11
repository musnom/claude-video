# Measuring and recreating motion (`--motion`)

Read this when the question is about **timing or movement** rather than content:
"how fast does this move", "how long is that transition", "is this animation janky", "what
easing is this", "recreate this animation", "match this motion style".

`--motion` samples the source's **own** frames — no resampling — and labels each with its
measured presentation time to the millisecond. It never dedups, and it overrides `--detail`.

```bash
python3 "${SKILL_DIR}/scripts/watch.py" "<video-file-from-the-report-footer>" \
  --motion --start 0:12 --end 0:14 --crop 320,180,400,120 --no-whisper
```

- **Point it at the downloaded file**, not the URL, on a second pass. The first report's
  footer prints `_Video file: …_` — use that exact path. Do not construct it yourself; yt-dlp
  writes whatever extension the site served and `.webm` / `.mkv` are routine.
- **Keep the window tight.** One transition, not the scene containing it. Every source frame
  in the window is extracted, and an unbounded window is *refused* — see "If the run is
  refused" below.
- **`--crop x,y,w,h` in source pixels is the single biggest accuracy win** for a UI component.
  A 160×120 button cropped out of a 1920×1080 frame arrives at 1:1 instead of ~8% of the
  width, so its position is actually measurable — and it costs *fewer* tokens, not more.
- **Do not reach for `--fps`.** It is capped at 2 fps, reaches only the uniform sampler, and
  its frames go through dedup. It cannot do this.

## What you get back

Frame labels carry milliseconds (`t=00:12.317`), and the report states the envelope — first
change, last change, duration, peak. Alongside the frames the script writes **`motion.json`**
in the work dir: source dimensions and frame rate, the crop rect, the window, sampling stats,
and a per-frame series of `{t, gap_ms, mean_delta, peak_delta, cum_delta}`.

**`cum_delta` is the curve the envelope is read off** — change accumulated since the window
opened, always non-decreasing. It is the number to plot when you want the *shape* of the
animation: where it is steep, the thing is moving fast. Per-frame deltas cannot substitute
for it, because on a slow or low-contrast change every one of them rounds to zero while the
accumulated curve still climbs.

`peak_delta` is the per-frame figure to trust for a small element on a static background — a
whole-frame average barely registers a button sliding.

### Two lines that change your answer

**`- **Envelope clipped:** …`** means the motion was already underway at the edge of your
window, so the duration printed is a **lower bound**, not the measurement. Widen
`--start`/`--end` past the transition on that side and re-run before quoting a number.

**`- **Motion envelope:** no change detected …`** now carries the numbers it compared, e.g.
`the largest run of change in this window totalled 3.0, under the 6.0 needed to count as
motion`. If that total is close to the threshold, the animation is probably real but near the
noise floor of the recording — re-record at higher contrast, or `--crop` tighter so the moving
element fills more of the frame. If it is zero, nothing moved.

## Reading the measurements

Three things come out of a motion run, and they are all you need:

- **Duration** — the envelope, from `motion.json` or the report.
- **Position over time** — track the moving element across the frames. Their timestamps are
  absolute source time, so this falls straight out.
- **Shape** — from the spacing of those positions. Even spacing is linear, front-loaded is an
  ease-out, overshoot-and-settle is a spring.

Convert pixels to layout units using the **source** dimensions from the report, not the frame
dimensions — the frames are scaled and, when cropped, offset by the crop origin.

If the user wants this as code, write it the way you would write any other code, for their
stack and no one else's. Two things are worth carrying over from the measurement into whatever
you produce: **state the numbers you measured** next to it, so they can check your work rather
than trust it, and say which easing you concluded and why. `motion.json` is deliberately
stack-agnostic — durations, positions and a change signal, no CSS, no keyframes, no easing
names — so it serves any target equally.

### An ease-out's measured duration is shorter than its authored duration

This is a property of the video, not a defect in the measurement, and it is worth saying out
loud when you report a number.

An ease-out's velocity decays to zero, so its last stretch moves less than one pixel per
frame and is simply not recorded. Measured on a synthetic 500 ms ease-out travelling 400 px
at 60 fps: the pixels stop changing at **433 ms** for a cubic curve and **350 ms** for a
quintic one. No threshold recovers the rest — one frame before the nominal end, a quintic
curve advances about 0.00008 px.

So when you conclude "ease-out", say the measured duration **and** that the authored value is
somewhat longer. If you are recreating the animation, using the measured duration with an
ease-out curve reproduces the motion faithfully; the missing tail is, by definition, motion
nobody can see.

## If the run is refused

`--motion` over a wide window is blocked rather than warned about, because by the time a
warning could print, the JPEGs exist and are about to be read. The message states the frame
count and the image-token estimate. Three ways forward, in order of preference:

1. **Narrow the window.** The refusal message says how many seconds fit at the source's frame
   rate.
2. **`--crop` the moving component.** Fewer pixels per frame means more frames fit, and it
   makes the motion more measurable at the same time.
3. **`--max-frames N`** to accept the cost deliberately. This disables the guard.

## Getting a good recording to measure

If the user is capturing the clip themselves: record at 2×/retina so small movements survive;
leave a beat of stillness before and after the animation so its start and end are unambiguous
(this also stops the envelope reporting `clipped`); and keep the clip short.
