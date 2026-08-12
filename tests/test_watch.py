"""End-to-end routing of --detail through watch.py on a local clip."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

WATCH = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts" / "watch.py"


def _run(clip: Path, *args: str, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env.pop("WATCH_DETAIL", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(clip), "--no-whisper", *args],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_efficient_uses_keyframe_engine(cut_clip: Path):
    out = _run(cut_clip, "--detail", "efficient")
    assert "(keyframe" in out
    assert "**Detail:** efficient" in out


def test_balanced_uses_scene_engine(cut_clip: Path):
    out = _run(cut_clip, "--detail", "balanced")
    assert "(scene" in out
    assert "**Detail:** balanced" in out


def test_token_burner_uses_scene_engine(cut_clip: Path):
    out = _run(cut_clip, "--detail", "token-burner")
    assert "(scene" in out


def test_transcript_skips_frames(cut_clip: Path):
    out = _run(cut_clip, "--detail", "transcript")
    assert "skipped" in out
    assert "frame_0000.jpg" not in out


def test_flag_overrides_env(cut_clip: Path):
    out = _run(cut_clip, "--detail", "efficient", env_extra={"WATCH_DETAIL": "balanced"})
    assert "(keyframe" in out


def test_default_is_balanced(cut_clip: Path):
    out = _run(cut_clip)  # no flag, WATCH_DETAIL cleared
    assert "**Detail:** balanced" in out
    assert "(scene" in out


def test_timestamps_add_cue_frames_to_detail(cut_clip: Path):
    out = _run(cut_clip, "--detail", "balanced", "--timestamps", "1,3")
    assert "reason=transcript-cue" in out
    assert "reason=scene-change" in out  # detail frames still present (additive)


def test_timestamps_with_transcript_detail_is_cue_only(cut_clip: Path):
    out = _run(cut_clip, "--detail", "transcript", "--timestamps", "1,3")
    assert "reason=transcript-cue" in out
    assert "reason=scene-change" not in out
    assert "reason=keyframe" not in out


def _frame_lines(out: str) -> int:
    """Count frame rows in the report.

    Matches either path separator: watch.py prints whatever the OS produced, so
    on Windows these read `...\\frames\\frame_0000.jpg` and a hardcoded "/" makes
    every count come back 0 — which flips the assertions below in both
    directions, not just toward passing.
    """
    return sum(
        1
        for line in out.splitlines()
        if ("/frames/frame_" in line or "\\frames\\frame_" in line) and "(t=" in line
    )


def test_dedup_collapses_static_by_default(static_clip: Path):
    out = _run(static_clip)  # solid blue → identical frames collapse to one
    assert "near-duplicate" in out
    assert _frame_lines(out) == 1


def test_no_dedup_preserves_static_frames(static_clip: Path):
    out = _run(static_clip, "--no-dedup")
    assert "near-duplicate" not in out
    assert _frame_lines(out) > 1


def test_cue_frame_labels_have_millisecond_precision(cut_clip: Path):
    """Dense cue frames must not all collapse onto the same whole-second label.

    Before this, eight cue frames at 50ms spacing printed 00:00 then 00:01 seven
    times, putting the implied boundary a frame away from the real colour cut.
    """
    out = _run(
        cut_clip, "--detail", "transcript",
        "--timestamps", "0.50,0.55,0.60,0.65,0.70,0.75,0.80,0.85",
    )
    stamps = re.findall(r"\(t=([0-9:.]+), reason=transcript-cue\)", out)
    assert len(stamps) == 8, out
    assert len(set(stamps)) == 8, f"labels collapsed: {stamps}"
    assert all("." in s for s in stamps), stamps


def test_detail_frame_labels_carry_milliseconds(cut_clip: Path):
    """Was a guard that scene/keyframe/uniform labels stay whole seconds.

    That contract is now inverted, deliberately. Whole seconds cannot describe a
    clip that cuts more than once a second, and on a 120-shot clip that fit under
    the frame cap 20% of consecutive frames printed the identical timestamp — so
    the label stopped identifying the frame it was attached to. cut_clip cuts
    every 0.4s, which is exactly that regime.
    """
    for detail in ("balanced", "efficient"):
        out = _run(cut_clip, "--detail", detail)
        stamps = re.findall(r"\(t=([0-9:.]+), reason=", out)
        assert stamps, out
        assert all("." in s for s in stamps), f"{detail}: {stamps}"
        assert len(set(stamps)) == len(stamps), f"{detail}: labels collapsed: {stamps}"


def test_frame_labels_still_agree_with_the_transcript_clock(cut_clip: Path):
    """Finer labels must not become a second, competing clock.

    format_time is shared with transcribe.format_transcript so frame and
    transcript stamps line up; format_time_ms is that same value one order finer.
    Re-coarsening any frame label has to land back on the transcript's clock.
    """
    import sys as _sys
    _sys.path.insert(0, str(WATCH.parent))
    from frames import format_time, parse_time

    out = _run(cut_clip, "--detail", "balanced")
    stamps = re.findall(r"\(t=([0-9:.]+), reason=", out)
    assert stamps
    for stamp in stamps:
        assert format_time(parse_time(stamp)) == format_time(round(parse_time(stamp)))


# --- motion mode --------------------------------------------------------------


@pytest.mark.parametrize("detail", ["transcript", "efficient", "balanced", "token-burner"])
def test_motion_overrides_every_detail_mode(motion_clip: Path, detail):
    """--motion must never be a silent no-op.

    This is the failure that makes --fps useless: it reaches only the uniform
    fallback, so it does nothing under efficient or on scene-rich clips. The
    transcript case is the sharp one — that mode skips the video download
    entirely, so without the fix it yields zero frames.
    """
    out = _run(motion_clip, "--motion", "--detail", detail)
    assert "reason=motion" in out, f"{detail}: {out[:400]}"
    assert _frame_lines(out) > 50, detail


def test_motion_overrides_the_configured_default(motion_clip: Path):
    """Same trap, reached through WATCH_DETAIL rather than the flag."""
    out = _run(motion_clip, "--motion", env_extra={"WATCH_DETAIL": "transcript"})
    assert "reason=motion" in out
    assert _frame_lines(out) > 50


def test_motion_labels_carry_milliseconds(motion_clip: Path):
    out = _run(motion_clip, "--motion", "--start", "1", "--end", "1.5")
    stamps = re.findall(r"\(t=([0-9:.]+), reason=motion\)", out)
    assert len(stamps) > 20, out[:400]
    assert all("." in s for s in stamps), stamps[:5]
    assert len(set(stamps)) == len(stamps), "labels collapsed inside a second"


def test_motion_report_states_dedup_off_and_measured_rate(motion_clip: Path):
    out = _run(motion_clip, "--motion", "--start", "1", "--end", "1.5")
    assert "dedup off" in out
    assert "sampled" in out and "source ~" in out
    assert "Motion window:" in out


def test_motion_warns_when_thinned(motion_clip: Path):
    out = _run(motion_clip, "--motion", "--max-frames", "10")
    assert "Warning" in out and "cap" in out


def test_motion_ignores_fps_and_says_so(motion_clip: Path):
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(motion_clip), "--no-whisper", "--motion",
         "--fps", "30", "--start", "1", "--end", "1.2"],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    assert "--fps is ignored with --motion" in proc.stderr


# --- the download-skip trap ---------------------------------------------------
# transcript detail skips the video download when captions cover the request.
# Any mode that grabs frames must override that or it silently yields nothing.
# Only reachable end-to-end with a captioned URL, which this suite cannot use,
# so the condition is asserted directly.


def test_needs_pixels_for_motion():
    import watch
    assert watch.needs_pixels([], motion=True) is True


def test_needs_pixels_for_cue_timestamps():
    import watch
    assert watch.needs_pixels([1.0, 2.0], motion=False) is True


def test_needs_pixels_false_for_plain_transcript():
    """The audio-only fast path must survive — it is why transcript mode is cheap."""
    import watch
    assert watch.needs_pixels([], motion=False) is False


def test_motion_writes_stack_agnostic_data(slide_clip: Path, tmp_path):
    """motion.json is the machine-readable half: geometry, timing and a change
    signal, with no CSS, keyframes or easing names baked in."""
    import json

    out_dir = tmp_path / "w"
    _run(slide_clip, "--motion", "--out-dir", str(out_dir))
    data = json.loads((out_dir / "motion.json").read_text())

    assert set(data) == {"source", "crop", "window", "sampling", "envelope", "frames"}
    assert data["source"]["width"] == 640 and data["source"]["height"] == 360
    assert data["sampling"]["dedup"] is False
    assert data["sampling"]["mode"] == "every-source-frame"
    assert data["envelope"]["duration_ms"] == pytest.approx(300, abs=20)

    first = data["frames"][0]
    assert set(first) == {"i", "t", "gap_ms", "mean_delta", "peak_delta", "cum_delta", "path"}
    assert first["gap_ms"] is None
    assert all(f["gap_ms"] is not None for f in data["frames"][1:])
    # The cumulative curve is what the envelope is read off, so it has to be
    # monotone: a consumer plotting it is plotting "how much has changed so far".
    cum = [f["cum_delta"] for f in data["frames"]]
    assert cum[0] == 0.0
    assert all(b >= a for a, b in zip(cum, cum[1:])), cum

    # Both envelope branches return the same keys. They used to differ (4 on no
    # motion, 6 on motion) and the asymmetry leaked into this file.
    assert set(data["envelope"]) == {
        "first_motion", "last_motion", "duration_ms", "peak_at", "peak_delta",
        "total_change", "event_change", "floor", "min_change",
        "clipped_start", "clipped_end",
    }
    assert data["envelope"]["clipped_start"] is False
    assert data["envelope"]["clipped_end"] is False

    blob = json.dumps(data).lower()
    for leak in ("cubic-bezier", "keyframe", "ease-in", "css", "framer"):
        assert leak not in blob, f"stack-specific output leaked into the data: {leak}"


def test_motion_report_states_the_envelope(slide_clip: Path):
    out = _run(slide_clip, "--motion")
    assert "Motion envelope:" in out
    assert "Motion data:" in out
    assert "ms**" in out


def test_motion_json_records_the_crop(slide_clip: Path, tmp_path):
    import json

    out_dir = tmp_path / "w"
    _run(slide_clip, "--motion", "--crop", "40,150,500,60", "--out-dir", str(out_dir))
    data = json.loads((out_dir / "motion.json").read_text())
    assert data["crop"] == {"x": 40, "y": 150, "w": 500, "h": 60}


def test_motion_report_warns_when_the_window_clips_the_envelope(slide_clip: Path):
    """A duration that stops at the window edge is a lower bound. The report has
    to say so, because the motion workflow tells the reader to state the measured
    number and this is the case where that number is wrong by construction."""
    out = _run(slide_clip, "--motion", "--start", "1.0", "--end", "1.3")
    assert "Envelope clipped:" in out
    assert "lower bound" in out


def test_motion_report_is_quiet_when_the_window_contains_the_whole_move(slide_clip: Path):
    out = _run(slide_clip, "--motion", "--start", "0.5", "--end", "2.0")
    assert "Motion envelope:" in out
    assert "Envelope clipped:" not in out


def test_motion_end_to_end_on_an_eased_move(eased_clip_cubic: Path):
    """The Phase 1 end-to-end gate: a cubic ease-out through watch.py's own
    report. 433ms, not the nominal 500 — see test_fixtures.py for why the last
    67ms of an ease-out is not in the pixels."""
    out = _run(eased_clip_cubic, "--motion", "--start", "0.9", "--end", "1.6")
    match = re.search(r"\*\*(\d+) ms\*\*", out)
    assert match, out
    assert 410 <= int(match.group(1)) <= 460, out
    assert "Envelope clipped:" not in out


def test_motion_report_quantifies_a_no_change_verdict(static_clip: Path):
    """"No change detected" used to be unfalsifiable. It now carries the number
    it was compared against, so a reader can tell a still clip from a threshold
    that is set wrong."""
    out = _run(static_clip, "--motion", "--start", "0.5", "--end", "1.5")
    assert "no change detected" in out
    # The number the verdict was decided against, not a different one that was
    # never compared with it.
    assert "to count as motion" in out
    assert "noise floor" in out


# --- scene threshold and gap fill ----------------------------------------------


def test_graphic_cuts_report_shots_instead_of_a_uniform_fallback(graphic_cuts_clip: Path):
    """UC3's primary input. This used to print
    "1 selected from 1 candidates (uniform fallback, too few shots (1) …)"."""
    out = _run(graphic_cuts_clip, "--detail", "balanced")
    assert "(scene" in out
    assert "uniform fallback" not in out
    assert "reason=scene-change" in out


def test_scene_threshold_flag_changes_what_counts_as_a_cut(graphic_cuts_clip: Path):
    out = _run(graphic_cuts_clip, "--detail", "balanced", "--scene-threshold", "0.2")
    assert "uniform fallback" in out


def test_scene_threshold_is_rejected_when_out_of_range(cut_clip: Path):
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(cut_clip), "--no-whisper", "--scene-threshold", "5"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "between 0 and 1" in proc.stderr


@pytest.mark.parametrize(
    "extra,why",
    [
        (["--detail", "efficient"], "keyframes"),
        (["--motion", "--start", "1", "--end", "1.2"], "every source frame"),
    ],
)
def test_scene_threshold_says_so_when_it_does_nothing(motion_clip: Path, extra, why):
    """A silently ignored flag is treated as a bug in this codebase — the same
    rule --fps and --motion already follow."""
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(motion_clip), "--no-whisper",
         "--scene-threshold", "0.1", *extra],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--scene-threshold 0.1 had no effect" in proc.stderr
    assert why in proc.stderr


def test_gap_fill_is_reported_not_silent(sparse_moving_clip: Path):
    """Dozens of frames placed by the tool rather than found by detection is not
    a detail to leave out of the line that says where the frames came from."""
    out = _run(sparse_moving_clip, "--detail", "balanced")
    assert "added to fill gaps" in out
    assert "reason=gap-fill" in out
    assert "reason=scene-change" in out


def test_gap_fill_early_stop_is_reported(sparse_cuts_clip: Path):
    """When fills stop because the remaining candidates duplicate their
    neighbours, the report says so instead of leaving an unexplained gap between
    the frame count and the cap."""
    out = _run(sparse_cuts_clip, "--detail", "balanced")
    assert "fill stopped early" in out
    assert "near-duplicate candidate" in out
    assert "reason=gap-fill" not in out


def test_report_states_the_cut_rhythm(fast_cut_clip: Path):
    """`--detail balanced` caps at 100 frames; the shot line has to describe the
    video regardless of how many frames survived that cap — and as a lower
    bound, because detection misses cuts under the scene threshold."""
    out = _run(fast_cut_clip, "--detail", "balanced", "--max-frames", "6")
    match = re.search(
        r"\*\*Shots:\*\* at least (\d+) cuts \(detected at scene threshold ([0-9.]+)\), "
        r"([0-9.]+)/min — median ([0-9.]+)s",
        out,
    )
    assert match, out
    cuts, threshold = int(match.group(1)), float(match.group(2))
    per_minute, median = float(match.group(3)), float(match.group(4))
    assert cuts >= 20, out
    assert threshold == pytest.approx(0.05)
    assert 100 <= per_minute <= 130, out
    assert 0.45 <= median <= 0.55, out


def test_shot_line_echoes_the_requested_threshold(fast_cut_clip: Path):
    out = _run(fast_cut_clip, "--detail", "balanced", "--scene-threshold", "0.1")
    assert "detected at scene threshold 0.1)" in out


def test_shot_line_is_absent_when_there_are_no_shots(static_clip: Path):
    out = _run(static_clip, "--detail", "balanced")
    assert "**Shots:**" not in out


def test_token_burner_lists_every_cut(fast_cut_clip: Path):
    out = _run(fast_cut_clip, "--detail", "token-burner")
    assert "Cuts at:" in out
    times = re.search(r"Cuts at: (.+)", out).group(1).split(", ")
    assert len(times) >= 20
    assert all("." in t for t in times), times[:3]


# --- guards --------------------------------------------------------------------
# Three ways a run used to end badly: an unbounded --motion window that produced
# hundreds of thousands of image tokens without warning, a live URL that made
# yt-dlp record forever, and an audio-only file that aborted with a raw ffmpeg
# dump in the one case where the transcript path would have worked.


def _run_expecting_failure(clip: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WATCH), str(clip), "--no-whisper", *args],
        capture_output=True, text=True,
    )


def test_motion_refuses_an_unbounded_window(sparse_cuts_clip: Path):
    """A 12-minute clip with no --start/--end hits the 2000-frame cap, which at
    320x240 is ~204k image tokens — and the frames exist by the time any warning
    could print, so this has to block rather than warn."""
    proc = _run_expecting_failure(sparse_cuts_clip, "--motion")
    assert proc.returncode != 0
    assert "image tokens" in proc.stderr
    assert "ceiling" in proc.stderr
    # Actionable, not just a refusal.
    assert "--start" in proc.stderr and "--crop" in proc.stderr and "--max-frames" in proc.stderr


def test_motion_allows_an_explicit_frame_budget(sparse_cuts_clip: Path):
    """--max-frames means the user named the number, so the guard stands down."""
    out = _run(sparse_cuts_clip, "--motion", "--max-frames", "40")
    assert "reason=motion" in out


def test_motion_allows_a_tight_window(slide_clip: Path):
    out = _run(slide_clip, "--motion", "--start", "0.9", "--end", "1.6")
    assert "Motion envelope:" in out


def test_audio_only_input_returns_a_report_instead_of_an_ffmpeg_dump(tmp_path: Path):
    """`.m4a` used to exit 1 with zero stdout and a raw ffmpeg trace on stderr."""
    audio = tmp_path / "voice.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-t", "2", "-i", "sine=frequency=440:sample_rate=16000",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )
    proc = _run_expecting_failure(audio)
    assert proc.returncode == 0, proc.stderr
    assert "no video stream" in proc.stdout
    assert "# watch: video report" in proc.stdout
    assert "Traceback" not in proc.stderr


def test_still_image_gets_an_answer_not_an_encoder_error(tmp_path: Path):
    """A .png has a real width and height, so the audio-only path does not catch
    it; it died inside the mjpeg encoder instead. The useful answer is that
    /watch is the wrong tool for it."""
    image = tmp_path / "shot.png"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=teal:s=320x240", "-frames:v", "1", str(image)],
        check=True, capture_output=True,
    )
    proc = _run_expecting_failure(image)
    assert proc.returncode != 0
    assert "still image" in proc.stderr
    assert "Read the file directly" in proc.stderr
    assert "ff_frame_thread_encoder_init" not in proc.stderr


# --- live streams --------------------------------------------------------------
# Only reachable end-to-end with a live URL, which this suite cannot use, so the
# decision is asserted directly against the info dicts yt-dlp really emits.


def test_live_stream_is_refused():
    import watch
    with pytest.raises(SystemExit, match="live broadcast"):
        watch.refuse_if_live({"is_live": True, "live_status": "is_live"})


def test_live_flag_alone_is_enough():
    """Some extractors set is_live without live_status."""
    import watch
    with pytest.raises(SystemExit, match="live broadcast"):
        watch.refuse_if_live({"is_live": True})


def test_upcoming_stream_is_refused():
    import watch
    with pytest.raises(SystemExit, match="has not started"):
        watch.refuse_if_live({"is_live": False, "live_status": "is_upcoming"})


def test_finished_stream_still_processing_is_allowed():
    """post_live is a bounded artifact — the broadcast ended and the platform is
    still assembling the VOD. Refusing it would reject a normal recording."""
    import watch
    watch.refuse_if_live({"is_live": False, "live_status": "post_live"})


@pytest.mark.parametrize("info", [{}, {"live_status": "not_live"}, {"live_status": "was_live"}, {"is_live": False}])
def test_non_live_info_passes(info):
    """An empty dict is a normal outcome: fetch_captions passes --ignore-errors
    and never checks yt-dlp's return code, so "no info" must not read as "live"."""
    import watch
    watch.refuse_if_live(info)


# --- regressions found by review, not by the suite ----------------------------


def test_motion_on_a_source_with_no_video_stream_says_so(tmp_path: Path):
    """--motion on an audio file used to run no extractor at all and then print a
    full motion block anyway — sampled fps, gap range, envelope — computed from an
    empty frame list. Exactly the "report describes a code path that did not run"
    failure this whole change exists to remove."""
    audio = tmp_path / "voice.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-t", "2", "-i", "sine=frequency=440:sample_rate=16000",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(audio), "--no-whisper", "--motion"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "needs a video stream" in proc.stderr
    assert "Motion envelope" not in proc.stdout


def test_report_states_the_shot_rate_over_the_clip_not_the_cut_span(tmp_path: Path):
    """End to end: a clip that cuts eight times in four seconds and then holds.

    The rate used to be computed over the span between the first and last cut, so
    26 seconds of static card were excluded from the denominator and the report
    called it 120 cuts/min.
    """
    clip = tmp_path / "front_loaded.mp4"
    inputs = []
    colors = ["red", "green", "blue", "white", "black", "yellow", "cyan", "magenta"]
    for color in colors:
        inputs += ["-f", "lavfi", "-t", "0.5", "-i", f"color=c={color}:s=320x240:r=30"]
    inputs += ["-f", "lavfi", "-t", "26", "-i", "color=c=navy:s=320x240:r=30"]
    n = len(colors) + 1
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs,
         "-filter_complex", "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]",
         "-map", "[out]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-force_key_frames", "0.5,1,1.5,2,2.5,3,3.5,4", str(clip)],
        check=True, capture_output=True,
    )
    out = _run(clip, "--detail", "balanced")
    match = re.search(r"\*\*Shots:\*\* at least (\d+) cuts \([^)]*\), ([0-9.]+)/min.*longest ([0-9.]+)s", out)
    assert match, out
    cuts, per_minute, longest = int(match.group(1)), float(match.group(2)), float(match.group(3))
    # 30s clip: whatever the detector counted, the rate is that over 30s.
    assert per_minute == pytest.approx(cuts / 0.5, abs=0.5), out
    assert per_minute < 25, f"a 30s clip with {cuts} cuts is not {per_minute}/min"
    # ...and the 26s closing shot is the longest one, not a 0.5s colour block.
    assert longest > 20, out


def test_audio_only_report_does_not_advise_reading_frames_that_do_not_exist(tmp_path: Path):
    """"No transcript — proceed with frames only" is the standard fallback line
    and it is nonsense on a source that has no frames either. Following it sends
    the reader looking for images that were never produced."""
    audio = tmp_path / "voice.m4a"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-t", "2", "-i", "sine=frequency=440:sample_rate=16000",
         "-c:a", "aac", str(audio)],
        check=True, capture_output=True,
    )
    out = _run(audio)
    assert "no video stream" in out
    assert "proceed with frames only" not in out
    assert "nothing to report" in out


# --- --fps honesty --------------------------------------------------------------
# Two different code paths call themselves "uniform" and only one honours the
# caller's rate, so the warning cannot be keyed on the engine name.


@pytest.mark.parametrize(
    "detail,clip_name,should_warn,why",
    [
        ("balanced", "cut_clip", True, "scene engine selects by content"),
        ("efficient", "cut_clip", True, "keyframe engine selects by content"),
        ("efficient", "static_clip", True, "keyframe->uniform derives its own rate"),
        ("balanced", "static_clip", False, "scene->uniform samples at the caller's rate"),
    ],
)
def test_fps_says_so_exactly_when_it_did_nothing(detail, clip_name, should_warn, why, request):
    clip = request.getfixturevalue(clip_name)
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(clip), "--no-whisper",
         "--detail", detail, "--fps", "1"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    warned = "had no effect" in proc.stderr
    assert warned is should_warn, f"{why}: warned={warned}\n{proc.stderr[-400:]}"


def test_fps_really_does_bind_on_the_scene_uniform_fallback(static_clip: Path):
    """The one path where the flag works — asserted so the warning above cannot
    be "fixed" by making it fire everywhere."""
    sparse = _run(static_clip, "--no-dedup", "--fps", "0.5")
    dense = _run(static_clip, "--no-dedup", "--fps", "2")
    assert _frame_lines(sparse) < _frame_lines(dense)


def test_report_states_the_source_frame_rate(motion_clip: Path):
    """24p / 30p / 60p is a first-order style attribute and it cannot be
    recovered from the frames — they arrive as stills with no spacing. It used to
    appear only under --motion."""
    out = _run(motion_clip, "--detail", "transcript")
    assert re.search(r"\*\*Resolution:\*\* 320x240 @ 60 fps \(", out), out


def test_frame_rate_is_omitted_rather_than_faked_when_unknown(monkeypatch, cut_clip: Path):
    """parse_frame_rate returns None for ffprobe's several ways of saying "I
    don't know" (0/0, empty, missing). The line must drop the figure, not print
    a zero."""
    import sys as _sys
    _sys.path.insert(0, str(WATCH.parent))
    import frames as frames_mod

    assert frames_mod.parse_frame_rate("0/0") is None
    out = _run(cut_clip, "--detail", "transcript")
    assert "@ 0 fps" not in out


# --- --fps and --end bounds -----------------------------------------------------


def test_fps_clamp_is_reported(static_clip: Path):
    """--fps 25 used to sample at 2 fps with nothing on stderr — the user asked
    for a rate, got an eighth of it, and was told nothing (gaps.md G3)."""
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(static_clip), "--no-whisper",
         "--no-dedup", "--fps", "25"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "exceeds" in proc.stderr
    assert "25" in proc.stderr and "2 fps" in proc.stderr


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_non_positive_fps_is_rejected(static_clip: Path, bad: str):
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(static_clip), "--no-whisper", "--fps", bad],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "must be positive" in proc.stderr


def test_report_states_the_effective_uniform_rate(static_clip: Path):
    """When --fps really governed the sampling, the Frames line says the rate
    that ran — the other half of making the clamp visible."""
    out = _run(static_clip, "--no-dedup", "--fps", "1")
    assert "sampled at 1 fps" in out


def test_end_past_eof_is_clamped_and_reported(static_clip: Path):
    """--end 99:00 on a 3s clip used to print a Focus-range line for a range
    that does not exist and budget frames against the phantom duration."""
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(static_clip), "--no-whisper",
         "--start", "1", "--end", "99:00"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "clamped" in proc.stderr
    assert re.search(r"\*\*Focus range:\*\* 00:01 → 00:03", proc.stdout), proc.stdout


def test_bare_end_zero_is_rejected(static_clip: Path):
    """A zero-length window is a mistake, not a request — it used to produce an
    empty Frames section silently."""
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(static_clip), "--no-whisper", "--end", "0"],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "--end must be greater than 0" in proc.stderr


# --- motion cost visibility ------------------------------------------------------


def test_motion_prints_the_token_estimate_before_extracting(slide_clip: Path):
    proc = subprocess.run(
        [sys.executable, str(WATCH), str(slide_clip), "--no-whisper",
         "--motion", "--start", "0.9", "--end", "1.6"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "image tokens to read" in proc.stderr


def test_motion_frames_line_accounts_for_cue_frames(slide_clip: Path):
    """--motion --timestamps used to count only the motion frames in the Frames
    line while listing motion + cue frames below it."""
    out = _run(
        slide_clip, "--motion", "--start", "0.9", "--end", "1.6",
        "--timestamps", "1.0",
    )
    match = re.search(r"\*\*Frames:\*\* (\d+) \+ (\d+) cue frame", out)
    assert match, out
    motion_count, cue_count = int(match.group(1)), int(match.group(2))
    assert cue_count == 1
    # _frame_lines only matches frame_*.jpg; cue rows are cue_*.jpg.
    listed = len(re.findall(r"\(t=[0-9:.]+, reason=", out))
    assert listed == motion_count + cue_count
    # motion.json stays cue-free: it was serialized before the merge.
    assert "reason=transcript-cue" in out
