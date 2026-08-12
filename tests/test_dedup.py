"""Frame-delta dedup: per-pixel difference, greedy de-duplication, integration."""
from __future__ import annotations

from pathlib import Path

import frames


# --- _dedupe_by_deltas: greedy drop vs last *kept* thumbnail ------------------

def _touch(dirpath: Path, n: int) -> list[dict]:
    dirpath.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = dirpath / f"frame_{i:04d}.jpg"
        p.write_bytes(b"x")
        out.append({"index": i, "timestamp_seconds": float(i), "path": str(p), "reason": "scene-change"})
    return out


FLAT0 = bytes([0, 0, 0, 0])
FLAT255 = bytes([255, 255, 255, 255])


def test_dedupe_collapses_identical_run(tmp_path: Path):
    cands = _touch(tmp_path, 5)
    thumbs = [FLAT0, FLAT0, FLAT0, FLAT0, FLAT0]
    survivors, dropped = frames._dedupe_by_deltas(cands, thumbs, threshold=2.0)
    assert dropped == 4
    assert len(survivors) == 1
    assert survivors[0]["index"] == 0
    assert sorted(p.name for p in tmp_path.glob("frame_*.jpg")) == ["frame_0000.jpg"]


def test_dedupe_keeps_all_distinct(tmp_path: Path):
    cands = _touch(tmp_path, 4)
    thumbs = [FLAT0, FLAT255, FLAT0, FLAT255]
    survivors, dropped = frames._dedupe_by_deltas(cands, thumbs, threshold=2.0)
    assert dropped == 0
    assert [s["index"] for s in survivors] == [0, 1, 2, 3]
    assert len(list(tmp_path.glob("frame_*.jpg"))) == 4


def test_dedupe_compares_against_last_kept_not_previous(tmp_path: Path):
    """A,A,B,B,A with A/B far apart -> keep A0, B2, A4 (drops the repeats)."""
    cands = _touch(tmp_path, 5)
    survivors, dropped = frames._dedupe_by_deltas(
        cands, [FLAT0, FLAT0, FLAT255, FLAT255, FLAT0], threshold=2.0
    )
    assert [s["index"] for s in survivors] == [0, 1, 2]  # reindexed survivors
    assert dropped == 2


def test_dedupe_threshold_is_inclusive(tmp_path: Path):
    """Delta exactly == threshold is treated as a duplicate (<=)."""
    cands = _touch(tmp_path, 2)
    a = bytes([0, 0, 0, 0])
    b = bytes([8, 0, 0, 0])  # mean abs diff == 2.0
    survivors, dropped = frames._dedupe_by_deltas(cands, [a, b], threshold=2.0)
    assert dropped == 1
    assert len(survivors) == 1


def test_dedupe_empty_and_single_are_noops(tmp_path: Path):
    assert frames._dedupe_by_deltas([], [], threshold=2.0) == ([], 0)
    one = _touch(tmp_path, 1)
    survivors, dropped = frames._dedupe_by_deltas(one, [FLAT0], threshold=2.0)
    assert dropped == 0
    assert len(survivors) == 1


def test_dedupe_mismatched_thumb_count_is_noop(tmp_path: Path):
    """Fail open: if thumbs don't line up with candidates, change nothing."""
    cands = _touch(tmp_path, 3)
    survivors, dropped = frames._dedupe_by_deltas(cands, [FLAT0], threshold=2.0)
    assert dropped == 0
    assert len(survivors) == 3


# --- _thumb_frames + dedupe_perceptual: real ffmpeg over extracted JPEGs ------

def test_thumb_frames_match_candidate_count(cut_clip: Path, tmp_path: Path):
    out = frames.extract_scene_candidates(str(cut_clip), tmp_path / "f")
    thumbs = frames._thumb_frames([Path(fr["path"]) for fr in out])
    assert len(thumbs) == len(out)
    assert all(len(t) == frames.DEDUP_THUMB * frames.DEDUP_THUMB for t in thumbs)


def test_dedupe_perceptual_collapses_static_clip(static_clip: Path, tmp_path: Path):
    out = frames.extract(str(static_clip), tmp_path / "f", fps=4.0, max_frames=10)
    n_before = len(out)
    survivors, dropped, blank = frames.dedupe_perceptual(out)
    assert n_before > 1
    assert len(survivors) == 1
    assert dropped == n_before - 1
    assert blank == 0                    # blue, not black — nothing blank here
    assert len(list((tmp_path / "f").glob("frame_*.jpg"))) == 1


def test_dedupe_perceptual_keeps_distinct_cuts(cut_clip: Path, tmp_path: Path):
    """Distinct color shots differ in luma, so frame-delta keeps them all."""
    out = frames.extract_scene_candidates(str(cut_clip), tmp_path / "f")
    n_before = len(out)
    survivors, dropped, _blank = frames.dedupe_perceptual(out)
    assert dropped == 0
    assert len(survivors) == n_before


# --- engine integration: dedup runs before the cap, reports deduped_count -----

def test_scene_engine_reports_zero_dedup_on_distinct(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=100,
    )
    assert meta["engine"] == "scene"
    assert meta["deduped_count"] == 0
    assert len(out) == len(list((tmp_path / "f").glob("frame_*.jpg")))


def test_uniform_fallback_dedupes_static(static_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "f", fps=4.0, target_frames=12, max_frames=100,
    )
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True
    assert meta["deduped_count"] > 0
    assert meta["selected_count"] == 1  # identical frames collapse to one
    assert len(out) == 1
    assert len(list((tmp_path / "f").glob("frame_*.jpg"))) == 1


def test_keyframe_uniform_fallback_dedupes_static(static_clip: Path, tmp_path: Path):
    out, meta = frames.extract_keyframes(str(static_clip), tmp_path / "f", max_frames=50)
    assert meta["engine"] == "uniform"
    assert meta["deduped_count"] > 0
    assert len(out) == 1


def test_dedup_false_disables_collapse(static_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "f", fps=4.0, target_frames=12, max_frames=100,
        dedup=False,
    )
    assert meta["deduped_count"] == 0
    assert meta["selected_count"] > 1  # no collapse without dedup
    assert len(out) > 1


# --- localised change survives dedup ------------------------------------------
# frames.py's module docstring promised "a code diff / scrolling terminal /
# slide-gaining-a-bullet survives". Thresholding the mean alone, it did not: a
# change confined to a few of the 256 thumbnail cells barely moves a whole-frame
# average, so the tool deleted exactly the frames a reader needed and reported
# them as near-duplicates.


def test_a_slide_gaining_one_line_is_not_a_near_duplicate(slide_deck_clip: Path, tmp_path: Path):
    """The promise in the docstring, as a test.

    Measured on these frames: mean 1.42-1.77 (all under DEDUP_THRESHOLD = 2.0, so
    the old rule dropped every build) against peak 55-70.
    """
    from conftest import slide_deck_state_times

    states, _ = frames.extract_at_timestamps(
        str(slide_deck_clip), tmp_path / "f", slide_deck_state_times(), resolution=512,
    )
    assert len(states) == 5
    thumbs = frames._thumb_frames([Path(f["path"]) for f in states])
    deltas = [frames._cell_deltas(thumbs[i], thumbs[i - 1]) for i in range(1, len(thumbs))]

    # The premise: invisible to the mean, obvious to the peak. If this ever stops
    # holding the fixture has drifted and the test below proves nothing.
    assert all(mean < frames.DEDUP_THRESHOLD for mean, _ in deltas), deltas
    assert all(peak > frames.DEDUP_PEAK_THRESHOLD for _, peak in deltas), deltas

    survivors, dropped = frames._dedupe_by_deltas(states, thumbs)
    assert dropped == 0
    assert len(survivors) == 5


def test_five_state_deck_returns_five_frames_end_to_end(slide_deck_clip: Path, tmp_path: Path):
    """The user-visible bug: a 5-state deck came back as 3 frames, with states 2
    and 4 deleted and the loss reported as "near-duplicates dropped"."""
    out, meta = frames.extract_scene_or_uniform(
        str(slide_deck_clip), tmp_path / "f", fps=2.0, target_frames=20, max_frames=100,
    )
    stamps = [f["timestamp_seconds"] for f in out]
    # One frame per state at least, and each state represented.
    assert len(out) >= 5, stamps
    for state in range(5):
        lo, hi = state * 2.0, (state + 1) * 2.0
        assert any(lo <= t < hi for t in stamps), f"state {state} missing from {stamps}"


def test_peak_rule_does_not_stop_identical_frames_collapsing(static_clip: Path, tmp_path: Path):
    """The other half of the contract. Keeping localised change is only useful if
    genuinely identical frames still go away — otherwise the budget goes on a
    held slide."""
    out = frames.extract(str(static_clip), tmp_path / "f", fps=4.0, max_frames=10)
    survivors, dropped, _blank = frames.dedupe_perceptual(out)
    assert len(survivors) == 1
    assert dropped == len(out) - 1


def test_dedup_fails_open_on_ragged_thumbnails(tmp_path: Path):
    """_cell_deltas returns (0.0, 0.0) on ragged input, which read as a delta
    would DELETE the frame. The ragged case has to be handled explicitly in
    _is_near_duplicate (keep, never drop) or a decode hiccup starts eating
    frames."""
    cands = _touch(tmp_path, 3)
    thumbs = [FLAT0, b"\x00\x10", FLAT0]      # middle thumb is the wrong length
    survivors, dropped = frames._dedupe_by_deltas(cands, thumbs)
    assert dropped == 0
    assert len(survivors) == 3
    assert len(list(tmp_path.glob("frame_*.jpg"))) == 3


def test_peak_alone_is_enough_to_keep_a_frame(tmp_path: Path):
    """Mean says duplicate, peak says otherwise, and the frame survives — one
    cell of 256 changing by 200 is a control changing state."""
    cands = _touch(tmp_path, 2)
    a = bytes([100] * 256)
    b = bytearray(a)
    b[0] = 255                                 # mean moves 0.6, peak moves 155
    survivors, dropped = frames._dedupe_by_deltas(cands, [a, bytes(b)])
    assert dropped == 0
    assert len(survivors) == 2


# --- blank-frame filter (trailing end cards) ----------------------------------
# A solid-black end card genuinely differs from its predecessor, so dedup keeps
# it — and it spends an image slot on a rectangle of nothing. The filter drops
# only a *trailing* run of blank thumbnails, never below one surviving frame.


def test_blank_thumb_requires_black_and_uniform():
    black = bytes([2] * 256)
    dark_textured = bytes([2] * 246 + [30] * 10)   # dark scene with detail
    uniform_gray = bytes([20] * 256)               # uniform but not black
    assert frames._is_blank_thumb(black)
    assert not frames._is_blank_thumb(dark_textured)
    assert not frames._is_blank_thumb(uniform_gray)
    assert not frames._is_blank_thumb(b"")


def test_trailing_blanks_are_dropped_but_leading_and_middle_stay(tmp_path: Path):
    cands = _touch(tmp_path, 5)
    black = bytes([1] * 4)
    thumb_by_path = {
        cands[0]["path"]: black,      # leading black: real editing info, stays
        cands[1]["path"]: FLAT255,
        cands[2]["path"]: black,      # mid-video fade boundary, stays
        cands[3]["path"]: FLAT255,
        cands[4]["path"]: black,      # trailing end card: goes
    }
    survivors, n = frames._drop_trailing_blanks(cands, thumb_by_path)
    assert n == 1
    assert [s["index"] for s in survivors] == [0, 1, 2, 3]
    assert not Path(cands[4]["path"]).exists()
    assert Path(cands[0]["path"]).exists()


def test_all_blank_never_drops_below_one_frame(tmp_path: Path):
    cands = _touch(tmp_path, 3)
    black = bytes([0] * 4)
    thumb_by_path = {c["path"]: black for c in cands}
    survivors, n = frames._drop_trailing_blanks(cands, thumb_by_path)
    assert n == 2
    assert len(survivors) == 1


def test_end_card_dropped_end_to_end(cut_clip_black_tail: Path, tmp_path: Path):
    """The measured case from upstream: a real clip ending on a black card
    returns no black frame, and the meta says one was dropped."""
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip_black_tail), tmp_path / "f", fps=2.0, target_frames=20,
        max_frames=100, full_duration=7.0,
    )
    assert meta["blank_dropped"] >= 1
    # The clip ENDS clean: the last surviving frame is not the black card. A
    # black frame elsewhere is fine — cut_clip has a legitimate mid-clip black
    # shot, and mid-video black is editing information the filter must keep.
    thumbs = frames._thumb_frames([Path(f["path"]) for f in out])
    assert thumbs, "thumbnail decode failed; the assertion below would be vacuous"
    assert not frames._is_blank_thumb(thumbs[-1])


def test_no_dedup_keeps_the_end_card(cut_clip_black_tail: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip_black_tail), tmp_path / "f", fps=2.0, target_frames=20,
        max_frames=100, dedup=False, full_duration=7.0,
    )
    assert meta["blank_dropped"] == 0
    thumbs = frames._thumb_frames([Path(f["path"]) for f in out])
    assert any(frames._is_blank_thumb(t) for t in thumbs)
