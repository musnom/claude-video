#!/usr/bin/env python3
"""Probe video metadata and extract frames at an auto-scaled fps.

Auto-fps targets a frame budget, not a fixed rate. Token cost scales with frame
count, so budget-by-duration keeps short videos dense and long videos capped.
When a user-specified range is passed, focused-mode budgets denser (they are
zooming in for detail).
"""
from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_FPS = 2.0
# ffmpeg's scene metric is the mean absolute luma difference between consecutive
# frames, normalized to 0-1 — i.e. a straight area-times-contrast budget, and
# chroma is not counted. A cut between two unrelated camera shots repaints the
# whole frame and scores 0.8-1.0; a motion-graphics or infographic cut repaints a
# tenth of it and scores an order of magnitude lower. Measured on an 8-shot
# infographic whose cuts swap bars inside a frame that otherwise holds still:
#
#     threshold 0.20   0 of 7 cuts found     threshold 0.05   7 of 7
#     threshold 0.10   0 of 7 cuts found     (every cut scores 0.069-0.071)
#
# At 0.20 that clip reported "uniform fallback, too few shots (1)" — the tool
# telling the model a motion-graphics piece has one shot. Lowering the default is
# the riskiest change in this file, so it was checked for over-detection against
# every other fixture: cut / static / motion / slide / vfr return identical
# candidate counts at 0.20 and 0.05 (13 / 1 / 1 / 1 / 8). 0.03 is where it starts
# to fire on encoder noise — the vfr clip jumps 8 -> 13 — so 0.05 sits with real
# margin on both sides rather than at an edge.
SCENE_THRESHOLD = 0.05
# Keep scene-detection results once we have at least this many distinct shots.
# Below this the video is effectively static (screen recording, talking head),
# so we fall back to uniform sampling. Matching the reference fork's behaviour,
# this is a low floor — NOT the frame budget — so normal videos with cuts use
# the (single-pass) scene engine instead of paying for a wasted second decode.
SCENE_MIN_FRAMES = 8
# Do not add a frame to split a hole narrower than this. Scene selection can
# leave most of the cap unspent on a long take, but a clip whose frames are
# already ~2s apart is covered — padding it to the cap would double the token
# cost for nothing. Measured effect on the existing fixtures: none, because
# their gaps are all under 2s.
GAP_FILL_MIN_SECONDS = 2.0
# Below this many decoded keyframes a clip is too sparse for keyframe coverage
# (very short or oddly encoded), so the cheap tier falls back to uniform.
KEYFRAME_MIN = 4
MAX_READ_DIMENSION = 1998
# Frame-delta dedup: downscale each frame to a DEDUP_THUMB x DEDUP_THUMB
# grayscale thumbnail and treat two frames as near-identical only when BOTH the
# mean per-pixel difference is at or below DEDUP_THRESHOLD and the largest
# single-cell difference is at or below DEDUP_PEAK_THRESHOLD. Unlike a
# within-frame perceptual hash, this distinguishes flat frames (solid slides,
# fades) by luma.
#
# The mean alone does not work, and the claim that it did was wrong. A change
# confined to a few of the 256 cells barely moves a whole-frame average: measured
# on 1920x1080 slide states each gaining one 620x22 text line, through this
# module's own 512px pipeline, the means are 1.42 / 1.57 / 1.68 / 1.77 — every one
# of them under 2.0, so a five-state deck came back as three frames with the
# builds silently deleted. The same frames' peak cells read 55-70.
#
# DEDUP_PEAK_THRESHOLD is calibrated against measurements at both ends:
#
#     identical frames, and a static screen recording   0.0
#     a slowly drifting gradient (no real change)       1-2
#     heavy synthetic film grain (noise=alls=20)        6-8
#     ---- threshold 8.0 ----
#     a real light-mode dropdown panel opening          9.0
#     a 1080p slide gaining one line of text            55-70
#     a distinct scene cut                              100+
#
# The gap between grain and a real low-contrast UI change is one unit, so this is
# the tightest constant in the file. It errs toward keeping: on a grainier source
# than any of the above dedup simply stops collapsing, which costs nothing in
# tokens (the cap still binds downstream) and loses no information.
DEDUP_THUMB = 16
DEDUP_THRESHOLD = 2.0
DEDUP_PEAK_THRESHOLD = 8.0
SHOWINFO_TS_RE = re.compile(r"pts_time:([0-9.]+)")

# --- ffmpeg frame-sync flag compatibility ------------------------------------
# ffmpeg 5.1 renamed the global -vsync to the per-file -fps_mode, and 9.0 REMOVED
# -vsync outright. -fps_mode does not exist before 5.1, and Ubuntu 22.04 LTS
# still ships 4.4.2 (supported through 2027) — so neither spelling works
# everywhere and we have to ask the local binary which one it takes.
#
# The flag is load-bearing, not cosmetic: without it the image2 muxer runs our
# sparse select/keyframe output through constant-frame-rate sync and writes one
# JPEG per *source* frame instead of one per *selected* frame. Measured on a 9s
# three-cut clip through the real scene filter: 224 JPEGs with no flag, 3 with
# either spelling.
FRAME_SYNC_MODE = "vfr"
_FRAME_SYNC_OPTIONS = ("fps_mode", "vsync")  # preference order: modern first


@functools.lru_cache(maxsize=None)
def _ffmpeg_accepts_option(name: str) -> bool:
    """True if the local ffmpeg recognizes ``-<name> <FRAME_SYNC_MODE>``.

    Runs a ~17ms one-frame null job with the exact flag/value pair we intend to
    emit, so a success proves the pair works end to end.

    On a non-zero exit we deny support only when ffmpeg explicitly named the
    option as unrecognized. Two reasons that beats checking the exit code:
    argument splitting happens before any input is opened, so the message
    appears even on a build without lavfi; and the code itself is not stable
    (unrecognized-option exits 8 on 8.1.1 but 1 on 4.x), which is the exact
    version-coupling this function exists to remove.

    Every other failure is inconclusive and we keep the flag. If it truly were
    unsupported the extraction call fails exactly as it does today, whereas
    guessing "unsupported" would hand an ffmpeg 9 a removed option and
    guarantee the failure this probe exists to prevent.

    Deliberately not a version-banner or ``-h full`` parse: banners are
    unparseable on git and distro builds (``N-109871-g<sha>``,
    ``n4.4.2-0ubuntu0.22.04.1``) and defeated by backports, while ``-h full`` is
    ~1MB of help text whose formatting is not a stable API — and a naive
    ``vsync`` grep there also matches the unrelated ``avsynctest`` filter.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi", "-i", "nullsrc=d=0.04",
        "-frames:v", "1",
        f"-{name}", FRAME_SYNC_MODE,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        # Cannot spawn at all; the callers' shutil.which guard reports it with a
        # far better message than anything we could raise from here.
        return True
    if result.returncode == 0:
        return True
    return f"Unrecognized option '{name}'" not in (result.stderr or "")


@functools.lru_cache(maxsize=None)
def frame_sync_args() -> tuple[str, ...]:
    """The frame-sync argv pair this ffmpeg understands, or ``()`` if neither.

    Prefers ``-fps_mode``. Both call sites run at ``-loglevel info`` because they
    parse ``showinfo`` out of stderr, and at that level ``-vsync`` writes
    "-vsync is deprecated. Use -fps_mode" into the very stream SHOWINFO_TS_RE
    scans. It does not corrupt parsing today, but it is needless coupling and it
    pollutes the stderr we surface in SystemExit messages.

    Memoized per process: watch.py is one process per /watch and this module
    spawns ffmpeg 3-5 times per run, so one probe amortizes away. Deliberately
    not persisted to disk — such a cache goes stale the moment the user upgrades
    ffmpeg, reintroducing the exact fatal mismatch this prevents.

    Returns a tuple rather than a list so the cached value cannot be mutated by
    a caller.
    """
    for name in _FRAME_SYNC_OPTIONS:
        if _ffmpeg_accepts_option(name):
            return (f"-{name}", FRAME_SYNC_MODE)
    # No released ffmpeg lands here. Degrade rather than raise: extraction still
    # succeeds, it just emits one JPEG per source frame. Those duplicates are
    # byte-identical, so dedupe_perceptual (on by default) collapses them and
    # _even_sample enforces the cap. The cost is decode time, not correctness.
    print(
        "[watch] this ffmpeg accepts neither -fps_mode nor -vsync; frame "
        "selection may emit duplicate frames (dedup collapses them)",
        file=sys.stderr,
    )
    return ()


# --- motion mode -------------------------------------------------------------
# Runaway guard only, NOT a token budget. Motion runs are deliberately allowed to
# be expensive — the whole point is to see every frame of a short window. This
# exists so that pointing --motion at a two-hour video fails with a message
# instead of trying to extract 200,000 JPEGs.
MOTION_HARD_MAX = 2000
# Image-token ceiling for an unattended --motion run. MOTION_HARD_MAX bounds the
# JPEG count but not the cost of reading them: at 512px and 16:9 each frame is
# 197 image tokens, so hitting that cap is ~394,000 tokens — and by the time the
# report prints, the frames exist and the model is about to Read every one.
# 75,000 is about 380 frames, or 6s at 60fps / 12s at 30fps, which is generous
# for the one-transition window this mode is documented to want. Past it the run
# is refused rather than warned about, because a warning arrives too late to
# prevent the spend. An explicit --max-frames overrides: the user has then named
# the number and can be assumed to mean it.
MOTION_TOKEN_CEILING = 75_000

# Motion envelope tuning. The old design put a single absolute floor of 6.0 on
# the *per-frame* change, which is what made a cross-dissolve and a light-mode
# fade read as "no change detected": their per-frame change is real but small.
# The floor now sits on the *accumulated* change of one contiguous event, which
# is the quantity "did something happen" was always asking about. See
# :func:`motion_envelope` for the measured table these four numbers come from.
#
# A frame is moving when its change clears MOTION_NOISE_FLOOR — one unit of an
# 8-bit thumbnail cell, i.e. the smallest change that is not rounding — or 2% of
# the loudest frame in the window, whichever is larger. The relative term matters
# on high-contrast content, where a single-frame encoder transient measured 3-5
# against real motion of 190.
#
# 1.0 rather than something safer, measured across every clip whose duration is
# known independently (ms, against the truth in the left column):
#
#     case              truth   fl=1.0   fl=1.5   fl=2.0   fl=2.5
#     fade contrast 23    400      400      266      266     none
#     fade contrast 55    400      400      400      400      383
#     fade contrast 110   400      400      400      400      400
#     fade contrast 222   400      467      400      400      400
#     cubic ease-out      433      433      433      433      433
#     quintic ease-out    350      350      350      350      350
#     linear slide        300      300      300      300      300
#     slide, clipped      283      283      283      283      283
#     3 s pan            2983     2983     2983     2983     2983
#     static             none     none     none     none     none
#     real screen rec      33       33       33       33       33
#
# The trade is one row wide and it is not close. At 1.0 the only miss is contrast
# 222, which over-runs by 67 ms because x264's deblocking ripples around the
# card's edge for four frames after the fade settles — a case the old absolute
# floor already handled. At 1.5 the cost is contrast 23, the light-mode case that
# motivated this whole change, dropping from exact to −33%. Over-reporting a
# high-contrast animation by four frames is a smaller lie than reporting that a
# low-contrast one never happened.
MOTION_NOISE_FLOOR = 1.0
MOTION_REL_FLOOR = 0.02
# An event must accumulate this much total change to be called motion. Same 6.0
# as the old per-frame threshold, deliberately: it is the identical judgement
# about what counts as real, moved to the quantity that can actually carry it.
MOTION_MIN_CHANGE = 6.0
# Stillness shorter than this does not end an event. Whole-pixel quantisation
# makes the tail of an ease-out stutter — measured on a quintic ease-out, the box
# moves 1px, holds 150ms, then moves its last pixel — so a strict "first gap ends
# the animation" rule reported 350ms for a known 500ms move. The allowance grows
# with the event (half its length so far) so a long animation tolerates a longer
# stutter while two distinct 100ms animations 200ms apart stay separate.
MOTION_GAP_MS = 120.0


def parse_frame_rate(value: str | None) -> float | None:
    """Parse ffprobe's rational frame rate. ``"30000/1001"`` -> 29.97.

    Returns None for the several ways ffprobe says "I don't know": ``"0/0"``,
    an empty string, or a missing key.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            denominator = float(den)
            if denominator == 0:
                return None
            rate = float(num) / denominator
        else:
            rate = float(text)
    except ValueError:
        return None
    return rate if rate > 0 else None


def motion_interval(window_seconds: float, source_fps: float | None, cap: int) -> float:
    """Minimum seconds between kept frames; ``0.0`` means keep every source frame.

    Probing first matters on bursty sources. A screen recording that holds a
    frame for 300ms has far fewer real frames than its wall-clock duration
    suggests, so computing ``window/cap`` unconditionally would decimate a clip
    that was never near the cap.
    """
    if window_seconds <= 0 or cap <= 0:
        return 0.0
    if source_fps is None or source_fps <= 0:
        return 0.0
    if source_fps * window_seconds <= cap:
        return 0.0
    return window_seconds / cap


def _cell_deltas(a: bytes, b: bytes) -> tuple[float, float]:
    """``(mean, peak)`` change between two DEDUP_THUMB-square grayscale thumbs.

    Two numbers because they answer different questions and disagree in exactly
    the case that matters. ``mean`` is whole-frame change — good for scene cuts,
    nearly blind to a button sliding across a static background, since the moving
    element occupies one of 256 cells and its contribution is divided by 256.
    ``peak`` is the largest single-cell change, which is precisely that case.

    This is the same blindness that makes dedup destructive on UI animation, so
    reporting only the mean would hand back a signal that says "nothing moved"
    for the motion being measured.
    """
    if not a or len(a) != len(b):
        return 0.0, 0.0
    diffs = [abs(x - y) for x, y in zip(a, b)]
    return sum(diffs) / len(diffs), float(max(diffs))


def measure_motion(extracted: list[dict]) -> list[dict]:
    """Annotate frames with the gap and the change since the previous frame.

    Reuses the thumbnails the dedup pass already knows how to make, so this costs
    one extra ffmpeg call over the JPEGs already on disk and no new decoding of
    the source.

    Also emits ``cum_delta``: change accumulated from the window's first frame, a
    monotonically non-decreasing curve that :func:`motion_envelope` reads. The
    thumbnails only exist inside this function, so this is the one place a
    cumulative signal costs zero extra decoding.

    **Each step is measured two ways and the larger wins**, because neither alone
    covers both kinds of animation:

    - *Change from the previous frame* (``peak_delta``) sees movement. It is blind
      to change slower than one 8-bit step per frame: a 400ms fade whose total
      contrast is 23 units moves ~0.96 units per frame, which rounds to zero. That
      is measured, not hypothetical — every per-frame delta through that fade
      reads 0.0, which is why the old envelope answered "no change detected".
    - *Growth in the distance from the window's first frame* sees exactly that
      case, because the rounding accumulates in the comparison rather than in each
      step: the same fade's distance-from-first climbs cleanly 0 → 29.

    Neither is sufficient. Distance-from-first *saturates* on a moving element:
    once it clears its own footprint the difference against frame 0 stops growing
    (two disjoint silhouettes, forever). Measured on the 300ms slide fixture a
    distance-from-first curve reports 67-133ms, and on a 3s pan 233-267ms, against
    300ms and 2983ms for the accumulated one. Movement needs path length; slow
    in-place change needs distance-from-first. Taking the max of the two per-frame
    contributions gives one curve that is right for both.

    Deliberately stops at "how much changed". Velocity, easing classification and
    object tracking are left to the reader of the frames — the script's job is an
    accurate clock and an honest change signal.
    """
    if not extracted:
        return []
    thumbs = _thumb_frames([Path(f["path"]) for f in extracted])
    have_thumbs = len(thumbs) == len(extracted)
    out: list[dict] = []
    cumulative = 0.0
    from_first_prev = 0.0
    for i, frame in enumerate(extracted):
        prev_t = extracted[i - 1]["timestamp_seconds"] if i else None
        mean = peak = step = 0.0
        if have_thumbs and i:
            mean, peak = _cell_deltas(thumbs[i], thumbs[i - 1])
            _, from_first = _cell_deltas(thumbs[i], thumbs[0])
            # The growth term can go negative when an element returns towards its
            # starting appearance; `peak` is never negative, so max() clamps it.
            step = max(peak, from_first - from_first_prev)
            from_first_prev = from_first
        # Accumulate the unrounded value; rounding each term first would drift by
        # up to 0.05 per frame, which over a 2000-frame motion run is a bigger
        # error than the whole signal on a low-contrast fade.
        cumulative += step
        out.append({
            **frame,
            "gap_ms": None if prev_t is None else round((frame["timestamp_seconds"] - prev_t) * 1000, 1),
            "mean_delta": round(mean, 3),
            "peak_delta": round(peak, 1),
            # 0.0 on the fail-open path (no thumbnails) rather than absent, so
            # consumers never have to distinguish "no change" from "no key".
            "cum_delta": round(cumulative, 1),
        })
    return out


def _cumulative_curve(measured: list[dict]) -> list[float]:
    """The monotone change curve for ``measured``, in thumbnail-cell units.

    Prefers the ``cum_delta`` :func:`measure_motion` computed, which is the only
    version that can see change smaller than one 8-bit step per frame. Falls back
    to accumulating ``peak_delta`` when that key is absent, so this function stays
    total: callers hand-building a ``measured`` list (and one test that does) get
    the same shape of answer rather than a KeyError.
    """
    if measured and all("cum_delta" in f for f in measured):
        return [float(f["cum_delta"] or 0.0) for f in measured]
    curve: list[float] = []
    running = 0.0
    for i, f in enumerate(measured):
        if i:
            running += float(f.get("peak_delta") or 0.0)
        curve.append(running)
    return curve


def motion_envelope(
    measured: list[dict],
    noise_floor: float = MOTION_NOISE_FLOOR,
    min_change: float = MOTION_MIN_CHANGE,
    gap_ms: float = MOTION_GAP_MS,
) -> dict:
    """When the window's largest change event starts, stops, and peaks.

    The old rule flagged every frame whose ``peak_delta`` cleared an absolute 6.0
    and called the span between the first and last of them the animation. That
    floor is the single cause of four separate wrong answers, because it asks each
    frame to be loud on its own:

    ===========================  ==========  =========  ========
    case                         truth       old        new
    ===========================  ==========  =========  ========
    cubic ease-out               500 ms      433 ms*    500 ms
    quintic ease-out             500 ms      333 ms*    500 ms
    light-mode fade, contrast 23 400 ms      *none*     400 ms
    cross-dissolve               1000 ms     *none*     1000 ms
    300 ms slide, clipped window 300 ms      283 ms     283 ms + flags
    linear slide                 300 ms      300 ms     300 ms
    3 s pan                      3000 ms     2983 ms    2983 ms
    static clip                  none        none       none
    ===========================  ==========  =========  ========

    \\* the eased rows are the audit's geometry; on this repo's fixtures the old
    rule happens to land on 500 ms too, because a one-pixel step there measures
    almost exactly 6.0. That coincidence is the point — the answer depended on how
    big the moving element was, not on how long it moved.

    The rule now: a frame is *moving* when its change clears a small floor; runs of
    moving frames are *events*; the event that accumulates the most change is the
    animation; and it is real only if it accumulates ``min_change`` in total. The
    6.0 judgement survives, moved from each frame to the event as a whole — which
    is the quantity that can carry a slow fade.

    Only the largest event is reported, so a window containing a hard cut *and* a
    dissolve describes the cut. That is why the docs tell you to keep ``--start`` /
    ``--end`` tight around one transition; ``clipped_start`` / ``clipped_end`` say
    when the window itself, rather than the motion, decided an endpoint.
    """
    empty = {
        "first_motion": None,
        "last_motion": None,
        "duration_ms": None,
        "peak_at": None,
        "peak_delta": None,
        "total_change": 0.0,
        "event_change": 0.0,
        "floor": None,
        # The number the verdict was actually decided against. Reporting `floor`
        # next to `event_change` compared two quantities that are never compared
        # with each other, and read literally it said the change had cleared the
        # bar and been rejected anyway.
        "min_change": min_change,
        # Same key set on both branches. The two return shapes used to differ (4
        # keys vs 6), and that asymmetry leaked straight into motion.json.
        "clipped_start": False,
        "clipped_end": False,
    }
    if len(measured) < 2:
        return empty

    curve = _cumulative_curve(measured)
    times = [float(f["timestamp_seconds"]) for f in measured]
    steps = [0.0] + [max(0.0, curve[i] - curve[i - 1]) for i in range(1, len(curve))]
    empty["total_change"] = round(curve[-1], 1)
    floor = max(noise_floor, max(steps) * MOTION_REL_FLOOR)
    empty["floor"] = round(floor, 2)

    # The gap between two *adjacent samples* is not stillness — it is just the
    # sampling period. Subtracting it is what makes the rule work at any frame
    # rate: without it, a run thinned to one frame per 200ms could never merge
    # anything (200ms > the 120ms allowance), so 1800ms of continuous motion
    # reported as 200ms — one frame gap — no matter how long it really ran.
    periods = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
    period_ms = periods[len(periods) // 2] * 1000.0

    # [first index, last index, change accumulated] per contiguous event.
    events: list[list] = []
    for i in range(1, len(measured)):
        if steps[i] < floor:
            continue
        if events:
            still_ms = (times[i] - times[events[-1][1]]) * 1000.0 - period_ms
            span = (times[events[-1][1]] - times[max(0, events[-1][0] - 1)]) * 1000.0
            if still_ms <= max(gap_ms, 0.5 * span):
                events[-1][1] = i
                events[-1][2] += steps[i]
                continue
        events.append([i, i, steps[i]])

    if not events:
        return empty
    best = max(events, key=lambda e: e[2])
    if best[2] < min_change:
        # Report what the loudest candidate actually managed, so "no change
        # detected" can be checked against a number instead of trusted.
        empty["event_change"] = round(best[2], 1)
        return empty

    # A delta describes the change between frame i-1 and frame i, so the first
    # frame that *shows* change is one frame after motion actually began. Report
    # the preceding frame as the start, or every measured duration is one frame
    # period short — 283ms for a known 300ms slide at 60fps.
    start_index = max(0, best[0] - 1)
    end_index = best[1]
    peak_index = max(range(best[0], end_index + 1), key=lambda i: steps[i])
    return {
        "first_motion": measured[start_index]["timestamp_seconds"],
        "last_motion": measured[end_index]["timestamp_seconds"],
        "duration_ms": round((times[end_index] - times[start_index]) * 1000, 1),
        "peak_at": measured[peak_index]["timestamp_seconds"],
        "peak_delta": round(steps[peak_index], 1),
        "total_change": round(curve[-1], 1),
        "event_change": round(best[2], 1),
        "floor": round(floor, 2),
        "min_change": min_change,
        # The window, not the motion, chose this endpoint: the reported duration
        # is a lower bound. Keyed on the first *moving* index, not on the backed-up
        # start: a first moving frame at index 1 describes the change from frame 0,
        # which could have begun before the window opened, but one at index 2
        # proves frame 0 -> frame 1 was still, so the window does contain the
        # start and the measurement is exact. Keying on start_index instead
        # flagged that second case too, printing a "lower bound" caveat over an
        # exact answer.
        "clipped_start": best[0] <= 1,
        "clipped_end": end_index == len(measured) - 1,
    }


def extract_motion(
    video_path: str,
    out_dir: Path,
    start_seconds: float,
    end_seconds: float,
    resolution: int = 512,
    max_frames: int = MOTION_HARD_MAX,
    source_fps: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[list[dict], dict]:
    """Extract the source's own frames across a window, with measured timestamps.

    Built for measuring motion — how long a transition takes, what its easing
    curve looks like — which needs two things the other engines cannot give.

    **No resampling.** The other dense path (``extract``) uses ``-vf fps=N``, a
    constant-frame-rate resampler: for each output slot it duplicates or drops a
    source frame and labels the copy with the *slot* time rather than the time of
    the pixels. On a screen recording that holds a frame between changes, that is
    catastrophic. Measured on a 48-frame clip with 317ms holds:

        -vf fps=60, label = i/fps     174 JPEGs, 126 duplicates, 304ms max error
        -vf fps=avg_frame_rate         48 JPEGs,  30 duplicates, 279ms max error
        select + measured pts          48 JPEGs,   0 duplicates,  ~0ms error

    Note the middle row: matching the resample rate to the source's average rate
    does not fix it, because an average is meaningless on a bursty source. The
    fix is to not resample at all.

    **No dedup.** There is deliberately no ``dedup`` parameter, so a caller
    cannot re-enable it by accident. ``dedupe_perceptual`` compares 16x16
    grayscale thumbnails against the last *kept* frame, which makes it a
    motion-dependent resampler: it emits a frame only once enough pixels have
    changed. On a moving-bar clip it collapsed 180 frames to 15, and the
    survivors landed 350/267/200/200/183ms apart — it deletes precisely the slow
    ends of an ease curve, which is the shape being measured.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    window = max(0.0, end_seconds - start_seconds)
    cap = max(1, min(max_frames, MOTION_HARD_MAX))
    interval = motion_interval(window, source_fps, cap)

    # ffmpeg's documented "one frame every N seconds" idiom. interval=0 passes
    # every source frame, so native capture and decimation share one code path.
    select = (
        rf"select='isnan(prev_selected_t)+gte(t-prev_selected_t\,{interval:.6f})'"
    )
    # select BEFORE scale, so frames we discard are never scaled or encoded;
    # showinfo LAST, so it reports only what actually gets written.
    vf = f"{select},{_crop_filter(crop)}{_scale_filter(resolution)},showinfo"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",          # showinfo writes at info level
        "-y",
        "-ss", f"{start_seconds:.3f}",
        "-to", f"{end_seconds:.3f}",
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
    ]
    # Load-bearing, not an optimization. Without it the image2 muxer expands the
    # selection back to constant frame rate and the JPEG list desyncs from the
    # showinfo list: measured 188 JPEGs against 48 stamps on the same clip.
    cmd += frame_sync_args()
    # Deliberately no -frames:v. That flag stops ffmpeg after N frames, which
    # truncates the tail of the window rather than thinning across it.
    cmd += ["-q:v", "4", str(out_dir / "frame_%04d.jpg")]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    files = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0 and not files:
        raise SystemExit(f"ffmpeg motion extraction failed: {result.stderr.strip()}")
    if not files:
        raise SystemExit(
            f"motion extraction produced no frames for "
            f"{format_time_ms(start_seconds)}-{format_time_ms(end_seconds)}"
        )

    timestamps = [round(float(m.group(1)), 6) for m in SHOWINFO_TS_RE.finditer(result.stderr)]
    if len(timestamps) != len(files):
        # Never paper over this with the `timestamps[i] if i < len(...)` idiom the
        # other engines use — a mislabeled motion frame is a wrong measurement
        # presented as a right one.
        raise SystemExit(
            f"motion extraction desynced: {len(files)} frames written but "
            f"{len(timestamps)} timestamps reported. This usually means ffmpeg "
            f"rejected the frame-sync flag {frame_sync_args() or '(none available)'} "
            "and re-expanded the selection to constant frame rate."
        )

    offset = start_seconds or 0.0
    candidates = [
        {
            "index": i,
            "timestamp_seconds": round(offset + ts, 3),
            "path": str(path),
            "reason": "motion",
        }
        for i, (path, ts) in enumerate(zip(files, timestamps))
    ]

    candidate_count = len(candidates)
    # Net for a bad probe (an unknown or wrong source_fps). Even-samples across
    # the window rather than dropping the tail.
    selected = _even_sample(candidates, cap) if candidate_count > cap else candidates

    stamps = [f["timestamp_seconds"] for f in selected]
    gaps = [round((b - a) * 1000) for a, b in zip(stamps, stamps[1:])]
    span = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
    return selected, {
        "engine": "motion",
        "candidate_count": candidate_count,
        "selected_count": len(selected),
        "deduped_count": 0,
        "fallback": False,
        "window": (round(start_seconds, 3), round(end_seconds, 3)),
        "interval": interval,
        "source_fps": source_fps,
        "sampled_fps": round((len(stamps) - 1) / span, 2) if span > 0 else None,
        "min_gap_ms": min(gaps) if gaps else None,
        "max_gap_ms": max(gaps) if gaps else None,
        "even_sampled": candidate_count > cap,
        "cap": cap,
        "crop": crop,
    }


def frame_dimensions(
    width: int | None,
    height: int | None,
    resolution: int,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[int, int] | None:
    """The pixel size of an extracted frame, or None if the source size is unknown.

    Mirrors :func:`_scale_filter` and :func:`_crop_filter` — crop first, then
    scale down to ``resolution`` wide without upscaling, clamped to
    MAX_READ_DIMENSION tall. Kept next to them so the two cannot drift: this is
    what the token estimate is computed from, and an estimate derived from
    different arithmetic than the extractor uses is worse than none.
    """
    if crop is not None:
        _, _, width, height = crop
    if not width or not height:
        return None
    out_w = min(resolution, width)
    out_h = min(MAX_READ_DIMENSION, max(1, round(height * out_w / width)))
    if out_h == MAX_READ_DIMENSION and height * out_w / width > MAX_READ_DIMENSION:
        out_w = max(1, round(width * MAX_READ_DIMENSION / height))
    return out_w, out_h


def image_tokens(width: int, height: int) -> int:
    """Anthropic's image-token estimate for one frame: ``(w x h) / 750``.

    A 512px-wide 16:9 frame is 512x288 = 197 tokens. Worth stating in one place
    because the whole point of the guard below is to put a real number in front
    of a decision, and the number the docs used to quote was 3-5x too high.
    """
    return int(width * height / 750)


def _scale_filter(resolution: int) -> str:
    return (
        f"scale=w='min({resolution},iw)':h='min({MAX_READ_DIMENSION},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``x,y,w,h`` in source pixels into a crop rect.

    Source coordinates, not scaled ones — the user reads them off the video, and
    the scale factor is an implementation detail they should not have to know.
    """
    if not value:
        return None
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 4:
        raise SystemExit(
            f"--crop expects x,y,w,h in source pixels (got {value!r})"
        )
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise SystemExit(f"--crop values must be whole pixels (got {value!r})")
    if w <= 0 or h <= 0:
        raise SystemExit(f"--crop width and height must be positive (got {w}x{h})")
    if x < 0 or y < 0:
        raise SystemExit(f"--crop x and y must be non-negative (got {x},{y})")
    return x, y, w, h


def validate_crop(
    crop: tuple[int, int, int, int] | None,
    width: int | None,
    height: int | None,
) -> tuple[int, int, int, int] | None:
    """Check a crop fits inside the source. A clear error beats an ffmpeg trace."""
    if crop is None or not width or not height:
        return crop
    x, y, w, h = crop
    if x + w > width or y + h > height:
        raise SystemExit(
            f"--crop {x},{y},{w},{h} extends past the {width}x{height} source "
            f"(needs {x + w}x{y + h}). Check the rect against the video's own resolution."
        )
    return crop


def _crop_filter(crop: tuple[int, int, int, int] | None) -> str:
    """Crop clause for a filter chain, or empty. Always precedes scale, so the
    region fills the output frame instead of being a few pixels inside it."""
    if crop is None:
        return ""
    x, y, w, h = crop
    return f"crop={w}:{h}:{x}:{y},"


def _clamp_fps(fps: float, duration_seconds: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration_seconds))))
    return fps, target


def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_time_ms(seconds: float) -> str:
    """``MM:SS.mmm`` (``H:MM:SS.mmm`` past an hour) — format_time's clock, one order finer.

    Not a competing formatter: same origin, same field layout, same hour rule, so
    ``format_time`` is exactly this value rounded to the second. A test pins that.

    Used only where several frames can land inside one second — cue frames and
    motion frames. Everything else stays whole-second on purpose. ``format_time``
    is shared with ``transcribe.format_transcript`` so the frame and transcript
    clocks agree; a caption cue is a ±1s truth and rendering it as ``[00:12.317]``
    would be precision the source does not have.

    Why this matters: ``format_time`` *rounds*, so at 50ms sampling a frame at
    t=0.55 prints ``00:01`` while the one at t=0.50 prints ``00:00`` — putting the
    implied boundary a frame away from where the change actually is. Read off a
    report like that, a 300ms transition becomes an unanswerable question.

    Milliseconds rather than centiseconds: 120fps content collides at two
    decimals, and ffmpeg's ``pts_time`` and the ``-ss`` argv are already 3-decimal.
    """
    # Integer milliseconds throughout, so .9996 carries into the next second
    # instead of rendering as ".1000".
    total_ms = int(round(seconds * 1000))
    return f"{format_time(total_ms // 1000)}.{total_ms % 1000:03d}"


def get_metadata(video_path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(Path(video_path).resolve()),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        # Used by motion mode to decide whether the window fits under the cap at
        # native rate. Never used as a resample rate — see extract_motion.
        "fps": parse_frame_rate(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
        "has_audio": audio_stream is not None,
        # ffmpeg opens a still image through a demuxer named "<codec>_pipe"
        # (png_pipe, jpeg_pipe, webp_pipe …), which is the one reliable way to
        # tell "a picture" from "a video whose container forgot to write a
        # duration". Both report duration 0 and a real width/height, so codec or
        # duration alone would misclassify one of them.
        "format_name": fmt.get("format_name"),
    }


def auto_fps(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps that targets a sensible frame budget for full-video scans."""
    if duration_seconds <= 0:
        return 1.0, 1

    if duration_seconds <= 30:
        target = min(max_frames, max(12, int(round(duration_seconds))))
    elif duration_seconds <= 60:
        target = min(max_frames, 40)
    elif duration_seconds <= 180:  # 3 min
        target = min(max_frames, 60)
    elif duration_seconds <= 600:  # 10 min
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def auto_fps_focus(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges — they are zooming in for detail."""
    if duration_seconds <= 0:
        return min(MAX_FPS, 2.0), 2

    if duration_seconds <= 5:
        target = min(max_frames, max(10, int(round(duration_seconds * 6))))
    elif duration_seconds <= 15:
        target = min(max_frames, max(30, int(round(duration_seconds * 4))))
    elif duration_seconds <= 30:
        target = min(max_frames, 60)
    elif duration_seconds <= 60:
        target = min(max_frames, 80)
    elif duration_seconds <= 180:
        target = max_frames
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def extract(
    video_path: str,
    out_dir: Path,
    fps: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    # -ss before -i = fast seek (keyframe-snap, good enough for preview frames).
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", f"fps={fps},{_crop_filter(crop)}{_scale_filter(resolution)}",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            # 3 decimals like every other engine, but note what this number is:
            # `i / fps` is where the sampler *asked* for a frame, not where the
            # returned pixels sit. `-vf fps=N` is a constant-frame-rate resampler
            # that snaps each output slot to the nearest source frame, so on a
            # source slower than the sample rate a label can be early by up to
            # half a sampling period. The finer rounding does not fix that — it
            # stops the label being quantised to 10ms on top of it.
            "timestamp_seconds": round(offset + (i / fps if fps > 0 else 0.0), 3),
            "path": str(p),
            "reason": "uniform",
        }
        for i, p in enumerate(frames)
    ]


def extract_scene_candidates(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    max_frames: int | None = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    threshold: float = SCENE_THRESHOLD,
) -> list[dict]:
    """Extract first frame plus ffmpeg scene-change frames.

    When ``max_frames`` is set, ``-frames:v`` lets ffmpeg stop decoding once it
    has emitted that many frames (early exit) and avoids writing extras that we
    would only delete afterwards. ``None`` (uncapped "complete" detail) keeps
    every detected shot, as the user explicitly opted in.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",
    ]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    vf = f"select='eq(n\\,0)+gt(scene\\,{threshold})',{_crop_filter(crop)}{_scale_filter(resolution)},showinfo"
    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
    ]
    cmd += frame_sync_args()
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [
        "-q:v", "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg scene extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    # 3 decimals, not 2. These come from ffmpeg's own pts_time, so they are
    # measured to the millisecond — rounding to a centisecond threw that away and
    # collided outright above 100fps. Fast cutting lives in the 0.3-1.0s band,
    # which whole seconds cannot represent at all.
    timestamps = [round(offset + float(match.group(1)), 3) for match in SHOWINFO_TS_RE.finditer(result.stderr)]
    frames = sorted(out_dir.glob("frame_*.jpg"))
    out: list[dict] = []
    for i, path in enumerate(frames):
        ts = timestamps[i] if i < len(timestamps) else offset
        out.append({
            "index": i,
            "timestamp_seconds": ts,
            "path": str(path),
            "reason": "first-frame" if i == 0 else "scene-change",
        })
    return out


def _even_indices(count: int, n: int) -> list[int]:
    """Indices of ``n`` evenly-spaced items out of ``count`` (first + last kept).

    ``n >= count`` returns every index; ``n == 1`` returns just the first.
    """
    if n >= count:
        return list(range(count))
    if n <= 1:
        return [0]
    return [round(i * (count - 1) / (n - 1)) for i in range(n)]


def parse_timestamps(value: str | None) -> list[float]:
    """Parse a comma-separated list of times (SS, MM:SS, HH:MM:SS) into a
    sorted, de-duplicated list of seconds. Empty/blank tokens are skipped;
    an unparseable token raises (via :func:`parse_time`)."""
    if not value:
        return []
    out: list[float] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        seconds = parse_time(token)
        if seconds is not None:
            out.append(float(seconds))
    return sorted(set(out))


def merge_frames(primary: list[dict], pinned: list[dict]) -> list[dict]:
    """Combine two frame lists into one chronological list and reindex 0..n-1.

    ``pinned`` frames (transcript cues) are never dropped — this is a plain
    union, so the cap is enforced upstream by reserving budget for the cues.
    """
    merged = sorted([*primary, *pinned], key=lambda f: f["timestamp_seconds"])
    for i, frame in enumerate(merged):
        frame["index"] = i
    return merged


def extract_at_timestamps(
    video_path: str,
    out_dir: Path,
    timestamps: list[float],
    resolution: int = 512,
    max_frames: int | None = None,
    start_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    end_seconds: float | None = None,
    reason: str = "transcript-cue",
    prefix: str = "cue",
) -> tuple[list[dict], dict]:
    """Grab exactly one frame at each requested timestamp (transcript cues).

    Timestamps are absolute source seconds. Any falling outside an active
    ``[start, end]`` focus window are dropped. Files use a ``<prefix>_*.jpg``
    name so they sit alongside detail-engine ``frame_*.jpg`` output without
    either clobbering the other. When more cues than ``max_frames`` survive, they
    are even-sampled (first + last kept) before extraction.

    ``reason`` and ``prefix`` exist for :func:`_fill_time_gaps`, which wants the
    same "one frame at each of these exact times" behaviour under a different
    label and a different filename. Both have to move together: this function
    deletes every ``<prefix>_*.jpg`` in ``out_dir`` before it starts, so a second
    caller sharing the prefix would delete the first caller's frames — and in a
    /watch run the transcript cues are already on disk by the time the detail
    engine runs.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob(f"{prefix}_*.jpg"):
        existing.unlink()

    lo = start_seconds or 0.0
    hi = end_seconds if end_seconds is not None else float("inf")
    # 3 decimals: these are precision-targeted frames rendered as MM:SS.mmm,
    # so rounding the request to 10ms would quantise below one frame period
    # at 120fps and show up as a label that disagrees with the argv.
    requested = sorted(set(round(float(t), 3) for t in timestamps))
    in_window = [t for t in requested if lo <= t <= hi]
    dropped = len(requested) - len(in_window)

    if max_frames is not None and len(in_window) > max_frames:
        points = [in_window[i] for i in _even_indices(len(in_window), max_frames)]
    else:
        points = in_window

    out: list[dict] = []
    for t in points:
        path = out_dir / f"{prefix}_{len(out):04d}.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{t:.3f}",
            "-i", str(Path(video_path).resolve()),
            "-frames:v", "1",
            "-vf", f"{_crop_filter(crop)}{_scale_filter(resolution)}",
            "-q:v", "4",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0 and path.exists():
            out.append({
                "index": len(out),
                "timestamp_seconds": t,
                "path": str(path),
                "reason": reason,
            })

    meta = {
        "engine": "timestamps",
        "candidate_count": len(requested),
        "selected_count": len(out),
        "dropped_out_of_window": dropped,
        "fallback": False,
    }
    return out, meta


def _even_sample(candidates: list[dict], n: int) -> list[dict]:
    """Pick ``n`` evenly-spaced candidates (always including first and last),
    delete the JPEGs we drop, and reindex the survivors 0..len-1.

    Shared by every capped engine so all detail modes sample the same way:
    detect all candidates across the full range, then thin down to the cap.
    ``n >= len(candidates)`` keeps everything (the uncapped / under-cap case).
    """
    selected = [candidates[i] for i in _even_indices(len(candidates), n)]

    keep_paths = {sel["path"] for sel in selected}
    for cand in candidates:
        if cand["path"] not in keep_paths:
            try:
                Path(cand["path"]).unlink()
            except OSError:
                pass
    for i, frame in enumerate(selected):
        frame["index"] = i
    return selected


def _frame_delta(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference (0-255) between two grayscale
    thumbnails. Mismatched lengths are treated as maximally different so a
    decode hiccup never collapses distinct frames."""
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _thumb_frames(paths: list[Path]) -> list[bytes]:
    """Decode every frame in ``paths`` to a small grayscale thumbnail via one
    ffmpeg pass over the JPEG sequence.

    ffmpeg does the pixel decode (keeps us pure-stdlib); we slice the raw
    grayscale stream into one ``DEDUP_THUMB``-square thumbnail per frame.
    Fail-open: any ffmpeg error, an unrecognized name, or a byte-count mismatch
    returns ``[]`` so the caller skips dedup rather than breaking extraction.
    """
    if not paths:
        return []
    paths = [Path(p) for p in paths]
    m = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", paths[0].name)
    if m is None:
        return []
    prefix, digits, ext = m.group(1), m.group(2), m.group(3)

    # A numbered %0Nd pattern only works while the sequence is contiguous: the
    # image2 demuxer stops at the first index that is missing. _even_sample
    # deletes the frames it did not select, so after it runs the survivors are
    # frame_0001, frame_0021, frame_0041… and the demuxer reads exactly one of
    # them. The byte-count check below then fails and this returns [] — which
    # fails open for dedup, but for measure_motion means every delta is 0.0 and
    # the report says "no change detected" about a clip that plainly moves.
    # Measured on a 3s 60fps pan thinned to 10 frames: 0 thumbnails, max
    # peak_delta 0.0, envelope None.
    #
    # Glob reads whatever is actually on disk, in lexicographic order, which for
    # zero-padded names is chronological. It is not supported by Windows ffmpeg
    # builds, so it is used only when the sequence really has holes — the
    # contiguous case keeps the portable path, and on Windows the holed case
    # degrades to today's behaviour rather than to something worse.
    numbers = []
    for path in paths:
        match = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", path.name)
        if match is None or match.group(1) != prefix:
            return []
        numbers.append(int(match.group(2)))
    contiguous = numbers == list(range(numbers[0], numbers[0] + len(numbers)))

    if contiguous:
        source = [
            "-start_number", str(numbers[0]),
            "-i", str(paths[0].parent / f"{prefix}%0{len(digits)}d{ext}"),
        ]
    else:
        source = [
            "-pattern_type", "glob",
            "-i", str(paths[0].parent / f"{prefix}*{ext}"),
        ]

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        *source,
        "-vf", f"scale={DEDUP_THUMB}:{DEDUP_THUMB},format=gray",
        "-f", "rawvideo",
        "-",
    ]
    # Intentionally no text=/encoding= here, unlike every other call in this
    # module: stdout is raw grayscale pixel data that gets sliced by byte
    # offset below. Decoding it as text would silently corrupt dedup.
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return []

    chunk = DEDUP_THUMB * DEDUP_THUMB
    data = result.stdout
    if len(data) != chunk * len(paths):
        return []
    return [data[i * chunk:(i + 1) * chunk] for i in range(len(paths))]


def dedupe_perceptual(
    candidates: list[dict],
    threshold: float = DEDUP_THRESHOLD,
    peak_threshold: float = DEDUP_PEAK_THRESHOLD,
) -> tuple[list[dict], int]:
    """Drop near-identical frames from a chronological candidate list.

    Thumbnails the extracted JPEGs and greedily removes frames that are within
    *both* ``threshold`` mean per-pixel difference and ``peak_threshold``
    largest-single-cell difference of the last kept one. Returns
    ``(survivors, dropped_count)``; a no-op (unchanged list) when thumbnails are
    unavailable or there are fewer than two candidates.
    """
    if len(candidates) <= 1:
        return candidates, 0
    thumbs = _thumb_frames([Path(c["path"]) for c in candidates])
    return _dedupe_by_deltas(candidates, thumbs, threshold, peak_threshold)


def _is_near_duplicate(
    thumb: bytes, last: bytes, threshold: float, peak_threshold: float
) -> bool:
    """Whether ``thumb`` is close enough to ``last`` to throw away.

    Both tests must agree it is a duplicate. The mean answers "did the picture
    change" and the peak answers "did anything in it change", and only the second
    one can see a control changing state on an otherwise identical screen.

    The mismatch case is handled here rather than left to ``_cell_deltas``,
    because the two helpers fail in opposite directions: ``_frame_delta`` returns
    ``inf`` on ragged input, which keeps the frame, while ``_cell_deltas`` returns
    ``(0.0, 0.0)``, which would silently *delete* it. Deleting frames because a
    decode hiccup made the thumbnails ragged is exactly the failure the fail-open
    contract exists to prevent.
    """
    if not thumb or len(thumb) != len(last):
        return False
    mean, peak = _cell_deltas(thumb, last)
    return mean <= threshold and peak <= peak_threshold


def _dedupe_by_deltas(
    candidates: list[dict],
    thumbs: list[bytes],
    threshold: float = DEDUP_THRESHOLD,
    peak_threshold: float = DEDUP_PEAK_THRESHOLD,
) -> tuple[list[dict], int]:
    """Greedily drop frames that are near-duplicates of the last *kept* frame —
    within ``threshold`` on the mean per-pixel difference *and* within
    ``peak_threshold`` on the largest single-cell difference. Deletes dropped
    JPEGs and reindexes survivors 0..n-1 (same cleanup contract as
    :func:`_even_sample`). Fail-open: if ``thumbs`` does not line up 1:1 with
    ``candidates``, return them unchanged.
    """
    if len(thumbs) != len(candidates) or len(candidates) <= 1:
        return candidates, 0

    kept = [candidates[0]]
    last = thumbs[0]
    dropped: list[dict] = []
    for cand, thumb in zip(candidates[1:], thumbs[1:]):
        if _is_near_duplicate(thumb, last, threshold, peak_threshold):
            dropped.append(cand)
        else:
            kept.append(cand)
            last = thumb

    for cand in dropped:
        try:
            Path(cand["path"]).unlink()
        except OSError:
            pass
    for i, frame in enumerate(kept):
        frame["index"] = i
    return kept, len(dropped)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile of an already-sorted list.

    Nearest-rank rather than interpolating: every value here is a real shot
    length, and reporting an interpolated 0.47s that no shot actually had would
    be inventing a measurement in a report whose whole problem was inventing
    measurements.
    """
    index = min(len(sorted_values) - 1, max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[index]


def shot_stats(
    timestamps: list[float],
    window_start: float = 0.0,
    window_end: float | None = None,
) -> dict:
    """Cut count and shot-length distribution from the full detected-cut list.

    Every cut is timestamped during detection and then most of them are thrown
    away — ``_even_sample`` keeps ~100 frames *by list index* and unlinks the
    rest along with their times. Reading the surviving gaps as if they were shot
    lengths is what made the report 12x wrong: on a clip cutting every 0.5s (840
    cuts over 600s) the kept frames sit ~6s apart, implying ~10 cuts/min against
    a true 84. The numbers were already in memory; they were being discarded a
    line before anyone could use them.

    ``window_end`` is required for both numbers to be true, and leaving it out is
    its own version of the same bug. Without it:

    - the rate divides by the span between the first and last *cut*, so a clip
      that cuts eight times in its first four seconds and then holds a card for
      twenty-six more reports 120 cuts/min against a true 16;
    - the closing shot — last cut to end of clip — is not a gap between two
      stamps, so it never enters the distribution at all. On the 12-minute
      fixture whose final shot runs 250s, ``longest`` came back as 200s.

    Both were caught by review, not by the tests, because every clip the tests
    measured was uniformly cut and ended on a cut.

    Computed from the candidates *before* dedup, so this is what the detector
    found. Dedup can then drop a cut between two visually similar shots, which is
    why the caller reports the dedup count next to these.
    """
    stamps = sorted(timestamps)
    empty = {
        "cuts": 0, "per_minute": None, "median_s": None, "p10_s": None,
        "p90_s": None, "shortest_s": None, "longest_s": None, "cut_times": [],
    }
    if len(stamps) < 2:
        return empty
    boundaries = list(stamps)
    # The closing shot runs from the last cut to the end of the window. Only
    # appended when the end is known and actually past the last cut; a caller
    # that cannot supply it gets the old, short distribution rather than a
    # fabricated one.
    if window_end is not None and window_end > stamps[-1]:
        boundaries.append(window_end)
    durations = sorted(round(b - a, 3) for a, b in zip(boundaries, boundaries[1:]))
    # The first candidate is the unconditional first frame, not a cut.
    cuts = len(stamps) - 1
    # Rate over the window the detector actually looked at. Not the span between
    # cuts: that denominator silently excludes the final shot, and the longer
    # that shot is the more the rate is inflated.
    span = (window_end - window_start) if window_end is not None else (stamps[-1] - stamps[0])
    return {
        "cuts": cuts,
        "per_minute": round(cuts / (span / 60.0), 1) if span > 0 else None,
        "median_s": _percentile(durations, 0.5),
        "p10_s": _percentile(durations, 0.10),
        "p90_s": _percentile(durations, 0.90),
        "shortest_s": durations[0],
        "longest_s": durations[-1],
        "cut_times": [round(t, 3) for t in stamps[1:]],
    }


def _gap_fill_points(
    stamps: list[float], end_seconds: float, budget: int, min_gap: float
) -> list[float]:
    """Times to add so the biggest holes in ``stamps`` get covered first.

    Repeatedly bisects whichever interval is currently widest, which spreads
    ``budget`` frames to minimise the worst remaining gap rather than sprinkling
    them evenly. On the 12-shot / 12-minute fixture that turns a worst gap of
    250 s into one of ~9 s for the same 100-frame cap.

    The trailing interval — last frame to the end of the clip — is included, and
    on that fixture it *is* the worst one: the final shot runs 470 s to 720 s and
    the scene engine represents it with a single frame at 470.
    """
    if not stamps or budget <= 0:
        return []
    segments = [(a, b) for a, b in zip(stamps, stamps[1:])]
    if end_seconds > stamps[-1]:
        segments.append((stamps[-1], end_seconds))

    points: list[float] = []
    while len(points) < budget:
        segments.sort(key=lambda s: s[1] - s[0], reverse=True)
        start, stop = segments[0]
        if stop - start <= min_gap:
            break
        middle = (start + stop) / 2
        points.append(round(middle, 3))
        segments[0:1] = [(start, middle), (middle, stop)]
    return sorted(points)


def _fill_time_gaps(
    video_path: str,
    out_dir: Path,
    selected: list[dict],
    budget: int,
    resolution: int,
    crop: tuple[int, int, int, int] | None,
    start_seconds: float | None,
    end_seconds: float | None,
    min_gap: float = GAP_FILL_MIN_SECONDS,
    window_end: float | None = None,
) -> tuple[list[dict], int]:
    """Spend leftover frame budget on the widest holes in the coverage.

    Scene detection returns one frame per shot and stops, so a clip with few
    shots leaves most of the cap unused — measured on a 12-minute, 12-shot clip:
    "12 selected from 12 candidates (scene, cap 100)", one frame per 60 s, with a
    250-second closing shot represented by one frame. The same clip at
    ``--detail efficient`` returned 50 frames, i.e. the cheap mode beat the
    default. This spends the remaining 88.

    Runs *after* :func:`_even_sample`, which is not a style choice: that function
    unlinks every candidate it does not select, so frames added before it would
    be thinned straight back out. New frames are decoded at the chosen times
    rather than recovered from the discarded candidates for the same reason —
    by then those JPEGs are gone.
    """
    if budget <= 0 or not selected:
        return selected, 0

    stamps = [f["timestamp_seconds"] for f in selected]
    # The caller probes this once and shares it with shot_stats; falling back to
    # the last frame just means the trailing gap goes unfilled, which is safe.
    end = window_end if window_end is not None else (end_seconds or stamps[-1])
    points = _gap_fill_points(stamps, end, budget, min_gap)
    if not points:
        return selected, 0

    fills, _meta = extract_at_timestamps(
        video_path,
        out_dir,
        points,
        resolution=resolution,
        max_frames=None,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        crop=crop,
        reason="gap-fill",
        prefix="fill",
    )
    if not fills:
        return selected, 0
    return merge_frames(selected, fills), len(fills)


def extract_scene_or_uniform(
    video_path: str,
    out_dir: Path,
    fps: float,
    target_frames: int,
    resolution: int = 512,
    max_frames: int | None = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    dedup: bool = True,
    scene_threshold: float = SCENE_THRESHOLD,
) -> tuple[list[dict], dict]:
    """Prefer scene selection, falling back to uniform only when the video is
    effectively static (fewer than ``SCENE_MIN_FRAMES`` detected shots).

    Scene cuts are detected across the *whole* range (uncapped), near-identical
    frames are dropped (:func:`dedupe_perceptual`, unless ``dedup`` is False),
    and the survivors are even-sampled down to ``max_frames`` via
    :func:`_even_sample`, exactly like the keyframe engine. This costs a full
    decode, but it guarantees coverage spans the entire clip — capping detection
    with ``-frames:v`` instead would keep only the first ``max_frames`` cuts and
    drop the tail of long videos (and could even fall below ``SCENE_MIN_FRAMES``
    and misfire the uniform fallback on a cut-heavy clip).
    """
    scene_frames = extract_scene_candidates(
        video_path,
        out_dir,
        resolution=resolution,
        max_frames=None,
        crop=crop,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        threshold=scene_threshold,
    )
    scene_count = len(scene_frames)
    if scene_count >= SCENE_MIN_FRAMES:
        # Probed once and shared: both the shot statistics and the gap-fill need
        # to know where the window ends, and neither is correct without it.
        window_start = start_seconds or 0.0
        window_end = end_seconds
        if window_end is None:
            try:
                window_end = get_metadata(video_path)["duration_seconds"] or None
            except SystemExit:
                window_end = None
        # Read the shot list now: dedup and _even_sample below both delete frames
        # and their timestamps go with them.
        shots = shot_stats(
            [f["timestamp_seconds"] for f in scene_frames], window_start, window_end
        )
        deduped, n_dropped = dedupe_perceptual(scene_frames) if dedup else (scene_frames, 0)
        cap = len(deduped) if max_frames is None else max_frames
        selected = _even_sample(deduped, cap)
        # Uncapped detail still fills gaps, against the duration budget rather
        # than a cap. Skipping it there made `token-burner` — the maximum-fidelity
        # mode the report's own long-video warning recommends — return *fewer*
        # frames than `balanced`: measured 12 against 100 on a 12-minute clip,
        # because balanced topped up to its cap and token-burner stopped at the
        # detected shots. Uncapped means "keep every shot", not "cover less".
        fill_target = max_frames if max_frames is not None else target_frames
        n_filled = 0
        if fill_target is not None:
            selected, n_filled = _fill_time_gaps(
                video_path,
                out_dir,
                selected,
                budget=fill_target - len(selected),
                resolution=resolution,
                crop=crop,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                window_end=window_end,
            )
        return selected, {
            "engine": "scene",
            "candidate_count": scene_count,
            "deduped_count": n_dropped,
            "selected_count": len(selected),
            "gap_filled": n_filled,
            # Independent of which frames survived — that is the point.
            "shots": shots,
            "fallback": False,
            # The cap this engine actually enforced, and the duration budget it
            # actually consumed. Reported rather than recomputed by the caller
            # because `target_frames`/`fps` reach only the uniform fallback: the
            # scene path ignores both, so a caller printing them would describe a
            # code path that did not run.
            "effective_cap": max_frames,
            "budget": None,
        }

    fallback_cap = target_frames if max_frames is None else min(max_frames, target_frames)
    frames = extract(
        video_path,
        out_dir,
        fps=fps,
        resolution=resolution,
        max_frames=fallback_cap,
        crop=crop,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    # Candidates are what dedup ran against — the uniformly sampled frames, not
    # the handful of scene cuts that failed the SCENE_MIN_FRAMES test and sent us
    # here. Reporting `scene_count` made the line self-contradicting ("1 selected
    # from 1 candidates, 59 near-duplicates dropped"). The shot count that
    # triggered the fallback is kept separately.
    sampled_count = len(frames)
    n_dropped = 0
    if dedup:
        frames, n_dropped = dedupe_perceptual(frames)
    return frames, {
        "engine": "uniform",
        "candidate_count": sampled_count,
        "scene_count": scene_count,
        "deduped_count": n_dropped,
        "selected_count": len(frames),
        "fallback": True,
        # Uncapped detail modes still land a real cap here: `fallback_cap` is
        # `target_frames` when `max_frames is None`, so token-burner's uniform
        # fallback tops out at the duration budget (<=100) despite the mode
        # being advertised as uncapped. Report the number that was enforced.
        "effective_cap": fallback_cap,
        "budget": target_frames,
    }


def extract_keyframes(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    max_frames: int | None = 50,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    dedup: bool = True,
) -> tuple[list[dict], dict]:
    """Decode only keyframes (I-frames) — the cheap, near-instant tier.

    ``-skip_frame nokey`` makes ffmpeg reconstruct only keyframes, skipping all
    P/B frames. Encoders emit keyframes at scene cuts, so these already
    approximate "distinct moments". Near-identical frames are dropped
    (:func:`dedupe_perceptual`, unless ``dedup`` is False); over-cap →
    even-sample first→last; too few keyframes → uniform fallback.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",
    ]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-skip_frame", "nokey",
        "-i", str(Path(video_path).resolve()),
        "-vf", f"{_crop_filter(crop)}{_scale_filter(resolution)},showinfo",
    ]
    cmd += frame_sync_args()
    cmd += [
        "-q:v", "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # A range containing no keyframe (e.g. --start past the only one on a static
    # screen recording) starves the mjpeg encoder, so ffmpeg fails at encoder
    # init instead of exiting 0 with no output — which made the
    # `len(candidates) < KEYFRAME_MIN` fallback below unreachable in exactly the
    # case it was written for, while `balanced` handled the same window fine.
    #
    # Gate on "produced no files" rather than on ffmpeg's wording: the message
    # is itself version-coupled (8.x says "Could not open encoder before EOF",
    # older builds said "No filtered frames for output stream"). The glob is
    # trustworthy because every frame_*.jpg in out_dir was deleted above, and
    # cue frames use a cue_*.jpg prefix — so a non-empty glob provably means
    # this invocation wrote something. Partial output still raises, unchanged.
    files = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0:
        if files:
            raise SystemExit(f"ffmpeg keyframe extraction failed: {result.stderr.strip()}")
        detail = (result.stderr or "").strip().splitlines()
        print(
            "[watch] no keyframes decoded in range — falling back to uniform "
            f"sampling ({detail[-1] if detail else 'no ffmpeg output'})",
            file=sys.stderr,
        )

    offset = start_seconds or 0.0
    # 3 decimals, as in the scene engine: measured pts, so keep the millisecond.
    timestamps = [round(offset + float(m.group(1)), 3) for m in SHOWINFO_TS_RE.finditer(result.stderr)]
    candidates: list[dict] = []
    for i, path in enumerate(files):
        ts = timestamps[i] if i < len(timestamps) else offset
        candidates.append({
            "index": i,
            "timestamp_seconds": ts,
            "path": str(path),
            "reason": "keyframe",
        })

    # Too few keyframes → uniform fallback over the same range.
    if len(candidates) < KEYFRAME_MIN:
        for cand in candidates:
            try:
                Path(cand["path"]).unlink()
            except OSError:
                pass
        meta = get_metadata(video_path)
        full_duration = meta["duration_seconds"]
        eff_start = start_seconds or 0.0
        eff_end = end_seconds if end_seconds is not None else full_duration
        eff_duration = max(0.0, eff_end - eff_start)
        budget = max_frames if max_frames is not None else 100
        fps, _ = auto_fps(eff_duration, max_frames=budget)
        frames_out = extract(
            video_path,
            out_dir,
            fps=fps,
            resolution=resolution,
            max_frames=budget,
            crop=crop,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        # As in the scene fallback: candidates are the uniform samples dedup
        # actually compared, not the too-few keyframes that triggered this path.
        sampled_count = len(frames_out)
        n_dropped = 0
        if dedup:
            frames_out, n_dropped = dedupe_perceptual(frames_out)
        return frames_out, {
            "engine": "uniform",
            "candidate_count": sampled_count,
            "keyframe_count": len(candidates),
            "deduped_count": n_dropped,
            "selected_count": len(frames_out),
            "fallback": True,
            # This fallback computes its own budget from `max_frames` (line
            # above) — the caller's duration target never reaches it.
            "effective_cap": budget,
            "budget": budget,
        }

    # Detect-all, drop near-duplicates, then even-sample down to the cap (first +
    # last always kept). ``max_frames is None`` (uncapped) keeps every keyframe.
    candidate_count = len(candidates)
    deduped, n_dropped = dedupe_perceptual(candidates) if dedup else (candidates, 0)
    cap = len(deduped) if max_frames is None else max_frames
    selected = _even_sample(deduped, cap)
    return selected, {
        "engine": "keyframe",
        "candidate_count": candidate_count,
        "deduped_count": n_dropped,
        "selected_count": len(selected),
        "fallback": False,
        "effective_cap": max_frames,
        "budget": None,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: frames.py <video-path> <out-dir> [--fps F] [--resolution W] "
            "[--max-frames N] [--start T] [--end T] [--no-dedup]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    out = Path(sys.argv[2])
    args = sys.argv[3:]

    fps_override = None
    resolution = 512
    max_frames = 100
    start_arg = None
    end_arg = None
    dedup = True
    i = 0
    while i < len(args):
        if args[i] == "--fps":
            fps_override = float(args[i + 1]); i += 2
        elif args[i] == "--resolution":
            resolution = int(args[i + 1]); i += 2
        elif args[i] == "--max-frames":
            max_frames = int(args[i + 1]); i += 2
        elif args[i] == "--start":
            start_arg = args[i + 1]; i += 2
        elif args[i] == "--end":
            end_arg = args[i + 1]; i += 2
        elif args[i] == "--no-dedup":
            dedup = False; i += 1
        else:
            i += 1

    meta = get_metadata(video)
    start_sec = parse_time(start_arg)
    end_sec = parse_time(end_arg)
    full_duration = meta["duration_seconds"]

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)

    focused = start_sec is not None or end_sec is not None
    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=max_frames)
    else:
        fps, target = auto_fps(effective_duration, max_frames=max_frames)
    if fps_override is not None:
        fps = fps_override
        target = max(1, int(round(fps * effective_duration)))

    frames = extract(
        video, out,
        fps=fps,
        resolution=resolution,
        max_frames=max_frames,
        start_seconds=start_sec,
        end_seconds=end_sec,
    )
    deduped_count = 0
    if dedup:
        frames, deduped_count = dedupe_perceptual(frames)
    print(json.dumps(
        {
            "meta": meta, "fps": fps, "target": target, "focused": focused,
            "deduped_count": deduped_count, "frames": frames,
        },
        indent=2,
    ))
