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
        str(cut_clip), tmp_path / "low", threshold=0.05,
    )
    high = frames.extract_scene_candidates(
        str(cut_clip), tmp_path / "high", threshold=0.20,
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


def test_gap_fill_spends_the_budget_on_a_moving_long_take(
    sparse_moving_clip: Path, tmp_path: Path
):
    """Content everywhere (the drifting marker makes every fill distinct), so
    the budget is genuinely spent and the worst hole shrinks from a fifth of
    the clip to something a reader can navigate."""
    fps, target = frames.auto_fps(300.0, max_frames=100)
    out, meta = frames.extract_scene_or_uniform(
        str(sparse_moving_clip), tmp_path / "f", fps=fps, target_frames=target,
        max_frames=100, full_duration=300.0,
    )
    assert meta["engine"] == "scene"
    assert meta["candidate_count"] >= 12
    assert meta["gap_filled"] > 40
    assert len(out) == 100

    stamps = [f["timestamp_seconds"] for f in out]
    assert stamps == sorted(stamps)
    assert [f["index"] for f in out] == list(range(len(out)))

    # Every detected shot survives — fills are additive, never a replacement.
    scene_frames = [f for f in out if f["reason"] in ("first-frame", "scene-change")]
    assert len(scene_frames) >= 12

    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 15.0, f"worst gap still {max(gaps):.0f}s"


def test_gap_fill_stops_early_on_static_shots(sparse_cuts_clip: Path, tmp_path: Path):
    """The 89%-duplicate pathology, pinned the other way. 720 s of static solid
    shots: every fill midpoint duplicates the bounding frame of its own shot, so
    the fills are rejected, the holes retired, and the run returns the detected
    shots rather than 100 frames of padding — which is what the measured
    '100 frames, 11 visually distinct' regression was."""
    out, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=60,
        max_frames=100, full_duration=720.0,
    )
    assert meta["engine"] == "scene"
    assert meta["gap_fill_rejected"] > 0
    # Either non-budget stop is the honest outcome here: holes retired as
    # static ("saturated") or narrowed under the 2s floor ("min-gap").
    assert meta["gap_fill_stop"] in ("saturated", "min-gap")
    assert meta["gap_filled"] == 0
    assert len(out) == 12
    scene_frames = [f for f in out if f["reason"] in ("first-frame", "scene-change")]
    assert len(scene_frames) == 12


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
    # dedup=False so fills survive on this static fixture — the point here is
    # the filename-prefix contract, not the rejection rule.
    frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), out_dir, fps=0.5, target_frames=60, max_frames=100,
        dedup=False, full_duration=720.0,
    )
    assert len(list(out_dir.glob("cue_*.jpg"))) == 3
    assert all(Path(c["path"]).exists() for c in cues)
    assert list(out_dir.glob("fill_*.jpg"))


def _fake_decode(monkeypatch) -> list[float]:
    """Route _decode_frame_at to a stub that records the requested times and
    writes a placeholder JPEG, so the fill loop's geometry runs without ffmpeg."""
    order: list[float] = []

    def fake(video_path, t, path, resolution, crop):
        order.append(t)
        Path(path).write_bytes(b"jpg")
        return True, ""

    monkeypatch.setattr(frames, "_decode_frame_at", fake)
    return order


def _selected_at(dirpath: Path, stamps: list[float]) -> list[dict]:
    dirpath.mkdir(parents=True, exist_ok=True)
    out = []
    for i, t in enumerate(stamps):
        p = dirpath / f"frame_{i:04d}.jpg"
        p.write_bytes(b"x")
        out.append({"index": i, "timestamp_seconds": t, "path": str(p), "reason": "scene-change"})
    return out


def test_gap_fill_bisects_the_widest_hole_first(monkeypatch, tmp_path: Path):
    """Pure geometry, no ffmpeg: the placement rule minimises the worst gap
    rather than spreading frames evenly."""
    _fake_decode(monkeypatch)
    selected = _selected_at(tmp_path, [0.0, 10.0, 100.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=3, resolution=512, crop=None,
        window_end=100.0, dedup=False,
    )
    # 90-wide hole first, then its 45-wide halves — never the 10-wide one.
    fills = sorted(f["timestamp_seconds"] for f in out if f["reason"] == "gap-fill")
    assert fills == [32.5, 55.0, 77.5]
    assert meta["filled"] == 3
    assert meta["stop"] == "budget"


def test_gap_fill_respects_the_minimum_gap(monkeypatch, tmp_path: Path):
    _fake_decode(monkeypatch)
    selected = _selected_at(tmp_path, [0.0, 1.0, 2.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=10, resolution=512, crop=None,
        window_end=2.0, dedup=False,
    )
    assert meta["filled"] == 0
    assert meta["stop"] == "min-gap"
    assert out == selected


def test_gap_fill_covers_the_tail_after_the_last_frame(monkeypatch, tmp_path: Path):
    """The trailing hole is routinely the worst one: a closing shot has exactly
    one detected frame, at its start."""
    _fake_decode(monkeypatch)
    selected = _selected_at(tmp_path, [0.0, 1.0])
    out, _meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=1, resolution=512, crop=None,
        window_end=100.0, dedup=False,
    )
    fills = [f["timestamp_seconds"] for f in out if f["reason"] == "gap-fill"]
    assert fills == [50.5]


def test_gap_fill_survives_a_single_frame_and_unknown_end(monkeypatch, tmp_path: Path):
    """Regression: one surviving frame + no window end used to IndexError on an
    empty segment list after the budget check passed."""
    _fake_decode(monkeypatch)
    selected = _selected_at(tmp_path, [5.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=3, resolution=512, crop=None,
        window_end=None, dedup=False,
    )
    assert out == selected
    assert meta["filled"] == 0


def test_gap_fill_rejects_candidates_dedup_would_delete(monkeypatch, tmp_path: Path):
    """If dedup would have deleted the frame, don't create it: a fill matching
    any known neighbour is unlinked and its hole retired."""
    _fake_decode(monkeypatch)
    flat = bytes([100] * (frames.DEDUP_THUMB * frames.DEDUP_THUMB))
    monkeypatch.setattr(frames, "_thumb_frames", lambda paths: [flat] * len(paths))
    selected = _selected_at(tmp_path, [0.0, 100.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=10, resolution=512, crop=None,
        window_end=100.0, dedup=True,
    )
    assert meta["filled"] == 0
    assert meta["rejected"] == 1          # one probe retired the only hole
    assert meta["stop"] == "saturated"
    assert len(out) == 2
    assert not list(tmp_path.glob("fill_*.jpg"))


def test_gap_fill_fails_open_when_thumbnails_unavailable(monkeypatch, tmp_path: Path):
    """A failed thumbnail pass must degrade to unchecked filling — the same
    fail-open contract dedup itself has — not to no filling."""
    _fake_decode(monkeypatch)
    monkeypatch.setattr(frames, "_thumb_frames", lambda paths: [])
    selected = _selected_at(tmp_path, [0.0, 100.0])
    _out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=3, resolution=512, crop=None,
        window_end=100.0, dedup=True,
    )
    assert meta["filled"] == 3
    assert meta["rejected"] == 0


def test_gap_fill_counts_failed_decodes(monkeypatch, tmp_path: Path):
    def fake(video_path, t, path, resolution, crop):
        return False, "boom"

    monkeypatch.setattr(frames, "_decode_frame_at", fake)
    selected = _selected_at(tmp_path, [0.0, 100.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=3, resolution=512, crop=None,
        window_end=100.0, dedup=False,
    )
    assert meta["filled"] == 0
    assert meta["failed"] == 1            # the hole is retired, not retried
    assert out == selected


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
        full_duration=720.0,
    )
    shots = meta["shots"]
    assert shots["cuts"] == 11
    assert shots["per_minute"] == pytest.approx(0.9, abs=0.1)
    assert shots["longest_s"] == pytest.approx(250.0, abs=1.0)


def test_shot_stats_degrade_deterministically_without_a_window(
    sparse_cuts_clip: Path, tmp_path: Path
):
    """A direct caller that passes neither end_seconds nor full_duration gets
    the documented degraded stats (cut-span denominator, no closing shot) —
    deterministically, not on the luck of a hidden re-probe that used to swallow
    its own failure."""
    _, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=60, max_frames=100,
    )
    shots = meta["shots"]
    assert shots["cuts"] == 11
    # Span-based: 11 cuts over the 5..470s cut span, not the 720s clip.
    assert shots["per_minute"] == pytest.approx(11 / (465.0 / 60.0), abs=0.1)
    assert shots["longest_s"] == pytest.approx(200.0, abs=1.0)


def test_scene_engine_does_not_probe_metadata_internally(
    monkeypatch, sparse_cuts_clip: Path, tmp_path: Path
):
    """The window end is threaded down from the caller, never re-probed: the old
    hidden get_metadata call swallowed SystemExit and silently degraded the shot
    stats exactly when ffprobe was struggling."""
    def boom(video_path):
        raise AssertionError("extract_scene_or_uniform must not call get_metadata")

    monkeypatch.setattr(frames, "get_metadata", boom)
    _, meta = frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "f", fps=0.5, target_frames=60, max_frames=100,
        full_duration=720.0,
    )
    assert meta["shots"]["cuts"] == 11


def test_token_burner_covers_at_least_as_much_as_balanced(
    sparse_moving_clip: Path, tmp_path: Path
):
    """The maximum-fidelity mode must not return fewer frames than the default —
    under PRODUCTION conditions.

    The first version of this test passed target_frames=100 to both arms and
    used a 720 s fixture, the one duration band where auto_fps returns the cap —
    so it could not fail while watch.py's real call (target = auto_fps's 40-80
    on a 1-10 minute clip) returned 20% fewer frames from token-burner than from
    balanced. This one derives the target the way watch.py does and asserts the
    band first.
    """
    fps, target = frames.auto_fps(300.0, max_frames=100)
    assert target < 100, "precondition: the duration band that exposes the bug"
    balanced, _ = frames.extract_scene_or_uniform(
        str(sparse_moving_clip), tmp_path / "b", fps=fps, target_frames=target,
        max_frames=100, full_duration=300.0,
    )
    burner, meta = frames.extract_scene_or_uniform(
        str(sparse_moving_clip), tmp_path / "t", fps=fps, target_frames=target,
        max_frames=None, full_duration=300.0,
    )
    assert len(burner) >= len(balanced), f"token-burner {len(burner)} < balanced {len(balanced)}"
    assert meta["gap_filled"] > 0
    # Every detected shot still survives — filling is additive.
    assert sum(1 for f in burner if f["reason"] in ("first-frame", "scene-change")) >= 12


def test_uncapped_fill_budget_is_at_least_the_capped_one(
    monkeypatch, sparse_cuts_clip: Path, tmp_path: Path
):
    """The F1 invariant pinned at the budget level, decoupled from how many
    fills survive the duplicate check: both arms must hand _fill_time_gaps the
    same coverage target when the duration target sits under the cap."""
    targets: list[int] = []
    real = frames._fill_time_gaps

    def spy(video_path, out_dir, selected, budget, **kwargs):
        targets.append(budget + len(selected))
        return real(video_path, out_dir, selected, budget, **kwargs)

    monkeypatch.setattr(frames, "_fill_time_gaps", spy)
    frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "b", fps=0.5, target_frames=80,
        max_frames=100, full_duration=720.0,
    )
    frames.extract_scene_or_uniform(
        str(sparse_cuts_clip), tmp_path / "t", fps=0.5, target_frames=80,
        max_frames=None, full_duration=720.0,
    )
    assert len(targets) == 2
    assert targets[1] >= targets[0] == 100


def test_uncapped_fill_does_not_shrink_a_cut_heavy_clip(cut_clip: Path, tmp_path: Path):
    """A clip with more detected shots than the duration budget keeps them all
    and fills nothing — the budget is a floor for coverage, not a ceiling."""
    out, meta = frames.extract_scene_or_uniform(
        str(cut_clip), tmp_path / "f", fps=2.0, target_frames=5, max_frames=None,
    )
    assert len(out) == meta["candidate_count"] == 13
    assert meta["gap_filled"] == 0


def test_uniform_frames_carry_measured_times_not_slot_times(vfr_clip: Path, tmp_path: Path):
    """extract() selects source frames instead of resampling to a rate.

    `-vf fps=N` stamps each output with its *slot* time, which on a held-frame
    source is early by up to half a sampling period and unrecoverable afterwards.
    The give-away is that measured times are not multiples of the interval: a
    resampler's output always is.
    """
    out = frames.extract(
        str(vfr_clip), tmp_path / "f", fps=4.0, max_frames=50,
        start_seconds=0.0, end_seconds=3.0,
    )
    assert len(out) > 4
    stamps = [f["timestamp_seconds"] for f in out]
    assert stamps == sorted(stamps)
    off_grid = [t for t in stamps if abs(t / 0.25 - round(t / 0.25)) > 0.01]
    assert off_grid, f"every stamp landed on the 0.25s grid, so these are slot times: {stamps}"


def test_uniform_cap_thins_across_the_clip_rather_than_truncating(
    static_clip: Path, tmp_path: Path
):
    """`-frames:v N` stops ffmpeg after N frames, which keeps the FIRST N and
    drops the tail. It only shows when the rate and the cap are set
    independently — normally both derive from the same budget so the counts
    match — but then it is severe: fps=2 with a cap of 3 on a 3s clip returned
    0.0, 0.5, 1.0 and nothing from the last two thirds, while the report said
    "full range"."""
    out = frames.extract(str(static_clip), tmp_path / "f", fps=2.0, max_frames=3)
    stamps = [f["timestamp_seconds"] for f in out]
    assert len(out) == 3
    assert stamps[0] == 0.0
    assert stamps[-1] > 2.0, f"cap truncated the tail: {stamps}"
    # _even_sample's cleanup contract: the frames it dropped are gone from disk.
    assert len(list((tmp_path / "f").glob("frame_*.jpg"))) == 3
    assert [f["index"] for f in out] == [0, 1, 2]


def test_uniform_under_the_cap_is_untouched(static_clip: Path, tmp_path: Path):
    out = frames.extract(str(static_clip), tmp_path / "f", fps=1.0, max_frames=100)
    assert 1 < len(out) <= 100
    assert [f["index"] for f in out] == list(range(len(out)))


# --- containers with no duration header ---------------------------------------


def test_headerless_container_really_has_no_duration(headerless_clip: Path):
    """Fixture precondition: if ffprobe ever starts reporting a duration here,
    the tests below stop testing anything."""
    import subprocess as sp

    declared = sp.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(headerless_clip)],
        capture_output=True, text=True,
    ).stdout.strip()
    assert declared in ("", "N/A"), f"container declared {declared!r}"


def test_duration_is_recovered_by_demuxing(headerless_clip: Path):
    """A zero duration used to propagate: auto_fps(0) targets one frame, so the
    whole video came back as a single frame and the report said 00:00."""
    meta = frames.get_metadata(str(headerless_clip))
    assert meta["duration_seconds"] == pytest.approx(5.6, abs=0.4)
    assert frames.auto_fps(meta["duration_seconds"])[1] > 1


def test_scan_duration_is_a_demux_not_a_decode(headerless_clip: Path):
    """It stream-copies to null, so it stays cheap enough to run on any clip
    whose header is missing — measured 19ms on a 5.6s source."""
    import time as _time

    start = _time.perf_counter()
    seconds = frames.scan_duration(str(headerless_clip))
    assert seconds == pytest.approx(5.6, abs=0.4)
    assert _time.perf_counter() - start < 2.0


def test_scan_duration_returns_zero_on_junk(tmp_path: Path):
    """Fail back to where the caller already was, never raise."""
    junk = tmp_path / "junk.mkv"
    junk.write_bytes(b"not a video\n" * 200)
    assert frames.scan_duration(str(junk)) == 0.0


def test_headerless_clip_gets_normal_frame_coverage(headerless_clip: Path, tmp_path: Path):
    out, meta = frames.extract_scene_or_uniform(
        str(headerless_clip), tmp_path / "f", fps=2.0, target_frames=20, max_frames=100,
    )
    assert len(out) > 5
    assert max(f["timestamp_seconds"] for f in out) > 4.0


# --- fps bounds ----------------------------------------------------------------


def test_extract_rejects_a_non_positive_rate(static_clip: Path, tmp_path: Path):
    """fps <= 0 used to make the select expression pass EVERY decoded frame
    (~18,000 JPEGs on a 10-minute clip) before thinning. No caller legitimately
    wants that; motion mode has its own path."""
    with pytest.raises(SystemExit, match="must be positive"):
        frames.extract(str(static_clip), tmp_path / "f", fps=0.0)
    with pytest.raises(SystemExit, match="must be positive"):
        frames.extract(str(static_clip), tmp_path / "f", fps=-1.0)


def test_module_cli_rejects_a_non_positive_fps(static_clip: Path, tmp_path: Path):
    import subprocess
    import sys as _sys

    proc = subprocess.run(
        [_sys.executable, str(Path(frames.__file__)), str(static_clip),
         str(tmp_path / "out"), "--fps", "0"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "must be positive" in proc.stderr


def test_module_cli_clamps_and_reports_a_high_fps(static_clip: Path, tmp_path: Path):
    import subprocess
    import sys as _sys

    proc = subprocess.run(
        [_sys.executable, str(Path(frames.__file__)), str(static_clip),
         str(tmp_path / "out"), "--fps", "25"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "exceeds" in proc.stderr and "sampling at 2 fps" in proc.stderr


# --- extract_at_timestamps failure accounting ----------------------------------


def test_timestamp_extraction_counts_decode_failures(monkeypatch, tmp_path: Path):
    """A failed per-frame decode used to vanish: no counter, no stderr, and the
    report explained the shortfall with the only number it had (out-of-window),
    which was wrong."""
    calls = {"n": 0}

    def flaky(video_path, t, path, resolution, crop):
        calls["n"] += 1
        if calls["n"] == 2:
            return False, "Invalid data found when processing input"
        Path(path).write_bytes(b"jpg")
        return True, ""

    monkeypatch.setattr(frames, "_decode_frame_at", flaky)
    out, meta = frames.extract_at_timestamps(
        "v.mp4", tmp_path, [1.0, 2.0, 3.0],
    )
    assert len(out) == 2
    assert meta["selected_count"] == 2
    assert meta["extraction_failed"] == 1
    assert [f["index"] for f in out] == [0, 1]


# --- estimated timestamps are marked, never silent ------------------------------


def _short_showinfo_run(monkeypatch, n_files: int, n_stamps: int):
    """Fake the extraction subprocess: writes n_files JPEGs but reports only
    n_stamps pts_time lines — the showinfo-shortfall degraded path."""
    def fake_run(cmd, *args, **kwargs):
        pattern = cmd[-1]
        for i in range(1, n_files + 1):
            Path(pattern % i).write_bytes(b"jpg")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = "\n".join(
                f"[Parsed_showinfo] n:{i} pts_time:{i * 0.5:.6f}" for i in range(n_stamps)
            )

        return _Result()

    monkeypatch.setattr(frames.subprocess, "run", fake_run)


def test_uniform_stamp_shortfall_is_marked_estimated(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr(frames, "frame_sync_args", lambda: ())
    monkeypatch.setattr(frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    tmp_path.mkdir(parents=True, exist_ok=True)
    _short_showinfo_run(monkeypatch, n_files=4, n_stamps=2)
    out = frames.extract("v.mp4", tmp_path, fps=2.0, max_frames=10)
    assert len(out) == 4
    assert [bool(f.get("estimated")) for f in out] == [False, False, True, True]
    assert "marked estimated" in capsys.readouterr().err


def test_scene_stamp_shortfall_is_marked_and_excluded_from_shot_stats(
    monkeypatch, tmp_path: Path, capsys
):
    """Extras used to collapse onto the window start — several frames sharing one
    fabricated time — and those fabrications flowed straight into shot_stats."""
    monkeypatch.setattr(frames, "frame_sync_args", lambda: ())
    monkeypatch.setattr(frames.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    _short_showinfo_run(monkeypatch, n_files=5, n_stamps=3)
    out = frames.extract_scene_candidates("v.mp4", tmp_path)
    assert len(out) == 5
    assert [bool(f.get("estimated")) for f in out] == [False, False, False, True, True]
    # Estimated frames carry the LAST measured stamp, not the window start.
    assert out[3]["timestamp_seconds"] == out[2]["timestamp_seconds"]
    assert out[4]["timestamp_seconds"] == out[2]["timestamp_seconds"]
    assert "estimated" in capsys.readouterr().err
    # And shot_stats sees only the measured ones (the caller filters).
    measured = [f["timestamp_seconds"] for f in out if not f.get("estimated")]
    assert len(measured) == 3


# --- motion token estimate -------------------------------------------------------


def test_motion_token_estimate_matches_the_extraction_arithmetic():
    # 2s at 60fps under a 2000 cap: every source frame, 120 of them.
    est, per, total = frames.motion_token_estimate(2.0, 60.0, 2000, 1920, 1080, 512, None)
    assert est == 120
    assert per == frames.image_tokens(512, 288)
    assert total == est * per


def test_motion_token_estimate_assumes_the_cap_without_a_rate():
    est, _, _ = frames.motion_token_estimate(60.0, None, 2000, 640, 360, 512, None)
    assert est == 2000


def test_motion_token_estimate_is_unguardable_without_dimensions():
    est, per, total = frames.motion_token_estimate(2.0, 60.0, 2000, None, None, 512, None)
    assert per == 0 and total == 0


def test_explicit_max_frames_at_full_resolution_on_4k_exceeds_the_hard_ceiling():
    """The bypass the hard ceiling exists for: 2000 frames at --resolution 1998
    on a 4K source is ~6M image tokens — no answer needs that."""
    _, _, total = frames.motion_token_estimate(35.0, 60.0, 2000, 3840, 2160, 1998, None)
    assert total > frames.MOTION_TOKEN_HARD_CEILING
    # ...while the largest defensible run — the full cap at default resolution —
    # stays under it and is permitted.
    _, _, sane = frames.motion_token_estimate(35.0, 60.0, 2000, 1920, 1080, 512, None)
    assert sane < frames.MOTION_TOKEN_HARD_CEILING


def test_gap_fill_probes_past_a_one_sided_duplicate(monkeypatch, tmp_path: Path):
    """The trailing-hole rescue, pinned: a probe matching the only known bound
    must not retire the unexplored half. Content appearing mid-way through a
    long static tail — a box at t=7 on a gray screen — is exactly what
    gap-fill exists to find, and the first rejection rule lost it."""
    times: dict[str, float] = {}

    def fake_decode(video_path, t, path, resolution, crop):
        Path(path).write_bytes(b"jpg")
        times[str(path)] = t
        return True, ""

    gray = bytes([100] * (frames.DEDUP_THUMB * frames.DEDUP_THUMB))
    white = bytes([250] * (frames.DEDUP_THUMB * frames.DEDUP_THUMB))

    def fake_thumbs(paths):
        return [white if times.get(str(p), 0.0) >= 7.0 else gray for p in paths]

    monkeypatch.setattr(frames, "_decode_frame_at", fake_decode)
    monkeypatch.setattr(frames, "_thumb_frames", fake_thumbs)
    selected = _selected_at(tmp_path, [0.5])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=6, resolution=512, crop=None,
        window_end=10.0, dedup=True,
    )
    kept = [f["timestamp_seconds"] for f in out if f["reason"] == "gap-fill"]
    assert any(t >= 7.0 for t in kept), (kept, meta)
    assert meta["rejected"] >= 1  # the gray probes were still redundant frames


def test_static_hole_between_known_bounds_is_retired_without_keeping_frames(
    monkeypatch, tmp_path: Path
):
    """The other direction: bounds known and matching on both sides of every
    probe -> no frames kept, holes retired (the 89%-padding fix must survive
    the one-sided re-queue rule)."""
    _fake_decode(monkeypatch)
    flat = bytes([100] * (frames.DEDUP_THUMB * frames.DEDUP_THUMB))
    monkeypatch.setattr(frames, "_thumb_frames", lambda paths: [flat] * len(paths))
    selected = _selected_at(tmp_path, [0.0, 100.0])
    out, meta = frames._fill_time_gaps(
        "v.mp4", tmp_path, selected, budget=10, resolution=512, crop=None,
        window_end=100.0, dedup=True,
    )
    assert meta["filled"] == 0
    assert meta["stop"] == "saturated"
    assert len(out) == 2


def test_estimated_stamps_are_excluded_from_shot_stats_in_production(
    monkeypatch, tmp_path: Path
):
    """The caller-side filter in extract_scene_or_uniform, exercised directly:
    fabricated (estimated) stamps must not enter the shot statistics."""
    cands = _selected_at(tmp_path, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    for frame in cands[6:]:
        frame["estimated"] = True
        frame["timestamp_seconds"] = 5.0  # the fabricated copy of the last stamp
    monkeypatch.setattr(frames, "extract_scene_candidates", lambda *a, **k: list(cands))
    monkeypatch.setattr(
        frames, "_fill_time_gaps",
        lambda video_path, out_dir, selected, budget, **k: (
            selected, {"filled": 0, "rejected": 0, "failed": 0, "stop": None}
        ),
    )
    _out, meta = frames.extract_scene_or_uniform(
        "v.mp4", tmp_path, fps=1.0, target_frames=10, max_frames=10,
        dedup=False, full_duration=10.0,
    )
    # 6 measured stamps (0..5) -> 5 cuts. With the fabrications included the
    # count would read 7.
    assert meta["shots"]["cuts"] == 5
