"""Shared pytest fixtures: environment isolation, ffmpeg-synthesized clips, and
scripts/ on sys.path."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Make the bundled scripts importable (mirrors watch.py's sys.path insert).
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Config keys /watch reads from the environment. Any of these leaking in from the
# developer's shell changes what the code under test does.
WATCH_ENV_VARS = (
    "WATCH_DETAIL",
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "SETUP_COMPLETE",
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Run every test against an empty HOME with no /watch env vars set.

    Without this the suite reads the developer's real ``~/.config/watch/.env``:
    the subprocess-driven tests in test_setup.py and test_watch.py inherit HOME,
    and ``whisper.load_api_key`` resolves ``Path.home()`` at call time. A machine
    with ``WATCH_DETAIL=transcript`` configured fails four tests on a clean
    checkout, because /watch then extracts no frames.

    This is a *baseline* only. Tests that need a populated config still override
    HOME themselves (test_setup.py's ``_run(home=...)``) or monkeypatch the
    module constant (test_config.py) — both take precedence over this fixture,
    so no test loses its intent.

    USERPROFILE is set alongside HOME because that is what ``Path.home()``
    consults on Windows.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in WATCH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return home

# 14 visually distinct fills → 14 abrupt cuts → x264 emits a keyframe per cut.
COLORS = [
    "red", "green", "blue", "white", "black", "yellow", "cyan",
    "magenta", "gray", "orange", "purple", "brown", "navy", "olive",
]


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{result.stderr}")


def build_cut_clip(
    path: Path,
    n: int = 14,
    seg: float = 0.4,
    size: str = "320x240",
    fps: int = 10,
) -> None:
    """Concatenate ``n`` solid-color segments into one clip with ``n`` cuts.

    Each color change is a hard scene cut, so the scene selector finds ~n-1
    changes. x264's own scenecut detection is unreliable on flat fills, so we
    force a keyframe at every ``seg`` boundary — giving ~n real keyframes for
    the keyframe engine to find.
    """
    inputs: list[str] = []
    for i in range(n):
        color = COLORS[i % len(COLORS)]
        inputs += ["-f", "lavfi", "-t", str(seg), "-i", f"color=c={color}:s={size}:r={fps}"]
    streams = "".join(f"[{i}:v]" for i in range(n))
    filt = f"{streams}concat=n={n}:v=1:a=0[out]"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", filt, "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-force_key_frames", f"expr:gte(t,n_forced*{seg})",
        str(path),
    ])


def build_static_clip(
    path: Path,
    duration: float = 3.0,
    size: str = "320x240",
    fps: int = 10,
) -> None:
    """One solid color: 1 keyframe, no scene changes → triggers both fallbacks."""
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-t", str(duration), "-i", f"color=c=blue:s={size}:r={fps}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "600",
        str(path),
    ])


@pytest.fixture(scope="session")
def cut_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "cuts.mp4"
    build_cut_clip(path)
    return path


@pytest.fixture(scope="session")
def static_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "static.mp4"
    build_static_clip(path)
    return path


# --- motion fixtures ----------------------------------------------------------
# These clips carry their own clock in the pixels: a white bar whose x position
# encodes the frame's timestamp. That lets a test assert a *label* against the
# *content*, which is the only way to catch an engine that reports a plausible
# time for the wrong frame.

MOTION_W, MOTION_H = 320, 240
_BAR_SLOPE, _BAR_ORIGIN = 102, 4          # x = 102*t + 4
_BAR_EXPR = (
    f"geq=lum='if(lt(abs(X-({_BAR_SLOPE}*T+{_BAR_ORIGIN}))\\,8)\\,255\\,0)':cb=128:cr=128"
)


def build_motion_clip(path: Path, duration: float = 3.0, fps: int = 60) -> None:
    """Constant-frame-rate clip whose pixels encode their own timestamp."""
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={MOTION_W}x{MOTION_H}:r={fps}:d={duration}",
        "-vf", _BAR_EXPR,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "12",
        str(path),
    ])


def build_vfr_clip(path: Path, duration: float = 3.0, fps: int = 60) -> None:
    """Screen-recording shape: short bursts separated by long held frames.

    Keeps the original presentation timestamps (no setpts), so the holds survive
    as real gaps — measured 40 gaps of 17ms and 7 of 317ms. This is the shape
    where fps resampling fails: it duplicates the held frame into every slot and
    labels each copy with a slot time rather than the pixels' time.
    """
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={MOTION_W}x{MOTION_H}:r={fps}:d={duration}",
        "-vf", f"{_BAR_EXPR},select='lt(mod(n\\,24)\\,6)'",
        "-fps_mode", "passthrough",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "12",
        str(path),
    ])


def bar_content_time(jpeg_path) -> float | None:
    """Recover the instant a frame's *pixels* depict, independent of any metadata."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(jpeg_path)],
        capture_output=True, text=True,
    )
    try:
        stream = json.loads(probe.stdout)["streams"][0]
        width, height = stream["width"], stream["height"]
    except (KeyError, IndexError, ValueError):
        return None
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(jpeg_path), "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    if len(raw) < width * height:
        return None
    row = raw[(height // 2) * width:(height // 2 + 1) * width]
    lit = [i for i, v in enumerate(row) if v > 128]
    if not lit:
        return None
    # Frames may have been scaled; map the centroid back to source pixels.
    x_source = (sum(lit) / len(lit)) * MOTION_W / width
    return (x_source - _BAR_ORIGIN) / _BAR_SLOPE


@pytest.fixture(scope="session")
def motion_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "motion_cfr.mp4"
    build_motion_clip(path)
    return path


@pytest.fixture(scope="session")
def vfr_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "motion_vfr.mp4"
    build_vfr_clip(path)
    return path


def build_slide_clip(path: Path, fps: int = 60) -> None:
    """A UI-shaped animation with a known envelope: a 120x60 box slides 400px
    from t=1.000 to t=1.300 — exactly 300 ms — then holds.

    geq rather than drawbox: drawbox evaluates its position expression once at
    init in this ffmpeg (no `eval` option), so the box snaps to its end position
    and the clip is static. Verified the hard way.
    """
    xp = r"if(lt(T\,1)\,40\,if(lt(T\,1.3)\,40+400*(T-1)/0.3\,440))"
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=white:s=640x360:r={fps}:d=2.5",
        "-vf",
        f"geq=lum='if(gte(X\\,{xp})*lte(X\\,({xp})+120)*gte(Y\\,150)*lte(Y\\,210)\\,60\\,255)'"
        ":cb=128:cr=128",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ])


@pytest.fixture(scope="session")
def slide_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "slide.mp4"
    build_slide_clip(path)
    return path
