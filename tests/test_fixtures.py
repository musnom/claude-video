"""Smoke test: the ffmpeg fixtures actually produce playable clips.

Beyond "it plays": the animated fixtures carry a known curve in their pixels, and
the tests below read that curve back out. A fixture nobody verifies is a fixture
that can quietly stop testing what it claims — the linear-ramp and frozen-box
failure modes here both produce a perfectly playable clip.
"""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from conftest import (
    EASED_BOX_W,
    EASED_FPS,
    EASED_SPAN,
    EASED_START,
    EASED_TRAVEL,
    EASED_W,
    EASED_X0,
    DISSOLVE_CODED_A,
    DISSOLVE_CODED_B,
    DISSOLVE_CODED_C,
    DISSOLVE_CUT_T,
    DISSOLVE_DURATION,
    DISSOLVE_FADE_END,
    DISSOLVE_FADE_START,
    DISSOLVE_FPS,
    DISSOLVE_GRAY_A,
    DISSOLVE_GRAY_B,
    DISSOLVE_GRAY_C,
    DISSOLVE_H,
    DISSOLVE_W,
    GRAPHIC_CUT_TIMES,
    GRAPHIC_FPS,
    GRAPHIC_H,
    GRAPHIC_PUNCH_SCALE,
    GRAPHIC_W,
    LOWCONTRAST_CARD,
    LOWCONTRAST_CARD_XY,
    LOWCONTRAST_CONTRASTS,
    LOWCONTRAST_FADE_FRAMES,
    LOWCONTRAST_FPS,
    LOWCONTRAST_PLATEAU,
    LOWCONTRAST_SIZE,
    SPARSE_RGB,
    SPARSE_SHOTS,
    eased_box_left,
    eased_box_track,
    eased_position,
    graphic_bar_edges,
    dissolve_change_signal,
    dissolve_frame_index,
    dissolve_luma,
    dissolve_spread,
    dissolve_thumbs,
    card_bbox,
    card_luma_series,
    patch_luma_series,
    thumb_deltas,
    frame_rgb_at,
    graphic_luma_frame,
    scene_change_times,
    scene_cut_times,
    scene_scores,
    sparse_shot_bounds,
)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def test_cut_clip_builds(cut_clip: Path):
    assert cut_clip.exists() and cut_clip.stat().st_size > 0
    assert _duration(cut_clip) > 4.0  # 14 * 0.4s ≈ 5.6s


def test_static_clip_builds(static_clip: Path):
    assert static_clip.exists() and static_clip.stat().st_size > 0
    assert _duration(static_clip) > 2.0


# --- eased-motion fixtures -----------------------------------------------------

# The pipeline's own read-size filter, so the JPEG the helper is tested against
# is the size the extractor would really have written.
_SCALE_512 = (
    "scale=w='min(512,iw)':h='min(1998,ih)':"
    "force_original_aspect_ratio=decrease:force_divisible_by=2"
)


def _extract_frame(clip: Path, index: int, dest: Path) -> Path:
    """Write source frame ``index`` as a 512-wide JPEG, the way /watch would."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(clip),
         "-vf", rf"select='eq(n\,{index})',{_SCALE_512}",
         "-frames:v", "1", "-q:v", "4", str(dest)],
        check=True, capture_output=True,
    )
    return dest


def _last_moving_frame(track) -> int:
    """Index of the last frame whose rendered position differs from the one before."""
    return max(i for i in range(1, len(track)) if track[i][1] != track[i - 1][1])


@pytest.fixture(scope="module")
def cubic_track(eased_clip_cubic: Path):
    return eased_box_track(eased_clip_cubic)


@pytest.fixture(scope="module")
def quintic_track(eased_clip_quintic: Path):
    return eased_box_track(eased_clip_quintic)


def test_eased_clips_build(eased_clip_cubic: Path, eased_clip_quintic: Path):
    for clip in (eased_clip_cubic, eased_clip_quintic):
        assert clip.exists() and clip.stat().st_size > 0
        assert 2.4 < _duration(clip) < 2.6


@pytest.mark.parametrize("name,exponent", [("cubic_track", 3), ("quintic_track", 5)])
def test_eased_position_at_t105_matches_the_curve_not_a_line(name, exponent, request):
    """The headline assertion: at u=0.1 the eased box is where the *curve* says.

    u=0.1 is chosen because the three candidate answers are far apart — cubic
    p=0.271 (x=208.4), quintic p=0.40951 (x=263.8), linear p=0.1 (x=140). A
    builder that quietly rendered a linear ramp, or swapped the exponents, misses
    by 68px or more. The tolerance is 1px because the geq box has hard edges, so
    the rendered left edge is ceil() of the ideal position.
    """
    track = request.getfixturevalue(name)
    t = EASED_START + 0.05
    measured = dict(track)[round(t * EASED_FPS) / EASED_FPS]
    expected = eased_position(t, exponent)
    linear = EASED_X0 + EASED_TRAVEL * (t - EASED_START) / EASED_SPAN

    assert measured is not None
    assert measured == pytest.approx(expected, abs=1.0)
    # ...and nowhere near a line, or the fixture would not be worth having.
    assert abs(measured - linear) > 50


def test_cubic_and_quintic_are_visibly_different(cubic_track, quintic_track):
    """The two exponents must not be interchangeable, or one fixture is dead weight."""
    t = EASED_START + 0.05
    key = round(t * EASED_FPS) / EASED_FPS
    assert dict(quintic_track)[key] - dict(cubic_track)[key] > 40


@pytest.mark.parametrize("name,exponent", [("cubic_track", 3), ("quintic_track", 5)])
def test_every_frame_lands_on_the_easing_curve(name, exponent, request):
    """Not just one sample — all 150 frames, against closed-form arithmetic.

    This is what rules out the drawbox failure mode build_slide_clip warns about
    (position expression evaluated once at init, so the box snaps to its end
    position and never moves): a clip frozen at x=500 fails 86 of 150 frames for
    the cubic and 81 for the quintic, one frozen at x=100 fails 89 of both.
    """
    track = request.getfixturevalue(name)
    assert len(track) == round(2.5 * EASED_FPS)
    for t, measured in track:
        assert measured is not None, f"box vanished at t={t:.4f}"
        assert measured == pytest.approx(eased_position(t, exponent), abs=1.0), (
            f"t={t:.4f} rendered {measured} want {eased_position(t, exponent):.3f}"
        )


@pytest.mark.parametrize("name", ["cubic_track", "quintic_track"])
def test_holds_before_and_after_the_nominal_window(name, request):
    """Start hold, end hold, monotone in between, and it stays on canvas."""
    track = request.getfixturevalue(name)
    before = [x for t, x in track if t < EASED_START]
    after = [x for t, x in track if t >= EASED_START + EASED_SPAN]
    assert before and set(before) == {float(EASED_X0)}
    assert after and set(after) == {float(EASED_X0 + EASED_TRAVEL)}
    xs = [x for _, x in track]
    assert all(b >= a for a, b in zip(xs, xs[1:])), "position must never go backwards"
    assert max(xs) + EASED_BOX_W <= EASED_W, "box must stay inside a 640px canvas"


@pytest.mark.parametrize(
    "name,exponent,last_frame,observable_ms",
    [("cubic_track", 3, 86, 433.3), ("quintic_track", 5, 81, 350.0)],
)
def test_pixels_stop_moving_before_the_nominal_end(
    name, exponent, last_frame, observable_ms, request
):
    """The whole point of the fixture, pinned as a number.

    Hard geq edges quantise the position to whole source pixels, and an ease-out's
    tail velocity falls below one pixel per frame long before the nominal end: the
    cubic's last *observable* move is at t=1.4333 (433.3ms of a 500ms animation)
    and the quintic's at t=1.3500 (350.0ms). Any tool that reports 500ms for these
    clips is not reading the pixels.

    This is physics, not a tunable. An ease-out's terminal velocity is zero, so
    one frame before the nominal end the quintic advances ~8e-5 px. Antialiased
    edges do not rescue it either: the signal a moving edge produces is
    proportional to its displacement however soft it is, so sub-pixel motion stays
    sub-quantisation. Measured: the envelope reports 433/350 whether its floor is
    6.0, 4.0 or 2.0.

    The expected frame is re-derived from ``eased_position`` rather than hard-coded
    alone, so the constants above document the answer instead of defining it.
    """
    track = request.getfixturevalue(name)
    measured_last = _last_moving_frame(track)
    predicted = max(
        i for i in range(1, len(track))
        if math.ceil(eased_position(i / EASED_FPS, exponent))
        != math.ceil(eased_position((i - 1) / EASED_FPS, exponent))
    )
    assert measured_last == predicted == last_frame
    duration_ms = (measured_last / EASED_FPS - EASED_START) * 1000
    assert duration_ms == pytest.approx(observable_ms, abs=0.1)
    assert duration_ms < EASED_SPAN * 1000, "fixture is pointless if the tail is visible"


def _thumb_peak_deltas(clip: Path, work: Path) -> list[float]:
    """Per-frame max single-cell change through the real pipeline shape.

    512-wide JPEGs, then a 16x16 grayscale thumbnail, then the largest absolute
    cell difference against the previous frame — i.e. ``frames._cell_deltas``'
    ``peak``, reproduced here from ffmpeg calls so tests/test_fixtures.py stays a
    statement about the *clips* and not about frames.py's internals.
    """
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(clip), "-vf", _SCALE_512,
         "-q:v", "4", str(work / "frame_%04d.jpg")],
        check=True, capture_output=True,
    )
    count = len(list(work.glob("frame_*.jpg")))
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-start_number", "1",
         "-i", str(work / "frame_%04d.jpg"),
         "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    cells = 16 * 16
    assert len(raw) == cells * count
    thumbs = [raw[i * cells:(i + 1) * cells] for i in range(count)]
    return [0.0] + [
        float(max(abs(a - b) for a, b in zip(thumbs[i], thumbs[i - 1])))
        for i in range(1, count)
    ]


@pytest.mark.parametrize("clip_name", ["eased_clip_cubic", "eased_clip_quintic"])
def test_the_tail_signal_dies_and_leaves_no_phantom_at_the_nominal_end(
    clip_name, tmp_path, request
):
    """Guards the half-open ``lt(X, x+120)`` bound in build_eased_clip.

    build_slide_clip's inclusive ``lte`` makes the box 121px wide exactly when x
    is an integer, which happens exactly once during motion — at t=1.500, where
    the clamp pins x to 500. That extra column is 1/32 of a thumbnail cell across
    a 195-level contrast and shows up as peak_delta 6.0, sitting right on the
    envelope's old default threshold at exactly the nominal end time. It would
    hand a broken tool the right answer for the wrong reason. With ``lt`` the
    post-motion frames measure exactly 0.0, so the fixture actually asks the
    question it claims to ask.
    """
    clip = request.getfixturevalue(clip_name)
    peaks = _thumb_peak_deltas(clip, tmp_path / "jpg")
    track = eased_box_track(clip)
    last = _last_moving_frame(track)

    # Every frame after the pixels stop moving is dead flat — including t=1.500.
    assert max(peaks[last + 1:]) == 0.0
    assert peaks[round((EASED_START + EASED_SPAN) * EASED_FPS)] == 0.0
    # ...and the final moving frame's signal has already decayed to the threshold.
    assert 0.0 < peaks[last] <= 6.0


def test_box_left_reads_a_downscaled_jpeg_in_source_pixels(
    eased_clip_cubic: Path, tmp_path: Path
):
    """``eased_box_left`` must survive the 640->512 downscale the extractor applies.

    Same frame, two paths: full-res decode of the clip vs. a 512-wide JPEG. They
    have to agree in source pixels or every position assertion made against
    extracted frames is measuring the scale factor instead of the animation.
    """
    index = round((EASED_START + 0.05) * EASED_FPS)
    jpeg = _extract_frame(eased_clip_cubic, index, tmp_path / "frame_0001.jpg")
    from_jpeg = eased_box_left(jpeg)
    from_clip = dict(eased_box_track(eased_clip_cubic))[index / EASED_FPS]
    assert from_jpeg is not None
    assert from_jpeg == pytest.approx(from_clip, abs=2.0)
    assert from_jpeg == pytest.approx(eased_position(index / EASED_FPS, 3), abs=2.0)


# --- graphic-cuts clip --------------------------------------------------------

# Midpoint of each of the 8 shots: 1s into a 2s shot, far enough from either
# boundary that seek rounding cannot land in a neighbour.
_SHOT_MIDPOINTS = (1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0)


def test_graphic_cuts_clip_builds(graphic_cuts_clip: Path):
    assert graphic_cuts_clip.exists() and graphic_cuts_clip.stat().st_size > 0
    stream = json.loads(subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         str(graphic_cuts_clip)],
        capture_output=True, text=True,
    ).stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (GRAPHIC_W, GRAPHIC_H)
    assert stream["r_frame_rate"] == f"{GRAPHIC_FPS}/1"
    assert int(stream["nb_frames"]) == 480          # 8 shots * 2s * 30fps
    assert _duration(graphic_cuts_clip) == 16.0


def test_graphic_cuts_are_invisible_to_a_020_scene_threshold(graphic_cuts_clip: Path):
    """PINS A BUG. This clip has 8 shots and 7 unmistakable cuts. frames.py used
    to run scene detection at SCENE_THRESHOLD = 0.20, and at that threshold
    ffmpeg finds *nothing* — the whole motion-graphics piece reads as one shot.

    The three counts below are the bug's shape, not an arbitrary triple:

        0.20 (old production) -> 0   every cut missed
        0.10                  -> 0   still missed at half the threshold
        0.05                  -> 7   all seven, and only the seven, appear here

    So the cuts are not weak noise that a smaller threshold would drown in;
    they are a real, consistent signal sitting in a band the tool never looked
    at. Driving the production extractor over this clip shows the same thing
    from the other end:

        extract_scene_candidates(threshold=0.20) -> 1 candidate  [0.0]
        extract_scene_candidates(threshold=0.05) -> 8 candidates [0.0, 2.0,
                                                    4.0, ..., 14.0]

    and 1 is under SCENE_MIN_FRAMES (8), so the engine discarded the scene
    result and fell back to uniform sampling — which is how an 8-shot
    infographic ended up described as a single shot. At 0.05 it lands on exactly
    8, the minimum that keeps the scene result. This test deliberately does not
    import frames: it pins ffmpeg's raw behaviour, so it stays valid whatever
    the constants are tuned to.

    Uses select='gt(scene,TH)' WITHOUT the eq(n\\,0) term that
    extract_scene_candidates prepends. That term makes frame 0 an unconditional
    hit, so with it the two zeroes below would read as ones and the failure
    would look like "found 1 shot" instead of "found no cuts".
    """
    assert len(scene_change_times(graphic_cuts_clip, 0.20)) == 0
    assert len(scene_change_times(graphic_cuts_clip, 0.10)) == 0
    assert len(scene_change_times(graphic_cuts_clip, 0.05)) == 7


def test_graphic_cuts_hits_are_the_intended_cuts_and_nothing_else(graphic_cuts_clip: Path):
    """The 7 hits at 0.05 must be the 7 authored cuts.

    Without this, the count above could be satisfied by seven encoder ripples
    anywhere in the clip and the fixture would be pinning noise.
    """
    hits = scene_change_times(graphic_cuts_clip, 0.05)
    assert [round(t, 3) for t in hits] == list(GRAPHIC_CUT_TIMES)


def test_graphic_cuts_scores_sit_in_a_narrow_band_between_the_thresholds(
    graphic_cuts_clip: Path,
):
    """Ground truth per frame, so drift shows up as a number rather than as a
    count flipping. Measured: the six bar-swap cuts at 0.0709-0.0710 and the
    punch-in at 0.0692; every other frame in the clip is under 0.0001.

    The band is asserted at [0.06, 0.085] — tighter than the 0.05/0.10 gate the
    test above enforces — so a change to the graphics that erodes the margin
    fails here first, with a diagnosable value, instead of silently drifting
    until a count assertion breaks.
    """
    scores = dict(scene_scores(graphic_cuts_clip))
    assert len(scores) == 480

    for cut in GRAPHIC_CUT_TIMES:
        score = scores[cut]
        assert 0.06 <= score <= 0.085, f"cut at t={cut} scored {score}"

    noise = max(s for t, s in scores.items() if round(t, 3) not in GRAPHIC_CUT_TIMES)
    assert noise < 0.001, f"non-cut frame scored {noise}"


def test_graphic_cuts_change_layout_not_the_whole_frame(graphic_cuts_clip: Path):
    """Why the metric misses them: each cut repaints under a tenth of the frame.

    A hard cut between two unrelated shots changes essentially every pixel. Here
    the background, title band, axis, footer and legend are byte-identical
    across the cut and only the bars move, which is what a real infographic edit
    looks like — and is exactly the case a mean-absolute-difference metric
    cannot see.
    """
    shots = [graphic_luma_frame(graphic_cuts_clip, t) for t in _SHOT_MIDPOINTS]
    assert all(len(f) == GRAPHIC_W * GRAPHIC_H for f in shots)

    for i in range(1, len(shots)):
        before, after = shots[i - 1], shots[i]
        changed = sum(1 for a, b in zip(before, after) if abs(a - b) > 16)
        fraction = changed / (GRAPHIC_W * GRAPHIC_H)
        # Measured 7.60% for the bar swaps, 9.90% for the punch-in.
        assert 0.03 < fraction < 0.12, f"cut {i} repainted {fraction:.1%} of the frame"

    # Four background probes well clear of any drawn element: identical in all 8
    # shots, so "the background holds still" is asserted, not assumed.
    for offset in (10 * GRAPHIC_W + 10, 700 * GRAPHIC_W + 1270,
                   400 * GRAPHIC_W + 300, 650 * GRAPHIC_W + 640):
        assert len({f[offset] for f in shots}) == 1


def test_graphic_cuts_last_shot_is_a_punch_in(graphic_cuts_clip: Path):
    """The final cut is the same content scaled up, not different content.

    Read out of the pixels: every bar edge on a scanline moves to
    640 + (x - 640) * 1.05, i.e. a 5% zoom about the frame centre. Both
    conditions matter — the same *count* of edges proves nothing was added or
    removed, and the mapping proves it is a zoom rather than a redraw.
    """
    before = graphic_bar_edges(graphic_cuts_clip, 13.0)   # shot 6
    after = graphic_bar_edges(graphic_cuts_clip, 15.0)    # shot 7, punched
    assert len(before) == len(after) == 10                # 5 bars, 2 edges each

    centre = GRAPHIC_W / 2
    for x_before, x_after in zip(before, after):
        expected = centre + (x_before - centre) * GRAPHIC_PUNCH_SCALE
        assert abs(x_after - expected) <= 2, f"{x_before} -> {x_after}, expected {expected}"

    # And the zoom is outward: the outermost edges move away from centre.
    assert after[0] < before[0] and after[-1] > before[-1]


# --- sparse-cuts clip ---------------------------------------------------------


def test_sparse_cuts_clip_is_twelve_minutes(sparse_cuts_clip: Path):
    assert sparse_cuts_clip.exists() and sparse_cuts_clip.stat().st_size > 0
    assert abs(_duration(sparse_cuts_clip) - 720.0) < 1.0


def test_sparse_cuts_clip_has_eleven_detectable_cuts(sparse_cuts_clip: Path):
    """12 shots means 11 cuts — and ffmpeg has to *find* all 11 at the threshold
    the scene engine uses, or the fixture proves nothing about the engine."""
    cuts = scene_cut_times(sparse_cuts_clip)
    expected = [start for start, _, _ in sparse_shot_bounds()[1:]]
    assert len(cuts) == len(SPARSE_SHOTS) - 1 == 11
    assert cuts == [round(t, 3) for t in expected]


def test_sparse_cuts_shot_colors_land_where_intended(sparse_cuts_clip: Path):
    """Read the colour out of the pixels at three points inside every shot.

    Probing at ``start`` and ``end - one frame`` is what pins the boundaries: if
    a shot were a frame long or short, the neighbouring shot's colour would show
    up at one of those two instants.
    """
    for start, end, color in sparse_shot_bounds():
        expected = SPARSE_RGB[color]
        for t in (start, (start + end) / 2, end - 0.1):
            got = frame_rgb_at(sparse_cuts_clip, t)
            err = max(abs(a - b) for a, b in zip(got, expected))
            assert err <= 4, f"{color} at t={t}: got {got}, expected ~{expected}"


def test_sparse_cuts_are_front_loaded(sparse_cuts_clip: Path):
    """The uneven distribution is the point, so assert it rather than trusting
    the table: 6 of the 11 cuts land in the first 40 s (5.5% of the runtime),
    and the tail is one 250-second shot that a single frame cannot represent."""
    cuts = scene_cut_times(sparse_cuts_clip)
    assert sum(1 for t in cuts if t <= 40) == 6
    assert 720 - cuts[-1] == 250
    assert sum(d for _, d in SPARSE_SHOTS) == 720


# --- low-contrast fade clips ---------------------------------------------------
LC_FADE_IN, LC_FADE_OUT = LOWCONTRAST_FADE_FRAMES  # 60 (t=1.000) and 84 (t=1.400)
LC_MOTION_FLOOR = 6.0                              # motion_envelope's default peak-delta floor


def _lc_probe_stream(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         "-count_frames", str(path)],
        capture_output=True, text=True,
    )
    return json.loads(out.stdout)["streams"][0]


@pytest.mark.parametrize("contrast", LOWCONTRAST_CONTRASTS)
def test_lowcontrast_clip_geometry(lowcontrast_clips, contrast):
    """1280x720, exactly 60fps, exactly 120 frames.

    The frame index is the clock for every other assertion in this file, so a
    dropped or duplicated frame would quietly relabel the whole fade window.
    """
    stream = _lc_probe_stream(lowcontrast_clips[contrast])
    assert (stream["width"], stream["height"]) == LOWCONTRAST_SIZE
    assert stream["r_frame_rate"] == f"{LOWCONTRAST_FPS}/1"
    assert int(stream["nb_read_frames"]) == 120


@pytest.mark.parametrize("contrast", LOWCONTRAST_CONTRASTS)
def test_lowcontrast_background_is_flat_white(lowcontrast_clips, contrast):
    """Luma 255, every pixel, every frame, outside the card.

    Without this the change signal measured below could be background noise
    wearing a card's clothes.
    """
    series = patch_luma_series(lowcontrast_clips[contrast], 64, 64, 16, 16)
    assert len(series) == 120
    assert {(low, high) for low, high, _ in series} == {(255, 255)}


@pytest.mark.parametrize("contrast", LOWCONTRAST_CONTRASTS)
def test_lowcontrast_card_is_still_outside_the_fade_window(lowcontrast_clips, contrast):
    """White through t=1.000 inclusive, then held at the plateau to the end.

    Asserts the inclusive boundary frames because that is where an off-by-one in
    fade's start latch would surface: f60 must still be pure background and f84
    must already be fully opaque.
    """
    series = card_luma_series(lowcontrast_clips[contrast])
    plateau = LOWCONTRAST_PLATEAU[contrast]
    assert all(v == 255 for v in series[:LC_FADE_IN + 1]), series[:LC_FADE_IN + 1]
    assert all(v == plateau for v in series[LC_FADE_OUT:]), series[LC_FADE_OUT:]
    # The plateau is the nominal contrast, up to the single level the limited-range
    # yuv round trip costs at contrast 110 (146, not 145).
    assert abs((255 - plateau) - contrast) <= 1


@pytest.mark.parametrize("contrast", LOWCONTRAST_CONTRASTS)
def test_lowcontrast_card_ramps_linearly_in_alpha(lowcontrast_clips, contrast):
    """Every frame of the fade sits on the straight line 255 -> 255-contrast.

    Checks the whole ramp, not just its ends: an eased, stepped or snapped
    animation would still hit both endpoints, and a fixture that silently became
    a two-state cut is exactly the failure this file exists to catch.

    Tolerance 1.5 against a measured worst case of 1.333 (contrast 110, f64).
    That residual is not codec loss — it is identical under ``-crf 0``. It comes
    from the alpha chain: fade computes a 16-bit factor, yuva420p carries an 8-bit
    alpha plane, and overlay blends in integers, so a handful of frames round a
    level away from the ideal line.
    """
    series = card_luma_series(lowcontrast_clips[contrast])
    for frame in range(LC_FADE_IN, LC_FADE_OUT + 1):
        alpha = (frame - LC_FADE_IN) / (LC_FADE_OUT - LC_FADE_IN)
        ideal = 255 - contrast * alpha
        assert abs(series[frame] - ideal) <= 1.5, (frame, series[frame], ideal)
    # Named checkpoints, so a failure reads as a time rather than an index.
    assert series[LC_FADE_IN] == 255                              # t=1.000, alpha 0
    assert abs(series[72] - (255 - contrast / 2)) <= 1.5          # t=1.200, alpha 0.5
    assert series[LC_FADE_OUT] == LOWCONTRAST_PLATEAU[contrast]   # t=1.400, alpha 1
    # Monotone: alpha only ever increases, so luma only ever falls.
    ramp = series[LC_FADE_IN:LC_FADE_OUT + 1]
    assert all(later <= earlier for earlier, later in zip(ramp, ramp[1:])), ramp


@pytest.mark.parametrize("contrast", LOWCONTRAST_CONTRASTS)
def test_lowcontrast_card_is_a_filled_rectangle_in_the_right_place(lowcontrast_clips, contrast):
    """440x300 at (420, 210), read out of the pixels rather than off the filter graph.

    Checked mid-fade as well as at the plateau: the card must appear at full size
    and fade in place, not grow or slide into position. The interior is asserted
    uniform separately because a bounding box alone cannot tell a filled card from
    an outline.
    """
    path = lowcontrast_clips[contrast]
    expected = (*LOWCONTRAST_CARD_XY, *LOWCONTRAST_CARD)
    assert card_bbox(path, 72) == expected           # half faded
    assert card_bbox(path, LC_FADE_OUT) == expected   # t=1.400
    assert card_bbox(path, 119) == expected          # last frame
    # Nothing drawn before the fade starts.
    assert card_bbox(path, LC_FADE_IN) is None

    card_w, card_h = LOWCONTRAST_CARD
    card_x, card_y = LOWCONTRAST_CARD_XY
    inset = 16
    low, high, _ = patch_luma_series(
        path, card_w - 2 * inset, card_h - 2 * inset, card_x + inset, card_y + inset)[-1]
    assert (low, high) == (LOWCONTRAST_PLATEAU[contrast],) * 2


def test_lowcontrast_change_signal_is_below_the_motion_floor(lowcontrast_clips, tmp_path):
    """The reason this fixture exists: 400ms of visible animation that an absolute
    per-frame floor scores as nothing at all.

    Measured through the chain the frame engine really uses (512-wide JPEG at
    ``-q:v 4`` -> 16x16 gray). At contrast 23 the largest single-cell change over
    the whole fade is 2.0 and the largest whole-frame mean is 0.3125, against a
    default floor of 6.0 — and three of the 24 fade frames change by exactly
    nothing, because 23 levels spread over 24 frames is less than one level per
    frame. What does survive is the accumulation: 28.0 summed across the fade.
    """
    deltas = thumb_deltas(lowcontrast_clips[23], tmp_path / "jpg23")
    assert len(deltas) == 120
    fade = deltas[LC_FADE_IN + 1:LC_FADE_OUT + 1]
    still = deltas[1:LC_FADE_IN + 1] + deltas[LC_FADE_OUT + 1:]

    assert max(peak for _, peak in fade) <= 2.0
    assert max(mean for mean, _ in fade) < 0.5
    assert min(peak for _, peak in fade) == 0.0          # frames where nothing registers
    # The animation is really there, it is just tiny — accumulated it is obvious.
    assert sum(peak for _, peak in fade) >= 20.0
    # And nothing at all happens outside the window.
    assert max(peak for _, peak in still) == 0.0
    assert max(mean for mean, _ in still) == 0.0


def test_lowcontrast_signal_scales_with_contrast(lowcontrast_clips, tmp_path):
    """Contrast is a real variable, not a label — and it brackets the floor.

    Peak per-frame change rises monotonically with contrast, and the 6.0 floor
    falls inside the range this fixture spans: contrasts 23 and 55 never reach it
    on any frame, contrast 110 reaches it on 9 of 24 frames, and contrast 222
    clears it on all 24. A threshold picked from this fixture should be read off
    that bracket, not off the still-section noise, which is 0.0 here (see the
    caveat on synthetic flatness).
    """
    peaks = {
        contrast: [peak for _, peak in
                   thumb_deltas(lowcontrast_clips[contrast], tmp_path / f"jpg{contrast}")
                   [LC_FADE_IN + 1:LC_FADE_OUT + 1]]
        for contrast in LOWCONTRAST_CONTRASTS
    }
    highest = [max(peaks[c]) for c in sorted(LOWCONTRAST_CONTRASTS)]
    assert highest == sorted(highest) and len(set(highest)) == len(highest)

    assert max(peaks[23]) < LC_MOTION_FLOOR
    assert max(peaks[55]) < LC_MOTION_FLOOR
    # Straddles: some frames register, most do not.
    assert max(peaks[110]) >= LC_MOTION_FLOOR > min(peaks[110])
    # Clears it outright on every frame of the fade.
    assert min(peaks[222]) >= LC_MOTION_FLOOR

# --- dissolve clip ------------------------------------------------------------

PER_FRAME_FLOOR = 6.0

_CUT_N = dissolve_frame_index(DISSOLVE_CUT_T)          # 45
_FADE_N0 = dissolve_frame_index(DISSOLVE_FADE_START)   # 90, last pure-B frame
_FADE_N1 = dissolve_frame_index(DISSOLVE_FADE_END)     # 120, first pure-C frame


def test_dissolve_clip_shape(dissolve_clip: Path) -> None:
    """640x360, 30fps CFR, 180 frames, 6.000s — the arithmetic every index in
    this module depends on."""
    stream = json.loads(subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams",
         str(dissolve_clip)], capture_output=True, text=True).stdout)["streams"][0]
    assert (stream["width"], stream["height"]) == (DISSOLVE_W, DISSOLVE_H)
    assert stream["r_frame_rate"] == stream["avg_frame_rate"] == f"{DISSOLVE_FPS}/1"
    assert int(stream["nb_frames"]) == int(DISSOLVE_DURATION * DISSOLVE_FPS) == 180
    assert abs(float(stream["duration"]) - DISSOLVE_DURATION) < 0.01


def test_dissolve_scenes_are_three_distinct_solid_fills(dissolve_clip: Path) -> None:
    """The three scenes decode to the declared gray levels, and each is genuinely
    flat — a non-zero spread would mean the "solid" reference this clip measures
    everything against is not solid."""
    thumbs = dissolve_thumbs(dissolve_clip)
    for t, expected in ((0.500, DISSOLVE_GRAY_A),
                        (2.000, DISSOLVE_GRAY_B),
                        (5.000, DISSOLVE_GRAY_C)):
        thumb = thumbs[dissolve_frame_index(t)]
        assert dissolve_spread(thumb) == 0, f"t={t} is not a flat fill"
        assert dissolve_luma(thumb) == expected, f"t={t}"
    # Pairwise separations of 205, 102 and 103 — no two scenes are confusable.
    levels = (DISSOLVE_GRAY_A, DISSOLVE_GRAY_B, DISSOLVE_GRAY_C)
    gaps = [abs(a - b) for i, a in enumerate(levels) for b in levels[i + 1:]]
    assert min(gaps) > 100


def test_hard_cut_is_abrupt_and_nothing_gradual_precedes_it(dissolve_clip: Path) -> None:
    """The whole A->B change lands in one frame pair, and the 44 pairs before it
    are dead flat. This is the half of the clip a per-frame floor gets right."""
    thumbs = dissolve_thumbs(dissolve_clip)
    signal = dissolve_change_signal(thumbs)

    assert dissolve_luma(thumbs[_CUT_N - 1]) == DISSOLVE_GRAY_A
    assert dissolve_luma(thumbs[_CUT_N]) == DISSOLVE_GRAY_B

    _, cut_mean, cut_peak = signal[_CUT_N]
    assert cut_peak == abs(DISSOLVE_GRAY_B - DISSOLVE_GRAY_A) == 205.0
    assert cut_mean == cut_peak            # the change is the entire frame
    assert cut_peak > PER_FRAME_FLOOR * 30

    # Nothing gradual leads in: every pair from frame 1 to the cut is exactly 0,
    # so the cut cannot be mistaken for the tail of a ramp.
    lead_in = [peak for _, _, peak in signal[1:_CUT_N]]
    assert lead_in and max(lead_in) == 0.0


def test_dissolve_endpoints_and_midpoint(dissolve_clip: Path) -> None:
    """Pure scenes either side of the fade window, an even blend in the middle."""
    thumbs = dissolve_thumbs(dissolve_clip)
    assert dissolve_luma(thumbs[dissolve_frame_index(2.900)]) == DISSOLVE_GRAY_B
    assert dissolve_luma(thumbs[dissolve_frame_index(4.100)]) == DISSOLVE_GRAY_C

    midpoint = (DISSOLVE_GRAY_B + DISSOLVE_GRAY_C) / 2       # 185.0
    frame = thumbs[dissolve_frame_index(3.500)]
    assert dissolve_spread(frame) == 0                        # a blend, still flat
    assert abs(dissolve_luma(frame) - midpoint) <= 1.0

    # And the blend is a scene in its own right: 51 units clear of either source,
    # so "approximately the midpoint" is not satisfiable by either endpoint.
    for endpoint in (DISSOLVE_GRAY_B, DISSOLVE_GRAY_C):
        assert abs(dissolve_luma(frame) - endpoint) > 50


def test_dissolve_is_monotone_across_its_window(dissolve_clip: Path) -> None:
    """Strictly decreasing from B to C over the fade, flat on both shoulders."""
    thumbs = dissolve_thumbs(dissolve_clip)
    window = [dissolve_luma(t) for t in thumbs[_FADE_N0:_FADE_N1 + 1]]
    assert len(window) == 31                       # 30 steps over 1.000s
    assert window[0] == DISSOLVE_GRAY_B and window[-1] == DISSOLVE_GRAY_C
    assert all(b < a for a, b in zip(window, window[1:])), window

    # Flat outside the window, so the fade's edges are where the builder says.
    before = [dissolve_luma(t) for t in thumbs[_CUT_N:_FADE_N0 + 1]]
    after = [dissolve_luma(t) for t in thumbs[_FADE_N1:]]
    assert set(before) == {DISSOLVE_GRAY_B}
    assert set(after) == {DISSOLVE_GRAY_C}


def test_still_sections_have_zero_frame_to_frame_change(dissolve_clip: Path) -> None:
    """The zero reference: no phantom change anywhere outside a transition.

    This is what makes every other number in this module trustworthy. The
    dissolve's per-frame signal is only 2-4 units, so an encoder rippling by even
    1 unit on a flat fill would be 25-50% noise on the measurement and would put
    fake motion into the held sections too.

    Worth knowing that this currently passes at *any* quantiser — qp 0 through
    crf 40 give byte-identical thumbnails, because flat fills give x264 nothing
    to ring on. So this test does not today defend the crf choice; it defends the
    *content* staying flat. Give any scene a gradient or texture and this is the
    assertion that fails first.
    """
    signal = dissolve_change_signal(dissolve_thumbs(dissolve_clip))
    still = ([s for s in signal[1:_CUT_N]]
             + [s for s in signal[_CUT_N + 1:_FADE_N0 + 1]]
             + [s for s in signal[_FADE_N1 + 1:]])
    assert len(still) == 148            # 180 frames - frame 0 - 1 cut - 30 fade
    assert max(peak for _, _, peak in still) == 0.0
    assert max(mean for _, mean, _ in still) == 0.0


def test_per_frame_floor_catches_the_cut_and_misses_the_dissolve(dissolve_clip: Path) -> None:
    """The inconsistency, stated as one assertion pair.

    Both transitions repaint the entire frame. The cut does it in one step and
    clears a 6.0 floor by 34x; the dissolve does it in 30 steps and every step
    misses that same floor, even though the dissolve's *total* travel is half the
    cut's. A detector built on per-frame change reports one transition in this
    clip and there are two.
    """
    signal = dissolve_change_signal(dissolve_thumbs(dissolve_clip))
    fade = signal[_FADE_N0 + 1:_FADE_N1 + 1]       # the 30 frames that move
    assert len(fade) == 30

    assert signal[_CUT_N][2] > PER_FRAME_FLOOR
    assert max(peak for _, _, peak in fade) < PER_FRAME_FLOOR
    assert min(peak for _, _, peak in fade) > 0     # it is moving, just slowly

    # The change is real and large — it is only the per-frame view that hides it.
    total = sum(peak for _, _, peak in fade)
    assert total == abs(DISSOLVE_GRAY_B - DISSOLVE_GRAY_C) == 102.0
    assert total > PER_FRAME_FLOOR * 15


def test_ffmpeg_scene_metric_also_misses_the_dissolve(dissolve_clip: Path) -> None:
    """ffmpeg's own detector fails the same way, for a second reason.

    Its score is ``min(mafd, |mafd - prev_mafd|) / 100`` over the coded luma
    plane, so a *steady* ramp scores near zero however steep it is: mafd holds
    flat at 3.0 through the fade, the difference term collapses to 0, and 26 of
    the 30 fade frames report exactly 0.000000. Only 4 register at all — the
    onset at t=3.033, where mafd steps 0 -> 3, and t=3.500 / 3.533 / 4.000, where
    integer rounding makes a step 2 instead of 3. Even the *un-differenced* term
    would only be 0.03, still 6.7x under production's 0.20 threshold, so this
    clip defeats the metric twice over. The cut saturates it at 1.000000.
    """
    assert scene_change_times(dissolve_clip, 0.20) == [DISSOLVE_CUT_T]

    scores = dict(scene_scores(dissolve_clip))
    assert scores[DISSOLVE_CUT_T] == 1.0            # clipped: raw mafd is 1.76
    fade = [s for t, s in scores.items()
            if DISSOLVE_FADE_START < t <= DISSOLVE_FADE_END]
    assert len(fade) == 30
    assert max(fade) <= 0.03
    assert sum(1 for s in fade if s == 0.0) == 26


def test_dissolve_coded_plane_matches_the_documented_levels(dissolve_clip: Path) -> None:
    """The constants describing the coded (limited-range) plane are real.

    ``format=gray`` expands limited-range luma back to full range, so the two
    scales differ by 15-30 units. Both are documented in conftest because the
    scene-metric numbers above live on the coded scale while every other
    assertion here lives on the gray one; this pins the pair so a future edit
    cannot quietly mix them.
    """
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(dissolve_clip),
         "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"], capture_output=True).stdout
    plane = DISSOLVE_W * DISSOLVE_H
    frame_size = plane * 3 // 2

    def coded_luma(index: int) -> set[int]:
        start = index * frame_size
        return set(raw[start:start + plane])

    assert coded_luma(dissolve_frame_index(0.500)) == {DISSOLVE_CODED_A}
    assert coded_luma(dissolve_frame_index(2.000)) == {DISSOLVE_CODED_B}
    assert coded_luma(dissolve_frame_index(5.000)) == {DISSOLVE_CODED_C}
    # The metric's view of the cut: mean |dY| = 176 -> 1.76, clipped to 1.0.
    assert abs(DISSOLVE_CODED_B - DISSOLVE_CODED_A) == 176


# --- sparse-moving clip -------------------------------------------------------
# The gap-fill calibration fixture: static shots with a drifting two-tone marker
# so a fill probe anywhere is distinct from its neighbours while contributing
# nothing to the scene metric. Both halves of that claim are calibrations that
# can drift with encoders, so both are pinned here rather than trusted.


def test_sparse_moving_clip_is_five_minutes(sparse_moving_clip):
    assert sparse_moving_clip.exists() and sparse_moving_clip.stat().st_size > 0
    assert abs(_duration(sparse_moving_clip) - 300.0) < 1.0


def test_sparse_moving_clip_has_exactly_the_authored_cuts(sparse_moving_clip):
    """The 11 authored cuts are found at the production threshold, and the
    marker's drift (including its wrap at t=150) adds NO false cuts."""
    from conftest import sparse_moving_cut_times

    cuts = scene_cut_times(sparse_moving_clip, threshold=0.05)
    assert cuts == [round(t, 3) for t in sparse_moving_cut_times()]


def test_sparse_moving_marker_defeats_the_duplicate_check(sparse_moving_clip, tmp_path):
    """Two frames 1 s apart — the worst-case midpoint-to-bound distance the fill
    loop compares at — must NOT be near-duplicates, or the fixture cannot prove
    the budget-spending path. Probed inside the 60 s magenta shot."""
    import frames

    grabbed, _ = frames.extract_at_timestamps(
        str(sparse_moving_clip), tmp_path, [200.0, 201.0, 230.0],
    )
    assert len(grabbed) == 3
    thumbs = frames._thumb_frames([Path(f["path"]) for f in grabbed])
    assert len(thumbs) == 3
    for a, b in ((0, 1), (1, 2), (0, 2)):
        assert not frames._is_near_duplicate(
            thumbs[a], thumbs[b], frames.DEDUP_THRESHOLD, frames.DEDUP_PEAK_THRESHOLD
        ), f"frames {a},{b} read as duplicates — marker calibration drifted"
