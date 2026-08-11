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


def test_non_cue_frame_labels_stay_whole_seconds(cut_clip: Path):
    """No-default-change guard: scene/keyframe/uniform labels are unchanged."""
    for detail in ("balanced", "efficient"):
        out = _run(cut_clip, "--detail", detail)
        stamps = re.findall(r"\(t=([0-9:.]+), reason=", out)
        assert stamps, out
        assert not any("." in s for s in stamps), f"{detail}: {stamps}"


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
