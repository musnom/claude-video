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
