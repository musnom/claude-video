"""Keyframe engine + preserved scene/uniform fallbacks."""
from __future__ import annotations

from pathlib import Path

import pytest

import frames


def test_keyframe_engine_on_cut_clip(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_keyframes(str(cut_clip), tmp_path / "f", max_frames=50)
    assert meta["engine"] == "keyframe"
    assert meta["fallback"] is False
    assert len(out) >= frames.KEYFRAME_MIN
    assert all(fr["reason"] == "keyframe" for fr in out)
    assert len(out) == len(list((tmp_path / "f").glob("frame_*.jpg")))


def test_keyframe_even_sampling_caps_and_spans(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_keyframes(str(cut_clip), tmp_path / "f", max_frames=5)
    assert meta["engine"] == "keyframe"
    assert len(out) == 5
    assert meta["selected_count"] == 5
    assert meta["candidate_count"] > 5
    ts = [fr["timestamp_seconds"] for fr in out]
    assert ts == sorted(ts)
    assert ts[0] < ts[-1]  # spans first → last keyframe
    assert [fr["index"] for fr in out] == [0, 1, 2, 3, 4]


def test_keyframe_fallback_on_static_clip(static_clip: Path, tmp_path: Path):
    out, meta = frames.extract_keyframes(str(static_clip), tmp_path / "f", max_frames=50)
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True
    assert len(out) > 0
    assert all(fr["reason"] == "uniform" for fr in out)


def test_scene_engine_on_cut_clip(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=100,
    )
    assert meta["engine"] == "scene"
    assert meta["fallback"] is False
    assert len(out) >= frames.SCENE_MIN_FRAMES


def test_scene_even_sampling_caps_and_spans(cut_clip: Path, tmp_path: Path):
    """Over-cap scene detection must even-sample across the whole clip, not keep
    the first N cuts and drop the tail (the long-video coverage bug)."""
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=5,
    )
    assert meta["engine"] == "scene"
    assert meta["fallback"] is False
    assert len(out) == 5
    assert meta["selected_count"] == 5
    assert meta["candidate_count"] > 5  # all cuts detected, then sampled down
    ts = [fr["timestamp_seconds"] for fr in out]
    assert ts == sorted(ts)
    assert ts[-1] > 4.0  # spans the full ~5.6s clip, not just the first ~1.6s
    assert len(out) == len(list((tmp_path / "f").glob("frame_*.jpg")))
    assert [fr["index"] for fr in out] == [0, 1, 2, 3, 4]


def test_scene_fallback_on_static_clip(static_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "f", fps=2.0, target_frames=12, max_frames=100,
    )
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True


# --- reported cap / budget ----------------------------------------------------
# The report used to print the caller's duration `target` as "budget" on every
# run, and `max_frames` as the cap. Both described the uniform fallback, which is
# the one engine that had not run in those cases. These pin each engine to
# reporting the numbers it actually enforced.


def test_scene_engine_reports_its_own_cap_and_no_budget(cut_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=5,
    )
    assert meta["engine"] == "scene"
    assert meta["effective_cap"] == 5
    # target_frames=50 governs only the uniform fallback, which did not run.
    assert meta["budget"] is None
    assert len(out) == 5


def test_scene_engine_uncapped_reports_no_cap(cut_clip: Path, tmp_path: Path):
    _, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=None,
    )
    assert meta["engine"] == "scene"
    assert meta["effective_cap"] is None


def test_uncapped_uniform_fallback_reports_the_cap_it_enforced(
    static_clip: Path, tmp_path: Path
):
    """`--detail token-burner` is advertised as uncapped, but its uniform
    fallback still tops out at the duration budget. The report must say so
    instead of claiming no cap."""
    out, meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "f", fps=2.0, target_frames=7, max_frames=None,
    )
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True
    assert meta["effective_cap"] == 7  # not None
    assert meta["budget"] == 7
    assert len(out) <= 7


def test_uniform_fallback_candidate_count_is_what_dedup_compared(
    static_clip: Path, tmp_path: Path
):
    """Reporting the scene count here produced arithmetically impossible lines
    like "1 selected from 1 candidates, 59 near-duplicates dropped"."""
    out, meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "f", fps=2.0, target_frames=20, max_frames=20,
    )
    assert meta["engine"] == "uniform"
    assert meta["candidate_count"] == len(out) + meta["deduped_count"]
    # the shot count that triggered the fallback is kept, just not as "candidates"
    assert meta["scene_count"] < frames.SCENE_MIN_FRAMES


def test_keyframe_engine_reports_its_own_cap(cut_clip: Path, tmp_path: Path):
    _, meta = frames.extract_keyframes(str(cut_clip), tmp_path / "f", max_frames=5)
    assert meta["engine"] == "keyframe"
    assert meta["effective_cap"] == 5
    assert meta["budget"] is None


def test_keyframe_fallback_candidate_count_is_what_dedup_compared(
    static_clip: Path, tmp_path: Path
):
    out, meta = frames.extract_keyframes(str(static_clip), tmp_path / "f", max_frames=20)
    assert meta["engine"] == "uniform"
    assert meta["candidate_count"] == len(out) + meta["deduped_count"]
    assert meta["effective_cap"] == 20
    assert meta["keyframe_count"] < frames.KEYFRAME_MIN


# --- scene threshold ----------------------------------------------------------
# SCENE_THRESHOLD was 0.20, which is tuned for camera cuts. A motion-graphics cut
# changes part of the frame rather than all of it and scores an order of
# magnitude lower, so an 8-shot infographic came back as "uniform fallback, too
# few shots (1)" — the tool telling the model a motion-graphics piece has one
# shot.


def test_scene_engine_finds_graphic_cuts_at_the_default_threshold(
    graphic_cuts_clip: Path, tmp_path: Path
):
    """7 authored cuts, all found, and the scene result is kept rather than
    discarded for the uniform fallback."""
    out, meta = frames.extract_scene_or_uniform(
        str(graphic_cuts_clip), tmp_path / "f", fps=1.0, target_frames=30, max_frames=100,
    )
    assert meta["engine"] == "scene"
    assert meta["fallback"] is False
    # 7 cuts + the unconditional first frame.
    assert meta["candidate_count"] == 8
    stamps = [round(f["timestamp_seconds"]) for f in out if f["reason"] == "scene-change"]
    assert stamps == [2, 4, 6, 8, 10, 12, 14]


def test_graphic_cuts_fall_back_to_uniform_at_the_old_threshold(
    graphic_cuts_clip: Path, tmp_path: Path
):
    """The bug, still reachable through the parameter — this is what 0.20 did."""
    _, meta = frames.extract_scene_or_uniform(
        str(graphic_cuts_clip), tmp_path / "f", fps=1.0, target_frames=30, max_frames=100,
        scene_threshold=0.20,
    )
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True
    assert meta["scene_count"] == 1


def test_lowering_the_threshold_does_not_over_detect_on_camera_cuts(
    cut_clip: Path, tmp_path: Path
):
    """The risk of the change, guarded. 14 solid-colour shots score 0.8+ per cut,
    so 0.05 and 0.20 must find the same candidates; if they ever diverge the new
    default is firing on encoder noise."""
    low = frames.extract_scene_candidates(
        str(cut_clip), tmp_path / "low", max_frames=None, threshold=0.05,
    )
    high = frames.extract_scene_candidates(
        str(cut_clip), tmp_path / "high", max_frames=None, threshold=0.20,
    )
    assert len(low) == len(high)
    assert [round(f["timestamp_seconds"], 2) for f in low] == \
           [round(f["timestamp_seconds"], 2) for f in high]


def test_scene_threshold_reaches_the_filter_string(monkeypatch, tmp_path):
    """The parameter existed on extract_scene_candidates and no caller ever
    passed it, so the flag has to be pinned into the argv, not just accepted."""
    from test_ffmpeg_compat import _capture_extraction_argv

    calls = _capture_extraction_argv(monkeypatch)
    frames.extract_scene_or_uniform(
        "video.mp4", tmp_path, fps=1.0, target_frames=10, max_frames=10,
        scene_threshold=0.05,
    )
    vf = calls[0][calls[0].index("-vf") + 1]
    assert "gt(scene\\,0.05)" in vf, vf


def test_scene_threshold_defaults_into_the_filter_string(monkeypatch, tmp_path):
    from test_ffmpeg_compat import _capture_extraction_argv

    calls = _capture_extraction_argv(monkeypatch)
    frames.extract_scene_or_uniform(
        "video.mp4", tmp_path, fps=1.0, target_frames=10, max_frames=10,
    )
    vf = calls[0][calls[0].index("-vf") + 1]
    assert f"gt(scene\\,{frames.SCENE_THRESHOLD})" in vf, vf


# --- gap fill -----------------------------------------------------------------
# Scene detection returns one frame per shot and stops. On a long take that
# leaves most of the cap unspent: measured "12 selected from 12 candidates
# (scene, cap 100)" on a 12-minute clip, one frame per 60s, with a 250-second
# closing shot represented by a single frame — while `--detail efficient`
# returned 50 frames on the same input, i.e. the cheap mode beat the default.


def test_gap_fill_spends_the_budget_on_a_long_take(sparse_cuts_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=60, max_frames=100,
    )
    assert meta["engine"] == "scene"
    assert meta["candidate_count"] == 12
    assert meta["gap_filled"] == 88
    assert len(out) == 100 <= 100

    stamps = [f["timestamp_seconds"] for f in out]
    assert stamps == sorted(stamps)
    assert [f["index"] for f in out] == list(range(len(out)))

    # Every detected shot survives — fills are additive, never a replacement.
    scene_frames = [f for f in out if f["reason"] in ("first-frame", "scene-change")]
    assert len(scene_frames) == 12
    assert sum(1 for f in out if f["reason"] == "gap-fill") == 88

    # The point of the exercise: the worst hole shrinks from a quarter of the
    # clip to something a reader can actually navigate.
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 15.0, f"worst gap still {max(gaps):.0f}s"


def test_gap_fill_leaves_a_densely_cut_clip_alone(cut_clip: Path, tmp_path: Path):
    """A clip whose frames are already ~0.4s apart is covered. Padding it to the
    cap would multiply the token cost for nothing."""
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=50, max_frames=100,
    )
    assert meta["gap_filled"] == 0
    assert all(f["reason"] != "gap-fill" for f in out)


def test_gap_fill_does_not_run_when_the_cap_is_already_full(
    sparse_cuts_clip: Path, tmp_path: Path
):
    out, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=12, max_frames=12,
    )
    assert len(out) == 12
    assert meta["gap_filled"] == 0


def test_gap_fill_frames_do_not_clobber_transcript_cues(
    sparse_cuts_clip: Path, tmp_path: Path
):
    """Both paths write single frames at exact timestamps, and both delete their
    own prefix before starting. Sharing `cue_` would make the detail engine
    silently delete the cue frames the user explicitly asked for — and in a real
    run the cues are extracted first, so they would be the casualties."""
    out_dir = tmp_path / "f"
    cues, _ = frames.extract_at_timestamps(
        str(sparse_cuts_clip), out_dir, [100.0, 200.0, 300.0],
    )
    assert len(cues) == 3
    frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), out_dir, fps=0.5, target_frames=60, max_frames=100,
    )
    assert len(list(out_dir.glob("cue_*.jpg"))) == 3
    assert all(Path(c["path"]).exists() for c in cues)
    assert list(out_dir.glob("fill_*.jpg"))


def test_gap_fill_points_bisect_the_widest_hole_first():
    """Pure geometry, no ffmpeg: the placement rule minimises the worst gap
    rather than spreading frames evenly."""
    points = frames._gap_fill_points([0.0, 10.0, 100.0], end_seconds=100.0, budget=3, min_gap=2.0)
    # 90-wide hole first, then its 45-wide halves — never the 10-wide one.
    assert points == [32.5, 55.0, 77.5]


def test_gap_fill_points_respect_the_minimum_gap():
    assert frames._gap_fill_points([0.0, 1.0, 2.0], end_seconds=2.0, budget=10, min_gap=2.0) == []


def test_gap_fill_points_cover_the_tail_after_the_last_frame():
    """The trailing hole is routinely the worst one: a closing shot has exactly
    one detected frame, at its start."""
    points = frames._gap_fill_points([0.0, 1.0], end_seconds=100.0, budget=1, min_gap=2.0)
    assert points == [50.5]


# --- shot statistics ----------------------------------------------------------
# Every cut is timestamped during detection, then _even_sample keeps ~100 frames
# by list index and unlinks the rest along with their times. Reading the gaps
# between the survivors as shot lengths is what made the report state ~10
# cuts/min on a clip cutting at 120 — confidently, because the timestamps that
# remained were themselves correct.


def test_shot_stats_on_a_known_rhythm():
    """Pure arithmetic: 0.5s shots for 10s."""
    stamps = [i * 0.5 for i in range(21)]
    stats = frames.shot_stats(stamps)
    assert stats["cuts"] == 20
    assert stats["per_minute"] == 120.0
    assert stats["median_s"] == 0.5
    assert stats["shortest_s"] == stats["longest_s"] == 0.5


def test_shot_stats_percentiles_describe_the_spread():
    """A montage of quick cuts ending in two long holds.

    The median must describe the montage rather than being dragged by the tail,
    and p90 must show the tail exists — which is the pair of numbers an editing
    question actually wants ("fast cuts, then it sits on a shot").
    """
    quick = [0.4] * 8
    stamps, t = [0.0], 0.0
    for duration in quick + [10.0, 10.0]:
        t += duration
        stamps.append(round(t, 3))
    stats = frames.shot_stats(stamps)

    assert stats["cuts"] == 10
    assert stats["median_s"] == 0.4
    assert stats["p10_s"] == 0.4
    assert stats["p90_s"] == 10.0        # nearest-rank: the tail is 20% of shots
    assert stats["shortest_s"] == 0.4
    assert stats["longest_s"] == 10.0


def test_shot_stats_degenerate_inputs():
    empty = frames.shot_stats([])
    assert empty["cuts"] == 0 and empty["median_s"] is None
    assert frames.shot_stats([1.0])["cuts"] == 0


def test_shot_stats_are_reported_from_a_real_clip(fast_cut_clip: Path, tmp_path: Path):
    _, meta = frames.extract_scene_or_uniform(
        str(fast_cut_clip), tmp_path / "f", fps=2.0, target_frames=24, max_frames=100,
    )
    shots = meta["shots"]
    assert shots["cuts"] >= 20
    assert 100 <= shots["per_minute"] <= 130      # true rate is 23 cuts / 11.5s = 120
    assert shots["median_s"] == pytest.approx(0.5, abs=0.05)


def test_shot_stats_do_not_change_when_the_cap_thins_the_frames(
    fast_cut_clip: Path, tmp_path: Path
):
    """THE regression this exists for. The statistics describe the video; the cap
    describes the token budget. They must not be the same number."""
    _, generous = frames.extract_scene_or_uniform(
        str(fast_cut_clip), tmp_path / "a", fps=2.0, target_frames=24, max_frames=100,
    )
    kept, thinned = frames.extract_scene_or_uniform(
        str(fast_cut_clip), tmp_path / "b", fps=2.0, target_frames=24, max_frames=5,
    )
    assert len(kept) == 5, "the cap really did bite"
    assert thinned["shots"] == generous["shots"]

    # And the naive reading — gaps between surviving frames — is wrong by the
    # margin that motivated all of this.
    stamps = [f["timestamp_seconds"] for f in kept]
    naive_gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    naive_rate = 60.0 / (sum(naive_gaps) / len(naive_gaps))
    assert naive_rate < thinned["shots"]["per_minute"] / 3


# --- regressions found by review, not by the suite ----------------------------
# Every clip the shot tests above measure is uniformly cut and ends on a cut, so
# both of these were invisible: span-between-cuts equalled the duration, and the
# closing shot had the same length as all the others.


def test_shot_rate_is_over_the_window_not_the_span_between_cuts():
    """Eight cuts in the first four seconds of a thirty-second clip.

    Dividing by the cut span reported 120 cuts/min for a video that is a static
    card for 26 of its 30 seconds — squarely in the band references/editing-style.md
    calls "Vox/TikTok-style rapid cutting". The true rate is 16.
    """
    stamps = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    stats = frames.shot_stats(stamps, 0.0, 30.0)
    assert stats["cuts"] == 7
    assert stats["per_minute"] == pytest.approx(14.0, abs=0.1)   # 7 cuts / 30s
    # Without the window it reverts to the old, wrong denominator — pinned so the
    # difference is visible rather than implied.
    assert frames.shot_stats(stamps)["per_minute"] == pytest.approx(105.0, abs=0.1)


def test_the_closing_shot_is_in_the_distribution():
    """The last shot runs from the final cut to the end of the clip, which is not
    a gap between two stamps — so it was missing entirely, and `longest` reported
    the second-longest shot."""
    stamps = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
    assert frames.shot_stats(stamps, 0.0, 30.0)["longest_s"] == pytest.approx(26.0)
    assert frames.shot_stats(stamps)["longest_s"] == pytest.approx(1.0)


def test_shot_stats_survive_an_unknown_window_end():
    """No window means the old behaviour, not a fabricated closing shot."""
    stats = frames.shot_stats([0.0, 1.0, 2.0], 0.0, None)
    assert stats["cuts"] == 2
    assert stats["longest_s"] == 1.0


def test_real_clip_shot_rate_matches_its_duration(sparse_cuts_clip: Path, tmp_path: Path):
    """The repo's own 12-minute fixture: 11 cuts over 720s = 0.9/min. The
    span-based denominator reported 1.4, and called the 250s closing shot 200s."""
    _, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=60, max_frames=100,
    )
    shots = meta["shots"]
    assert shots["cuts"] == 11
    assert shots["per_minute"] == pytest.approx(0.9, abs=0.1)
    assert shots["longest_s"] == pytest.approx(250.0, abs=1.0)


def test_token_burner_covers_at_least_as_much_as_balanced(sparse_cuts_clip: Path, tmp_path: Path):
    """The maximum-fidelity mode must not return fewer frames than the default.

    Gap-fill was gated on `max_frames is not None`, so token-burner — which the
    report's own long-video warning recommends for better coverage — stopped at
    the 12 detected shots while balanced topped up to 100. Uncapped means "keep
    every shot", not "cover less".
    """
    balanced, _ = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "b", fps=0.5, target_frames=100, max_frames=100,
    )
    burner, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "t", fps=0.5, target_frames=100, max_frames=None,
    )
    assert len(burner) >= len(balanced), f"token-burner {len(burner)} < balanced {len(balanced)}"
    assert meta["gap_filled"] > 0
    # Every detected shot still survives — filling is additive.
    assert sum(1 for f in burner if f["reason"] in ("first-frame", "scene-change")) == 12


def test_uncapped_fill_does_not_shrink_a_cut_heavy_clip(cut_clip: Path, tmp_path: Path):
    """A clip with more detected shots than the duration budget keeps them all
    and fills nothing — the budget is a floor for coverage, not a ceiling."""
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=5, max_frames=None,
    )
    assert len(out) == meta["candidate_count"] == 13
    assert meta["gap_filled"] == 0
