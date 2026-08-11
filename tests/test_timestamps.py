"""Transcript-cue timestamps: parsing, point extraction, and pinned merge."""
from __future__ import annotations

from pathlib import Path

import pytest

import frames


def test_parse_timestamps_mixed_formats():
    assert frames.parse_timestamps("30,1:05,90") == [30.0, 65.0, 90.0]


def test_parse_timestamps_strips_and_dedupes():
    assert frames.parse_timestamps(" 90 , 30, 30 ") == [30.0, 90.0]


def test_parse_timestamps_empty():
    assert frames.parse_timestamps("") == []
    assert frames.parse_timestamps("  ,  ") == []


def test_parse_timestamps_rejects_garbage():
    with pytest.raises(SystemExit):
        frames.parse_timestamps("4:bad")


def test_merge_frames_sorts_and_reindexes():
    primary = [
        {"index": 0, "timestamp_seconds": 1.0, "path": "a", "reason": "scene-change"},
        {"index": 1, "timestamp_seconds": 5.0, "path": "b", "reason": "scene-change"},
    ]
    pinned = [
        {"index": 0, "timestamp_seconds": 3.0, "path": "c", "reason": "transcript-cue"},
    ]
    merged = frames.merge_frames(primary, pinned)
    assert [f["path"] for f in merged] == ["a", "c", "b"]
    assert [f["index"] for f in merged] == [0, 1, 2]
    assert merged[1]["reason"] == "transcript-cue"


def test_merge_frames_keeps_all_pinned():
    pinned = [{"index": 0, "timestamp_seconds": 2.0, "path": "c", "reason": "transcript-cue"}]
    merged = frames.merge_frames([], pinned)
    assert [f["path"] for f in merged] == ["c"]


def test_extract_at_timestamps_one_frame_per_point(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_at_timestamps(str(cut_clip), tmp_path / "f", [0.5, 2.0, 4.0])
    assert meta["engine"] == "timestamps"
    assert meta["fallback"] is False
    assert len(out) == 3
    assert all(f["reason"] == "transcript-cue" for f in out)
    ts = [f["timestamp_seconds"] for f in out]
    assert ts == sorted(ts)
    assert len(out) == len(list((tmp_path / "f").glob("cue_*.jpg")))


def test_extract_at_timestamps_drops_out_of_window(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_at_timestamps(
        str(cut_clip), tmp_path / "f", [0.5, 2.0, 4.0],
        start_seconds=1.0, end_seconds=3.0,
    )
    assert [f["timestamp_seconds"] for f in out] == [2.0]
    assert meta["dropped_out_of_window"] == 2


def test_extract_at_timestamps_caps_and_spans(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_at_timestamps(
        str(cut_clip), tmp_path / "f", [0.5, 1.5, 2.5, 3.5, 4.5], max_frames=3,
    )
    assert len(out) == 3
    ts = [f["timestamp_seconds"] for f in out]
    assert ts[0] == 0.5 and ts[-1] == 4.5  # even-sample keeps first + last
    assert len(out) == len(list((tmp_path / "f").glob("cue_*.jpg")))


def test_extract_at_timestamps_does_not_clobber_detail_frames(cut_clip: Path, tmp_path: Path):
    """Cue frames live alongside detail frames in the same dir without deleting them."""
    d = tmp_path / "f"
    scene, _ = frames.extract_scene_or_uniform(
        str(cut_clip), d, fps=2.0, target_frames=50, max_frames=100,
    )
    cues, _ = frames.extract_at_timestamps(str(cut_clip), d, [1.0, 3.0])
    assert len(list(d.glob("frame_*.jpg"))) == len(scene)
    assert len(list(d.glob("cue_*.jpg"))) == len(cues)


# --- sub-second frame labels --------------------------------------------------
# format_time rounds to the second, so at dense sampling several frames share a
# label and the implied boundary lands a frame away from the real one. Verified
# before the fix: eight cue frames at 50ms spacing printed 00:00 then 00:01 x7,
# while the underlying colour cuts were 100ms apart.


def test_format_time_ms_examples():
    assert frames.format_time_ms(0) == "00:00.000"
    assert frames.format_time_ms(0.55) == "00:00.550"
    assert frames.format_time_ms(12.3174) == "00:12.317"
    assert frames.format_time_ms(3725.5) == "1:02:05.500"


def test_format_time_ms_carries_into_the_next_second():
    """Integer-millisecond arithmetic, so .9996 must not render as '.1000'."""
    assert frames.format_time_ms(0.9996) == "00:01.000"
    assert frames.format_time_ms(59.9999) == "01:00.000"


def test_format_time_ms_agrees_with_format_time():
    """The two clocks must stay the same clock.

    format_time is shared with transcribe.format_transcript (unified in 0.3.0 so
    the frame and transcript stamps agree). format_time_ms is that value at finer
    resolution, never a second, competing clock.
    """
    for t in [0, 0.4, 0.5, 12.3174, 59.4, 60, 125, 3599.5, 3600, 3725.5, 86399]:
        coarse = frames.format_time(t)
        fine = frames.format_time_ms(t)
        # Round-trip through parse_time, not a string-prefix comparison: at a
        # rounding boundary the two legitimately differ in every field. t=3599.5
        # coarsens to "1:00:00" but is exactly "59:59.500", and both re-coarsen
        # to "1:00:00". Re-coarsening is the invariant; sharing a prefix is not.
        assert abs(frames.parse_time(fine) - t) < 0.001, f"round-trip drift at {t}"
        assert frames.format_time(frames.parse_time(fine)) == coarse, f"clocks disagree at {t}"


def test_format_time_stays_whole_seconds():
    """Regression guard on the shared transcript clock."""
    assert frames.format_time(12.6) == "00:13"
    assert frames.format_time(0.55) == "00:01"


def test_cue_timestamps_keep_millisecond_precision():
    """Requests are rounded to 3dp, so a 50ms grid survives intact."""
    assert frames.parse_timestamps("0.5,0.55,0.6") == [0.5, 0.55, 0.6]


def test_cue_frames_carry_millisecond_timestamps(cut_clip: Path, tmp_path):
    cues, _ = frames.extract_at_timestamps(
        str(cut_clip), tmp_path, [0.50, 0.55, 0.60, 0.65]
    )
    assert len(cues) == 4
    stamps = [frames.format_time_ms(c["timestamp_seconds"]) for c in cues]
    assert stamps == ["00:00.500", "00:00.550", "00:00.600", "00:00.650"]
    assert len(set(stamps)) == 4, "labels must be distinct inside one second"
