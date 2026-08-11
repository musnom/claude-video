"""Motion mode: source-frame sampling with measured timestamps.

The accuracy tests here assert a frame's *label* against its *content*, using
fixtures whose pixels encode their own clock. That is the only way to catch an
engine that reports a plausible-looking time for the wrong frame — which is
exactly what fps resampling does on a screen recording.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import frames
from conftest import bar_content_time


# --- pure helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("60/1", 60.0),
        ("30000/1001", pytest.approx(29.97, abs=0.01)),
        ("24", 24.0),
        ("0/0", None),
        ("", None),
        (None, None),
        ("garbage", None),
        ("0/1", None),
    ],
)
def test_parse_frame_rate(value, expected):
    assert frames.parse_frame_rate(value) == expected


def test_motion_interval_keeps_every_frame_when_it_fits():
    # 60fps x 2s = 120 frames, cap 200 -> no thinning
    assert frames.motion_interval(2.0, 60.0, 200) == 0.0


def test_motion_interval_thins_when_over_cap():
    # 60fps x 10s = 600 frames, cap 120 -> one frame per 83ms
    assert frames.motion_interval(10.0, 60.0, 120) == pytest.approx(10.0 / 120)


def test_motion_interval_unknown_fps_keeps_everything():
    """No probe means no basis to thin; the cap still nets it downstream."""
    assert frames.motion_interval(5.0, None, 100) == 0.0


def test_motion_interval_degenerate_inputs():
    assert frames.motion_interval(0.0, 60.0, 100) == 0.0
    assert frames.motion_interval(5.0, 60.0, 0) == 0.0


def test_get_metadata_reports_frame_rate(motion_clip: Path):
    assert frames.get_metadata(str(motion_clip))["fps"] == pytest.approx(60.0, abs=0.1)


# --- argv shape ---------------------------------------------------------------


def _capture_argv(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(frames, "frame_sync_args", lambda: ("-SENTINEL", "vfr"))
    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    return calls


def _vf(argv: list[str]) -> str:
    return argv[argv.index("-vf") + 1]


def test_motion_argv_has_no_fps_filter(monkeypatch, tmp_path):
    """The regression guard against reintroducing the resampler.

    `-vf fps=N` is a constant-frame-rate resampler; on a 48-frame clip with
    317ms holds it produced 174 JPEGs, 126 of them duplicates, with 304ms of
    label error. Measured timestamps exist precisely to avoid it.
    """
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):  # no files written by the stub
        frames.extract_motion("v.mp4", tmp_path, 0.0, 2.0)
    assert "fps=" not in _vf(calls[0])


def test_motion_argv_carries_sync_flag(monkeypatch, tmp_path):
    """Without it the muxer CFR-expands and the JPEG list desyncs from showinfo:
    measured 188 files against 48 stamps."""
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        frames.extract_motion("v.mp4", tmp_path, 0.0, 2.0)
    assert "-SENTINEL" in calls[0]


def test_motion_argv_orders_the_filter_chain(monkeypatch, tmp_path):
    """select before scale (skip work on discarded frames), showinfo last."""
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        frames.extract_motion("v.mp4", tmp_path, 0.0, 2.0)
    vf = _vf(calls[0])
    assert vf.index("select=") < vf.index("scale=") < vf.index("showinfo")


def test_motion_argv_omits_frames_v(monkeypatch, tmp_path):
    """-frames:v stops ffmpeg after N frames, truncating the tail of the window
    rather than thinning across it."""
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        frames.extract_motion("v.mp4", tmp_path, 0.0, 2.0)
    assert "-frames:v" not in calls[0]


def test_motion_argv_window_is_three_decimals(monkeypatch, tmp_path):
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        frames.extract_motion("v.mp4", tmp_path, 1.5, 2.25)
    assert calls[0][calls[0].index("-ss") + 1] == "1.500"
    assert calls[0][calls[0].index("-to") + 1] == "2.250"


def test_motion_desync_raises(monkeypatch, tmp_path):
    """A mislabeled motion frame is a wrong measurement presented as a right one,
    so never paper over a count mismatch."""
    def fake_run(cmd, *args, **kwargs):
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        for i in range(5):
            (out / f"frame_{i:04d}.jpg").write_bytes(b"x")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = "pts_time:0.0\npts_time:0.1\n"   # only 2 stamps for 5 files

        return _Result()

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="desynced"):
        frames.extract_motion("v.mp4", tmp_path / "out", 0.0, 2.0)


# --- accuracy, against the pixel clock ----------------------------------------


def _label_errors_ms(extracted: list[dict]) -> list[float]:
    """|label - the time the pixels actually depict|, in ms.

    Skips t<0.05 where the fixture's bar is clipped by the left frame edge.
    """
    out = []
    for frame in extracted:
        content = bar_content_time(frame["path"])
        if content is not None and frame["timestamp_seconds"] > 0.05:
            out.append(abs(content - frame["timestamp_seconds"]) * 1000)
    return out


def test_motion_labels_match_pixels_cfr(motion_clip: Path, tmp_path):
    extracted, _ = frames.extract_motion(
        str(motion_clip), tmp_path, 0.0, 3.0, source_fps=60.0
    )
    errors = _label_errors_ms(extracted)
    assert errors
    assert max(errors) < 10, f"max label error {max(errors):.1f} ms"


def test_motion_labels_match_pixels_vfr(vfr_clip: Path, tmp_path):
    meta = frames.get_metadata(str(vfr_clip))
    extracted, _ = frames.extract_motion(
        str(vfr_clip), tmp_path, 0.0, 3.0, source_fps=meta["fps"]
    )
    errors = _label_errors_ms(extracted)
    assert errors
    assert max(errors) < 10, f"max label error {max(errors):.1f} ms"


def test_fps_resampling_is_much_worse_on_vfr(vfr_clip: Path, tmp_path):
    """An executable statement of why this engine exists.

    The old uniform path resamples to a rate and models timestamps as i/fps.
    On a held-frame source that mislabels by hundreds of milliseconds — larger
    than the UI transitions being measured.
    """
    meta = frames.get_metadata(str(vfr_clip))
    old = frames.extract(
        str(vfr_clip), tmp_path / "old", fps=meta["fps"], max_frames=200,
        start_seconds=0.0, end_seconds=3.0,
    )
    errors = _label_errors_ms(old)
    assert max(errors) > 100, (
        f"expected the resampler to mislabel badly, got {max(errors):.1f} ms — "
        "if this now passes, re-check whether measured PTS is still needed"
    )


def test_motion_keeps_every_source_frame_under_cap(vfr_clip: Path, tmp_path):
    meta = frames.get_metadata(str(vfr_clip))
    extracted, info = frames.extract_motion(
        str(vfr_clip), tmp_path, 0.0, 3.0, source_fps=meta["fps"]
    )
    assert info["interval"] == 0.0
    assert not info["even_sampled"]
    # The holds must survive as real gaps, not be smoothed into a grid.
    assert info["max_gap_ms"] > 200, info
    assert info["min_gap_ms"] < 40, info


def test_motion_does_not_dedup(motion_clip: Path, tmp_path):
    """dedupe_perceptual is a motion-dependent resampler: it emits a frame only
    once enough pixels change, deleting the slow ends of an ease curve."""
    extracted, info = frames.extract_motion(
        str(motion_clip), tmp_path, 0.0, 3.0, source_fps=60.0
    )
    assert info["deduped_count"] == 0
    kept, dropped = frames.dedupe_perceptual(list(extracted))
    assert dropped > 0, "fixture should be dedup-vulnerable, else the test proves nothing"
    assert len(extracted) > len(kept)


def test_motion_labels_are_distinct_within_a_second(motion_clip: Path, tmp_path):
    """At 60fps, whole-second labels would collapse 60 frames onto one string."""
    extracted, _ = frames.extract_motion(
        str(motion_clip), tmp_path, 1.0, 1.5, source_fps=60.0
    )
    stamps = [frames.format_time_ms(f["timestamp_seconds"]) for f in extracted]
    assert len(set(stamps)) == len(stamps)


# --- bounds -------------------------------------------------------------------


def test_motion_never_exceeds_cap(motion_clip: Path, tmp_path):
    extracted, info = frames.extract_motion(
        str(motion_clip), tmp_path, 0.0, 3.0, max_frames=10, source_fps=60.0
    )
    assert len(extracted) <= 10
    assert info["cap"] == 10


def test_motion_over_cap_spans_the_window(motion_clip: Path, tmp_path):
    """Anti-truncation. The old uniform path would return 10 frames covering the
    first 0.16s of a 3s window; thinning must keep the span."""
    extracted, _ = frames.extract_motion(
        str(motion_clip), tmp_path, 0.0, 3.0, max_frames=10, source_fps=60.0
    )
    span = extracted[-1]["timestamp_seconds"] - extracted[0]["timestamp_seconds"]
    assert span > 2.5, f"only spanned {span:.2f}s of a 3s window"


def test_motion_respects_the_window_offset(motion_clip: Path, tmp_path):
    """Labels are absolute source time, and the pixels agree."""
    extracted, _ = frames.extract_motion(
        str(motion_clip), tmp_path, 1.0, 1.5, source_fps=60.0
    )
    assert all(0.99 <= f["timestamp_seconds"] <= 1.51 for f in extracted)
    assert max(_label_errors_ms(extracted)) < 10


def test_motion_hard_max_is_a_runaway_guard_not_a_budget():
    """Deliberately generous: motion runs are allowed to be expensive."""
    assert frames.MOTION_HARD_MAX >= 1000


# --- crop ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0,0,10,10", (0, 0, 10, 10)),
        (" 80 , 60 , 160 , 120 ", (80, 60, 160, 120)),
        (None, None),
        ("", None),
    ],
)
def test_parse_crop(value, expected):
    assert frames.parse_crop(value) == expected


@pytest.mark.parametrize("bad", ["1,2,3", "a,b,c,d", "0,0,0,10", "0,0,10,-1", "-1,0,10,10"])
def test_parse_crop_rejects_bad_input(bad):
    with pytest.raises(SystemExit):
        frames.parse_crop(bad)


def test_validate_crop_rejects_out_of_bounds():
    with pytest.raises(SystemExit, match="extends past"):
        frames.validate_crop((600, 400, 200, 200), 640, 480)


def test_validate_crop_accepts_exact_fit():
    assert frames.validate_crop((0, 0, 640, 480), 640, 480) == (0, 0, 640, 480)


def test_validate_crop_passes_through_unknown_dimensions():
    """No metadata is not a reason to refuse; ffmpeg will complain if it is wrong."""
    assert frames.validate_crop((0, 0, 10, 10), None, None) == (0, 0, 10, 10)


def test_crop_precedes_scale_in_the_chain(monkeypatch, tmp_path):
    """Crop must come first, or the region is scaled down before being isolated."""
    calls = _capture_argv(monkeypatch)
    with pytest.raises(SystemExit):
        frames.extract_motion("v.mp4", tmp_path, 0.0, 1.0, crop=(8, 4, 32, 16))
    vf = _vf(calls[0])
    assert "crop=32:16:8:4" in vf
    assert vf.index("crop=") < vf.index("scale=")


def test_crop_isolates_the_region(tmp_path):
    """A distinctly-coloured box, cropped to exactly its bounds, fills the frame."""
    clip = tmp_path / "box.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=640x480:r=30:d=1",
        "-vf", "drawbox=x=80:y=60:w=160:h=120:color=orange@1:t=fill",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
    ], check=True, capture_output=True)

    extracted, info = frames.extract_motion(
        str(clip), tmp_path / "out", 0.0, 0.2, crop=(80, 60, 160, 120), source_fps=30.0
    )
    assert info["crop"] == (80, 60, 160, 120)
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", extracted[0]["path"], "-vf", "scale=1:1",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    ).stdout
    r, g, b = raw[0], raw[1], raw[2]
    assert r > 200 and 120 < g < 210 and b < 60, f"expected orange, got {(r, g, b)}"


def test_crop_shrinks_the_frame_rather_than_growing_it(tmp_path):
    """The token win: a small region arrives at 1:1 instead of scaled into a
    full-size frame, so it is both more legible and cheaper."""
    clip = tmp_path / "box.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=navy:s=640x480:r=30:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
    ], check=True, capture_output=True)

    def dims(path):
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
        return tuple(int(v) for v in out.split(",")[:2])

    full, _ = frames.extract_motion(str(clip), tmp_path / "a", 0.0, 0.1, source_fps=30.0)
    cropped, _ = frames.extract_motion(
        str(clip), tmp_path / "b", 0.0, 0.1, crop=(80, 60, 160, 120), source_fps=30.0
    )
    fw, fh = dims(full[0]["path"])
    cw, ch = dims(cropped[0]["path"])
    assert (cw, ch) == (160, 120)
    assert cw * ch < fw * fh


# --- motion measurement -------------------------------------------------------
# slide_clip animates from t=1.000 to t=1.300 — a known 300ms envelope.


def test_measure_motion_annotates_gaps_and_change(slide_clip: Path, tmp_path):
    extracted, _ = frames.extract_motion(
        str(slide_clip), tmp_path, 0.0, 2.5, source_fps=60.0
    )
    measured = frames.measure_motion(extracted)
    assert len(measured) == len(extracted)
    assert measured[0]["gap_ms"] is None
    assert all(f["gap_ms"] is not None for f in measured[1:])
    assert all(15 <= f["gap_ms"] <= 18 for f in measured[1:])


def test_peak_delta_catches_what_mean_delta_misses(slide_clip: Path, tmp_path):
    """The reason there are two numbers.

    A 120x60 element on a 640x360 frame occupies a few of 256 thumbnail cells,
    so its movement is divided away by a whole-frame average. Measured: during
    the slide the mean peaks at ~2.7 on a 0-255 scale — indistinguishable from
    noise, and barely above the dedup threshold of 2.0 — while the peak cell
    reads ~116.
    """
    extracted, _ = frames.extract_motion(
        str(slide_clip), tmp_path, 0.0, 2.5, source_fps=60.0
    )
    measured = frames.measure_motion(extracted)
    during = [f for f in measured if 1.0 <= f["timestamp_seconds"] <= 1.32]
    assert during
    assert max(f["mean_delta"] for f in during) < 10, "mean should be nearly blind here"
    assert max(f["peak_delta"] for f in during) > 50, "peak must see the element move"


def test_motion_envelope_recovers_a_known_duration(slide_clip: Path, tmp_path):
    """Ground truth is 1.000 -> 1.300. Must land within one frame period."""
    extracted, _ = frames.extract_motion(
        str(slide_clip), tmp_path, 0.0, 2.5, source_fps=60.0
    )
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["first_motion"] == pytest.approx(1.0, abs=0.02)
    assert env["last_motion"] == pytest.approx(1.3, abs=0.02)
    assert env["duration_ms"] == pytest.approx(300, abs=20)


def test_motion_envelope_reports_nothing_on_a_static_clip(static_clip: Path, tmp_path):
    extracted, _ = frames.extract_motion(
        str(static_clip), tmp_path, 0.0, 2.0, source_fps=10.0
    )
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["first_motion"] is None
    assert env["duration_ms"] is None


def test_envelope_start_is_the_frame_before_the_change():
    """A delta describes i-1 -> i, so the onset belongs to i-1. Without this
    every duration comes out one frame period short."""
    measured = [
        {"timestamp_seconds": 0.0, "peak_delta": 0.0},
        {"timestamp_seconds": 0.1, "peak_delta": 0.0},
        {"timestamp_seconds": 0.2, "peak_delta": 90.0},
        {"timestamp_seconds": 0.3, "peak_delta": 90.0},
        {"timestamp_seconds": 0.4, "peak_delta": 0.0},
    ]
    env = frames.motion_envelope(measured)
    assert env["first_motion"] == 0.1
    assert env["last_motion"] == 0.3
    assert env["duration_ms"] == pytest.approx(200, abs=1)


def test_cell_deltas_handles_mismatched_thumbs():
    assert frames._cell_deltas(b"", b"") == (0.0, 0.0)
    assert frames._cell_deltas(b"\x00\x10", b"\x00") == (0.0, 0.0)


# --- cumulative change --------------------------------------------------------
# The envelope is read off a monotone "how much has changed since the window
# opened" curve rather than off each frame's own delta. These pin the curve's
# shape, because every envelope assertion below is only as good as it is.


def test_cum_delta_is_monotone_and_flat_outside_the_move(slide_clip: Path, tmp_path):
    """Rises only while the box is moving, never falls, and stops when it stops."""
    extracted, _ = frames.extract_motion(
        str(slide_clip), tmp_path, 0.0, 2.5, source_fps=60.0
    )
    measured = frames.measure_motion(extracted)
    cum = [f["cum_delta"] for f in measured]

    assert cum[0] == 0.0
    assert all(b >= a for a, b in zip(cum, cum[1:])), "curve must never fall"

    before = [f["cum_delta"] for f in measured if f["timestamp_seconds"] < 0.98]
    after = [f["cum_delta"] for f in measured if f["timestamp_seconds"] > 1.32]
    assert max(before) == 0.0, "nothing moved before t=1.0"
    assert len(set(after)) == 1, f"curve still climbing after the move: {sorted(set(after))[:5]}"
    # ...and it actually went somewhere in between.
    assert cum[-1] > 100


def test_cum_delta_stays_flat_on_a_static_clip(static_clip: Path, tmp_path):
    extracted, _ = frames.extract_motion(
        str(static_clip), tmp_path, 0.0, 2.0, source_fps=10.0
    )
    measured = frames.measure_motion(extracted)
    assert max(f["cum_delta"] for f in measured) == 0.0


def test_cum_delta_is_present_on_the_no_thumbnail_path(monkeypatch, motion_clip: Path, tmp_path):
    """Fail-open: when thumbnails are unavailable the key is 0.0, never absent.

    _thumb_frames returns [] on any decode hiccup, and a consumer of motion.json
    should not have to tell "no change" apart from "no key".
    """
    extracted, _ = frames.extract_motion(
        str(motion_clip), tmp_path, 1.0, 1.2, source_fps=60.0
    )
    monkeypatch.setattr(frames, "_thumb_frames", lambda paths: [])
    measured = frames.measure_motion(extracted)
    assert all(f["cum_delta"] == 0.0 for f in measured)
    assert frames.motion_envelope(measured)["first_motion"] is None


def test_envelope_returns_the_same_keys_whether_or_not_it_found_motion(
    slide_clip: Path, static_clip: Path, tmp_path
):
    """The two branches used to return 4 keys and 6, and the asymmetry leaked
    straight into motion.json."""
    moving, _ = frames.extract_motion(str(slide_clip), tmp_path / "a", 0.0, 2.5, source_fps=60.0)
    still, _ = frames.extract_motion(str(static_clip), tmp_path / "b", 0.0, 2.0, source_fps=10.0)
    found = frames.motion_envelope(frames.measure_motion(moving))
    none = frames.motion_envelope(frames.measure_motion(still))
    assert set(found) == set(none)
    assert found["first_motion"] is not None and none["first_motion"] is None


# --- eased motion -------------------------------------------------------------


@pytest.mark.parametrize(
    "clip_name,observable_ms",
    [("eased_clip_cubic", 433.0), ("eased_clip_quintic", 350.0)],
)
def test_envelope_recovers_the_observable_duration_of_an_eased_move(
    clip_name, observable_ms, tmp_path, request
):
    """An ease-out's nominal duration is 500ms; its *pixel-observable* duration
    is shorter, and the honest answer is the one in the pixels.

    tests/test_fixtures.py proves the clips stop moving at t=1.4333 (cubic) and
    t=1.3500 (quintic) — the tail velocity falls under one source pixel per frame
    long before the nominal end, so the last 67/150 ms are simply not recorded.
    This asserts the envelope reports what happened rather than the number the
    animation was authored with.
    """
    clip = request.getfixturevalue(clip_name)
    extracted, _ = frames.extract_motion(str(clip), tmp_path, 0.0, 2.5, source_fps=60.0)
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["first_motion"] == pytest.approx(1.0, abs=0.02)
    assert env["duration_ms"] == pytest.approx(observable_ms, abs=20)
    assert env["clipped_start"] is False and env["clipped_end"] is False


def test_eased_and_linear_moves_are_told_apart_by_where_the_change_lands(
    eased_clip_cubic: Path, slide_clip: Path, tmp_path
):
    """The curve's *shape*, not just its span, is what makes easing readable.

    Half of a linear move's change has happened at its midpoint; an ease-out is
    front-loaded, so it passes half by ~20% of the way through. A reader fitting
    an easing curve off motion.json depends on this being true of the data.
    """
    def half_change_fraction(clip, end):
        extracted, _ = frames.extract_motion(str(clip), tmp_path / clip.stem, 1.0, end, source_fps=60.0)
        measured = frames.measure_motion(extracted)
        total = measured[-1]["cum_delta"]
        i = next(i for i, f in enumerate(measured) if f["cum_delta"] >= total / 2)
        return i / (len(measured) - 1)

    assert half_change_fraction(slide_clip, 1.3) == pytest.approx(0.5, abs=0.12)
    assert half_change_fraction(eased_clip_cubic, 1.5) < 0.3


# --- window clipping ----------------------------------------------------------


def test_envelope_flags_a_window_that_clips_the_motion(slide_clip: Path, tmp_path):
    """The docs tell you to use a tight window, and a tight window silently
    truncated the answer: --start 1.00 --end 1.30 on a move that runs exactly
    1.000 -> 1.300 reported 183ms with nothing to say it was cut short."""
    extracted, _ = frames.extract_motion(str(slide_clip), tmp_path, 1.0, 1.3, source_fps=60.0)
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["clipped_start"] is True
    assert env["clipped_end"] is True
    # The reported span is the whole window, i.e. a lower bound on the real move.
    assert env["duration_ms"] == pytest.approx(283, abs=20)


def test_envelope_does_not_cry_clipped_on_a_generous_window(slide_clip: Path, tmp_path):
    extracted, _ = frames.extract_motion(str(slide_clip), tmp_path, 0.5, 2.0, source_fps=60.0)
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["clipped_start"] is False
    assert env["clipped_end"] is False
    assert env["duration_ms"] == pytest.approx(300, abs=20)


# --- low-contrast fades -------------------------------------------------------
# The headline fix. A 400ms card fade-in on white is plainly visible to a person
# and, at low contrast, completely invisible to a per-frame change threshold:
# 23 luma levels spread over 24 frames is under one level per frame, so three of
# those frames register exactly 0.0. Accumulating is the only way to see it.


@pytest.mark.parametrize("contrast", [23, 55, 110, 222])
def test_envelope_recovers_a_low_contrast_fade(lowcontrast_clips, contrast, tmp_path):
    """Ground truth is 1.000 -> 1.400 at every contrast.

    Contrasts 23 and 55 used to report "no change detected above the noise
    floor"; 110 reported 334ms. Contrast 222 now over-runs to ~467ms because
    x264's deblocking ripples around the card edge for a few frames after the
    fade settles — see MOTION_NOISE_FLOOR for the measured trade that buys.
    """
    tolerance = 20 if contrast != 222 else 70
    extracted, _ = frames.extract_motion(
        str(lowcontrast_clips[contrast]), tmp_path, 0.0, 2.0, source_fps=60.0
    )
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["first_motion"] == pytest.approx(1.0, abs=0.02)
    assert env["duration_ms"] == pytest.approx(400, abs=tolerance)


def test_the_faintest_fade_is_invisible_frame_to_frame(lowcontrast_clips, tmp_path):
    """Executable statement of why the metric had to change.

    Not "the old threshold was a bit high" — at contrast 23 the per-frame signal
    is below one 8-bit quantisation step, so no per-frame floor above zero can
    ever see this animation. What survives is the accumulation.
    """
    extracted, _ = frames.extract_motion(
        str(lowcontrast_clips[23]), tmp_path, 0.0, 2.0, source_fps=60.0
    )
    measured = frames.measure_motion(extracted)
    during = [f for f in measured if 1.0 < f["timestamp_seconds"] <= 1.4]

    assert max(f["peak_delta"] for f in during) <= 2.0
    assert min(f["peak_delta"] for f in during) == 0.0, "frames where nothing registers"
    # The old rule, verbatim, finds nothing at all.
    assert not [f for f in measured if f["peak_delta"] >= 6.0]
    # The cumulative curve is unambiguous over the same frames.
    assert measured[-1]["cum_delta"] >= 20.0


def test_a_still_clip_is_not_dragged_over_the_line_by_the_lower_floor(
    static_clip: Path, lowcontrast_clips, tmp_path
):
    """The risk of accumulating: given enough frames, noise adds up to anything.

    Guarded two ways — a genuinely static clip, and the still tail of a fade clip
    after its animation has finished.
    """
    still, _ = frames.extract_motion(str(static_clip), tmp_path / "a", 0.0, 2.0, source_fps=10.0)
    assert frames.motion_envelope(frames.measure_motion(still))["first_motion"] is None

    tail, _ = frames.extract_motion(
        str(lowcontrast_clips[23]), tmp_path / "b", 1.5, 2.0, source_fps=60.0
    )
    assert frames.motion_envelope(frames.measure_motion(tail))["first_motion"] is None


# --- gradual transitions ------------------------------------------------------


def test_envelope_measures_a_cross_dissolve(dissolve_clip: Path, tmp_path):
    """"How long is this cross-dissolve" routes straight here, and the answer
    used to be "no change detected above the noise floor".

    The dissolve spreads 102 units of luma change over 30 frames, so every step
    is 2-4 against an absolute floor of 6.0 — it misses by 1.5x on every single
    frame while repainting half as much of the picture as the hard cut that the
    same rule catches 34x over.
    """
    extracted, _ = frames.extract_motion(
        str(dissolve_clip), tmp_path, 2.5, 4.5, source_fps=30.0
    )
    measured = frames.measure_motion(extracted)
    env = frames.motion_envelope(measured)

    assert env["first_motion"] == pytest.approx(3.0, abs=0.04)
    assert env["last_motion"] == pytest.approx(4.0, abs=0.04)
    assert env["duration_ms"] == pytest.approx(1000, abs=30)
    # The old rule, verbatim, on the same frames.
    assert not [f for f in measured if f["peak_delta"] >= 6.0]


def test_a_hard_cut_reads_as_one_frame(dissolve_clip: Path, tmp_path):
    """The other half of the comparison: the same clip's hard cut is a single
    frame of change, and must not be smeared into a transition with a duration."""
    extracted, _ = frames.extract_motion(
        str(dissolve_clip), tmp_path, 1.0, 2.0, source_fps=30.0
    )
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["duration_ms"] == pytest.approx(33, abs=20)
    assert env["last_motion"] == pytest.approx(1.5, abs=0.04)


def test_the_largest_event_wins_when_a_window_holds_two(dissolve_clip: Path, tmp_path):
    """Documented behaviour, asserted so it cannot drift into a surprise.

    Over the whole clip the envelope describes the hard cut, not the dissolve —
    the cut moves twice as much luma. That is why the motion workflow says to put
    --start/--end around one transition rather than around the scene holding it.
    """
    extracted, _ = frames.extract_motion(
        str(dissolve_clip), tmp_path, 0.0, 6.0, source_fps=30.0
    )
    env = frames.motion_envelope(frames.measure_motion(extracted))
    assert env["last_motion"] == pytest.approx(1.5, abs=0.04)
    assert env["duration_ms"] < 100


# --- regressions found by review, not by the suite ----------------------------


def test_motion_sampled_coarser_than_the_gap_allowance_still_merges():
    """A cap-thinned run samples slower than the 120ms merge allowance, and the
    gap between two *adjacent samples* is not stillness — it is the sampling
    period. Subtracting it is what makes the rule frame-rate independent.

    Before: ten consecutive moving frames 200ms apart could never merge (200 >
    120), so every frame became its own event and the report said 200ms for
    1800ms of continuous motion.
    """
    measured = [
        {"timestamp_seconds": i * 0.2, "peak_delta": 0.0 if i == 0 else 50.0}
        for i in range(10)
    ]
    assert frames.motion_envelope(measured)["duration_ms"] == pytest.approx(1800, abs=1)


def test_two_separate_animations_are_still_not_merged():
    """The other side of that fix: subtracting one sampling period must not turn
    the allowance into "merge everything"."""
    measured = [
        {"timestamp_seconds": i / 60, "peak_delta": 50.0 if 0 < i < 60 else 0.0}
        for i in range(120)
    ]
    for i in range(114, 120):
        measured[i]["peak_delta"] = 50.0          # a second burst 900ms later
    env = frames.motion_envelope(measured)
    assert env["duration_ms"] == pytest.approx(983, abs=20), "should report the first event only"


def test_clipped_start_is_false_when_the_window_contains_stillness_first():
    """clipped_start keys on the first *moving* frame, not on the backed-up start.

    Keying on the start index flagged an event whose first moving frame is index
    2 — which proves frame 0 -> frame 1 was still, i.e. the window does contain
    the beginning — and printed a "lower bound" caveat over an exact answer.
    """
    measured = [
        {"timestamp_seconds": 0.0, "peak_delta": 0.0},
        {"timestamp_seconds": 0.1, "peak_delta": 0.0},
        {"timestamp_seconds": 0.2, "peak_delta": 90.0},
        {"timestamp_seconds": 0.3, "peak_delta": 90.0},
        {"timestamp_seconds": 0.4, "peak_delta": 0.0},
    ]
    env = frames.motion_envelope(measured)
    assert env["clipped_start"] is False
    assert env["duration_ms"] == pytest.approx(200, abs=1)


def test_clipped_start_is_true_when_motion_is_underway_at_the_first_frame():
    measured = [
        {"timestamp_seconds": 0.0, "peak_delta": 0.0},
        {"timestamp_seconds": 0.1, "peak_delta": 90.0},
        {"timestamp_seconds": 0.2, "peak_delta": 90.0},
        {"timestamp_seconds": 0.3, "peak_delta": 0.0},
    ]
    assert frames.motion_envelope(measured)["clipped_start"] is True


def test_motion_signal_survives_a_thinned_frame_sequence(motion_clip: Path, tmp_path):
    """THE silent killer: _even_sample deletes the frames it did not select, so
    the surviving JPEGs are frame_0001, frame_0021, frame_0041… and the image2
    demuxer stops at the first missing index. _thumb_frames then returned [],
    every delta came back 0.0, and the report said "no change detected" about a
    3-second pan.

    Reached whenever the fps probe cannot thin up front — an unknown frame rate,
    or a probe that under-counts — which is exactly the case the cap exists for.
    """
    extracted, meta = frames.extract_motion(
        str(motion_clip), tmp_path, 0.0, 3.0, max_frames=10, source_fps=None,
    )
    assert meta["even_sampled"] is True, "fixture must actually exercise the thinning path"
    thumbs = frames._thumb_frames([Path(f["path"]) for f in extracted])
    assert len(thumbs) == len(extracted), "thumbnails must survive holes in the numbering"

    measured = frames.measure_motion(extracted)
    assert max(f["peak_delta"] for f in measured) > 50
    assert frames.motion_envelope(measured)["duration_ms"] == pytest.approx(2983, abs=100)
