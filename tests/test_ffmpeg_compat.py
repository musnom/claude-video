"""ffmpeg frame-sync flag selection and the keyframe-less-range fallback.

ffmpeg 5.1 renamed -vsync to -fps_mode and 9.0 removed -vsync outright, while
-fps_mode does not exist before 5.1. We only have one ffmpeg here, so the
selection logic is driven through a fake that simulates each era's option table;
the two real-ffmpeg tests at the bottom are what catch the flag going missing
entirely on the binary we do have.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import frames


@pytest.fixture(autouse=True)
def clear_probe_cache():
    """Drop the memo before *and* after each test.

    After matters as much as before: without it a fake probe answer leaks into
    the real-ffmpeg tests later in the same session.
    """
    _clear()
    yield
    # Defensive: tests that monkeypatch frame_sync_args with a plain function
    # may not have been undone yet when this teardown runs.
    _clear()


def _clear() -> None:
    for fn in (frames.frame_sync_args, frames._ffmpeg_accepts_option):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch, *, known: set[str]) -> list[list[str]]:
    """Simulate an ffmpeg whose option table contains only ``known``.

    known={"fps_mode", "vsync"} -> 5.1 through 8.x
    known={"fps_mode"}          -> 9.0
    known={"vsync"}             -> 4.4
    known=set()                 -> hypothetical build with neither
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        unknown = [
            tok[1:] for tok in cmd
            if isinstance(tok, str)
            and tok in ("-fps_mode", "-vsync")
            and tok[1:] not in known
        ]

        class _Result:
            returncode = 8 if unknown else 0
            stdout = ""
            stderr = (
                f"Unrecognized option '{unknown[0]}'.\n"
                "Error splitting the argument list.\n" if unknown else ""
            )

        return _Result()

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    return calls


def _probe_calls(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "nullsrc=d=0.04" in c]


# --- flag selection ----------------------------------------------------------

def test_modern_ffmpeg_prefers_fps_mode(monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, known={"fps_mode", "vsync"})
    assert frames.frame_sync_args() == ("-fps_mode", "vfr")
    # -vsync would also work on 5.1-8.x, but probing it is wasted work.
    assert len(_probe_calls(calls)) == 1


def test_ffmpeg_9_uses_fps_mode(monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, known={"fps_mode"})
    assert frames.frame_sync_args() == ("-fps_mode", "vfr")
    assert len(_probe_calls(calls)) == 1


def test_ffmpeg_44_falls_back_to_vsync(monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, known={"vsync"})
    assert frames.frame_sync_args() == ("-vsync", "vfr")
    probes = _probe_calls(calls)
    assert len(probes) == 2, "should try fps_mode, then fall back"
    assert "-fps_mode" in probes[0] and "-vsync" in probes[1]


def test_neither_flag_degrades_to_empty(monkeypatch, capsys):
    _fake_ffmpeg(monkeypatch, known=set())
    assert frames.frame_sync_args() == ()
    err = capsys.readouterr().err
    assert "neither -fps_mode nor -vsync" in err


def test_result_is_memoized_per_process(monkeypatch):
    calls = _fake_ffmpeg(monkeypatch, known={"fps_mode", "vsync"})
    for _ in range(3):
        frames.frame_sync_args()
    assert len(_probe_calls(calls)) == 1


def test_probe_tests_exactly_what_it_returns(monkeypatch):
    """Guards a probe that validates something other than the shipped argv."""
    calls = _fake_ffmpeg(monkeypatch, known={"fps_mode", "vsync"})
    pair = frames.frame_sync_args()
    probe = _probe_calls(calls)[0]
    assert _contains_pair(probe, pair)
    assert "-frames:v" in probe and "null" in probe


def test_inconclusive_failure_keeps_fps_mode(monkeypatch):
    """Fail open: never demote to a removed option on an ambiguous signal."""
    def fake_run(cmd, *args, **kwargs):
        class _Result:
            returncode = 1
            stdout = ""
            stderr = "Unknown input format: 'lavfi'"
        return _Result()

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    assert frames.frame_sync_args() == ("-fps_mode", "vfr")


def test_unspawnable_ffmpeg_does_not_crash(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    assert frames.frame_sync_args() == ("-fps_mode", "vfr")


# --- the flag actually reaches both extraction call sites --------------------

def _contains_pair(argv: list[str], pair: tuple[str, ...]) -> bool:
    """True if ``pair`` appears contiguously in ``argv``."""
    if not pair:
        return True
    return any(
        tuple(argv[i:i + len(pair)]) == pair
        for i in range(len(argv) - len(pair) + 1)
    )


def _capture_extraction_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record extraction argv with the probe held constant.

    frame_sync_args is monkeypatched rather than subprocess.run, because
    otherwise the probe consumes calls[0] and the assertions drift by one.
    """
    monkeypatch.setattr(frames, "frame_sync_args", lambda: ("-SENTINEL", "vfr"))
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    return calls


def test_scene_argv_carries_sync_flag(monkeypatch, tmp_path):
    calls = _capture_extraction_argv(monkeypatch)
    frames.extract_scene_candidates("video.mp4", tmp_path, max_frames=None)
    argv = calls[0]
    assert _contains_pair(argv, ("-SENTINEL", "vfr"))
    assert argv.index("-SENTINEL") > argv.index("-vf")


def test_keyframe_argv_carries_sync_flag(monkeypatch, tmp_path):
    calls = _capture_extraction_argv(monkeypatch)
    frames.extract_keyframes("video.mp4", tmp_path, max_frames=None)
    argv = calls[0]
    assert _contains_pair(argv, ("-SENTINEL", "vfr"))
    assert argv.index("-SENTINEL") > argv.index("-vf")
    assert argv.index("-SENTINEL") < argv.index("-q:v")


# --- real ffmpeg: the flag is load-bearing -----------------------------------
# Without it the image2 muxer expands our sparse selection back to constant
# frame rate. cut_clip is 5.6s at 10fps, so a lost flag turns ~14 candidates
# into ~56. The margin is wide and stable.

def test_scene_candidates_are_not_cfr_expanded(cut_clip: Path, tmp_path):
    out = frames.extract_scene_candidates(str(cut_clip), tmp_path, max_frames=None)
    assert len(out) <= 20, f"looks CFR-expanded: {len(out)} frames"
    assert len(out) == len(list(tmp_path.glob("frame_*.jpg")))


def test_keyframes_are_not_cfr_expanded(cut_clip: Path, tmp_path):
    out, meta = frames.extract_keyframes(str(cut_clip), tmp_path, max_frames=None)
    assert len(out) <= 20, f"looks CFR-expanded: {len(out)} frames"
    assert meta["engine"] == "keyframe"


# --- keyframe-less range fallback --------------------------------------------

def test_keyframe_fallback_when_range_has_no_keyframes(static_clip: Path, tmp_path):
    """static_clip is -g 600, so its only keyframe is at t=0.

    Seeking past it starves the mjpeg encoder and ffmpeg fails at encoder init
    rather than exiting 0 empty, which used to raise before the uniform
    fallback could run. `balanced` handled the same window fine.
    """
    out, meta = frames.extract_keyframes(
        str(static_clip), tmp_path, start_seconds=1.0, end_seconds=3.0
    )
    assert out, "expected the uniform fallback to produce frames"
    assert meta["engine"] == "uniform"
    assert meta["fallback"] is True


def test_both_detail_engines_survive_keyframeless_window(static_clip: Path, tmp_path):
    """efficient and balanced must not disagree about the same window."""
    kf, kf_meta = frames.extract_keyframes(
        str(static_clip), tmp_path / "kf", start_seconds=1.0, end_seconds=3.0
    )
    sc, sc_meta = frames.extract_scene_or_uniform(
        str(static_clip), tmp_path / "sc", fps=1.0, target_frames=5,
        start_seconds=1.0, end_seconds=3.0,
    )
    assert kf and sc
    assert kf_meta["engine"] == sc_meta["engine"] == "uniform"


def test_corrupt_input_still_raises(tmp_path):
    """The gate must not turn a genuinely broken file into a silent no-op."""
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video\n" * 400)
    with pytest.raises(SystemExit):
        frames.extract_keyframes(str(junk), tmp_path / "out")


def test_partial_output_failure_still_raises(monkeypatch, tmp_path):
    """rc != 0 with files on disk means a real mid-run failure — keep raising.

    Real ffmpeg is hard to coax into this state, so drive it with a stub.
    """
    def fake_run(cmd, *args, **kwargs):
        for tok in cmd:
            if isinstance(tok, str) and tok.endswith("frame_%04d.jpg"):
                path = Path(tok.replace("%04d", "0000"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")

        class _Result:
            returncode = 1
            stdout = ""
            stderr = "boom decode error"

        return _Result()

    monkeypatch.setattr(frames, "frame_sync_args", lambda: ())
    monkeypatch.setattr(frames.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="boom decode error"):
        frames.extract_keyframes("video.mp4", tmp_path / "out")


def test_efficient_detail_survives_keyframeless_window(static_clip: Path):
    """The user-visible bug, end to end: `--detail efficient` over a window
    with no keyframe used to exit non-zero."""
    from test_watch import _run  # same driver the rest of the suite uses

    out = _run(static_clip, "--detail", "efficient", "--start", "1", "--end", "3")
    assert "**Frames:**" in out
