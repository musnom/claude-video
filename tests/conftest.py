"""Shared pytest fixtures: environment isolation, ffmpeg-synthesized clips, and
scripts/ on sys.path."""
from __future__ import annotations

import json
import re
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


# --- eased-motion fixture -----------------------------------------------------
# build_slide_clip's envelope is strictly linear, so any tool that reports "the
# animation ran from A to B" scores the same on it whether or not it handles a
# decaying tail. An ease-out is the case that separates them: most of the travel
# lands in the first third, and the last third moves so little that a per-frame
# change threshold can call the animation over early. These clips are that case,
# with the curve written down in closed form so a test can check the tool's
# answer against arithmetic rather than against another measurement.

EASED_W, EASED_H = 640, 360
EASED_FPS = 60
EASED_DURATION = 2.5
EASED_START = 1.0                      # animation begins
EASED_SPAN = 0.5                       # ...and nominally ends 500 ms later
EASED_X0, EASED_TRAVEL = 100, 400      # box left edge travels 100 -> 500 px
EASED_BOX_W, EASED_BOX_H = 120, 60
EASED_BOX_Y = 150                      # box occupies rows 150..209
EASED_PROBE_Y = EASED_BOX_Y + EASED_BOX_H // 2   # 180: a row that is always inside
EASED_DARK, EASED_LIGHT = 60, 255      # same ink/paper as build_slide_clip
# The plan specified 200 -> 900 px of travel, which does not fit a 640-wide
# canvas. Travel was scaled to 100 -> 500 (400 px, matching build_slide_clip)
# rather than widening the canvas, so this clip stays comparable to its linear
# sibling. Right edge maxes at 620.


def eased_position(t: float, exponent: int) -> float:
    """The box's intended left edge at time ``t`` — the clip's spec, in Python.

    p(u) = 1 - (1-u)^n over u = (t-1.0)/0.5 clamped to [0,1]: an ease-out that
    starts at full speed and decays to zero. Tests assert against this rather
    than against a second measurement, so a builder that silently renders the
    wrong curve cannot agree with its own test.
    """
    u = min(max((t - EASED_START) / EASED_SPAN, 0.0), 1.0)
    return EASED_X0 + EASED_TRAVEL * (1.0 - (1.0 - u) ** exponent)


def _eased_x_expr(exponent: int) -> str:
    """The same curve as an ffmpeg eval expression, commas escaped for filter args.

    ``pow`` because ``^`` is not an operator in ffmpeg's expression language, and
    ``clip`` rather than nested ``if`` because it makes the hold-before / hold-after
    behaviour fall out of the clamp instead of needing three branches.
    """
    u = rf"clip((T-{EASED_START})/{EASED_SPAN}\,0\,1)"
    return rf"({EASED_X0}+{EASED_TRAVEL}*(1-pow(1-{u}\,{exponent})))"


def build_eased_clip(path: Path, exponent: int = 3, fps: int = EASED_FPS) -> None:
    """A 640x360 UI animation with a *non-linear* envelope: a 120x60 dark box
    ease-outs 400px (x=100 -> 500) between t=1.000 and t=1.500 — exactly 500 ms —
    holding at x=100 before and x=500 after.

    geq rather than drawbox for the reason build_slide_clip documents: drawbox
    evaluates its position expression once at init in this ffmpeg, so an animated
    drawbox renders a static clip. Verified here too — decoded every frame's probe
    row and confirmed the left edge moves 39px between frames 60 and 61 (n=3).

    The horizontal bound is ``lt(X, x+120)``, not build_slide_clip's ``lte``. With
    ``lte`` the box is 121px wide whenever x lands on an integer and 120px
    otherwise, so it gains a column at exactly t=1.500 where the clamp makes x
    exactly 500. That one column is 1/32 of a 16x16 thumbnail cell holding a
    195-level contrast, and it registers as a peak cell delta of 6.0 — landing
    precisely on ``motion_envelope``'s old default threshold, at precisely the
    nominal end time. Measured: with ``lte`` the frame at t=1.500 reports
    peak_delta 6 despite the box not having moved since t=1.433. That is a fake
    motion sample sitting exactly where this fixture is supposed to prove there is
    none, so the half-open bound is load-bearing, not a style choice. ``lt`` on Y
    likewise makes the box exactly 60 rows tall rather than 61.
    """
    xp = _eased_x_expr(exponent)
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"color=c=white:s={EASED_W}x{EASED_H}:r={fps}:d={EASED_DURATION}",
        "-vf",
        f"geq=lum='if(gte(X\\,{xp})*lt(X\\,({xp})+{EASED_BOX_W})"
        f"*gte(Y\\,{EASED_BOX_Y})*lt(Y\\,{EASED_BOX_Y + EASED_BOX_H})"
        f"\\,{EASED_DARK}\\,{EASED_LIGHT})'"
        ":cb=128:cr=128",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ])


def _gray_rows(path: Path, count: int | None = None) -> tuple[list[bytes], int]:
    """Decode ``path`` to one grayscale scanline per frame, plus the frame width.

    ``format=gray`` runs *before* ``crop`` on purpose: cropping a yuv420p frame to
    height 1 rounds the chroma planes to height 0 and ffmpeg rejects the filter
    outright ("non positive size ... height '0'"). Converting to a planar-gray
    frame first removes the subsampling constraint. Measured the hard way.

    The probe row is placed proportionally, so this reads a 512-wide extracted
    JPEG and the 640-wide source with the same code.
    """
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    try:
        stream = json.loads(probe.stdout)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, ValueError):
        return [], 0
    row = min(height - 1, max(0, round(EASED_PROBE_Y * height / EASED_H)))
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vf", f"format=gray,crop={width}:1:0:{row}", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    frames = [raw[i * width:(i + 1) * width] for i in range(len(raw) // width)]
    return (frames if count is None else frames[:count]), width


def _row_left_edge(row: bytes, width: int) -> float | None:
    """Leftmost dark pixel in a probe row, mapped back to SOURCE pixels.

    Threshold at the midpoint of the 60/255 ink-on-paper contrast: a downscaled
    frame turns the box's hard edge into a one-pixel ramp, and the midpoint is the
    only crossing that stays put as the scale factor changes.
    """
    midpoint = (EASED_DARK + EASED_LIGHT) // 2
    dark = [i for i, v in enumerate(row) if v < midpoint]
    if not dark:
        return None
    return dark[0] * EASED_W / width


def eased_box_left(frame_path) -> float | None:
    """Recover the box's left edge, in source pixels, from a single frame image.

    The still-image counterpart to ``eased_box_track``: point it at a JPEG the
    extractor wrote and it reports where the box *is*, independent of whatever
    timestamp the extractor attached to that file.
    """
    rows, width = _gray_rows(Path(frame_path), count=1)
    if not rows:
        return None
    return _row_left_edge(rows[0], width)


def eased_box_track(clip_path, fps: int = EASED_FPS) -> list[tuple[float, float | None]]:
    """``[(timestamp, left_edge_source_px)]`` for every frame of an eased clip.

    One decode pass over a single scanline (640 bytes/frame), so tracking the
    whole 150-frame clip costs about as much as seeking to one frame — and unlike
    ``-ss``, frame N's time is N/fps by construction with no seek rounding to
    reason about. The clips are CFR by construction (lavfi ``color`` at a fixed
    rate, no select filter), so the index-to-time mapping is exact.
    """
    rows, width = _gray_rows(Path(clip_path))
    return [(i / fps, _row_left_edge(row, width)) for i, row in enumerate(rows)]


@pytest.fixture(scope="session")
def eased_clip_cubic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "eased_cubic.mp4"
    build_eased_clip(path, exponent=3)
    return path


@pytest.fixture(scope="session")
def eased_clip_quintic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "eased_quintic.mp4"
    build_eased_clip(path, exponent=5)
    return path


# --- graphic-cuts fixture -----------------------------------------------------
# A motion-graphics piece whose cuts are *layout* changes. Real infographic and
# explainer edits swap bars, blocks and captions inside a frame that otherwise
# holds still, so ffmpeg's scene metric — which is nothing more than the mean
# absolute luma difference between consecutive frames — barely moves. This clip
# pins the consequence: at a SCENE_THRESHOLD of 0.20 the whole 8-shot sequence
# reads as a single shot.
#
# The metric was calibrated against this ffmpeg (8.1.2) rather than assumed. A
# probe clip that flips boxes of known area between two known luma values gives
#
#     scene_score == (changed_pixels * dY) / (width * height) / 100
#
# on the *luma plane only* — chroma is not counted, and the divisor is 100, not
# 255. Four probe boxes (20000, 57600, 129600 and 217600 px, all at dY=95)
# matched that to within 0.03%. So a cut's score is a straight
# area-times-contrast budget and the graphics below are sized against it: every
# cut is aimed at ~0.070, leaving 40% of headroom over the 0.05 floor this
# fixture must clear and 30% under the 0.10 ceiling it must not.

GRAPHIC_W, GRAPHIC_H = 1280, 720
GRAPHIC_FPS = 30
GRAPHIC_SHOT_SECONDS = 2.0
GRAPHIC_CUT_TIMES = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0)

# Punched shot is rendered at 1344x756 and centre-cropped back to 1280x720.
# 1344 = 1280 * 1.05 and 756 = 720 * 1.05, both even, so the crop offsets are
# whole pixels and the zoom stays exactly on the 16:9 aspect. 1.05 and not more:
# a punch-in is a whole-frame transform and therefore the most expensive cut in
# the clip by changed area (9.90% against 7.60% for a bar swap), so roughly 1.09x
# would cross the 0.10 ceiling this fixture has to stay under.
GRAPHIC_PUNCH_W, GRAPHIC_PUNCH_H = 1344, 756
GRAPHIC_PUNCH_SCALE = GRAPHIC_PUNCH_W / GRAPHIC_W

# Grayscale on purpose, and load-bearing: chroma is not counted by the metric, so
# recolouring these to brand colours at similar luma would drop every cut's score
# toward zero while looking like a cosmetic change in review.
_G_BG = "0x202020"       # decoded Y = 43; every shot shares it
_G_BAR = "0x8c8c8c"      # decoded Y = 136, so a bar-vs-background dY of 93
_G_BAND = "0x505050"
_G_RULE = "0x3c3c3c"
_G_INK = "0x646464"
_G_ACCENT = "0xc8c8c8"

_G_BAR_X = (340, 520, 700, 880, 1060)
_G_BAR_W = 120
_G_BAR_BASE = 596        # bars grow upward from the axis line

# Consecutive rows differ by exactly 580px of summed bar height. That number is
# the whole design: 120px-wide bars at dY=93 mean a cut repaints
# 120 * 580 = 69600 px, for 69600 * 93 / (1280*720) / 100 = 0.0702; the legend
# cursor adds 0.0007 and the measurement comes in at 0.0709. Every row stays
# inside [180, 410] so no bar clips the axis or the title band, and each
# individual bar moves 60-160px, so a human sees an obvious cut where the metric
# sees almost nothing. That gap is the point of the fixture.
_G_SHOT_HEIGHTS = (
    (200, 340, 180, 300, 240),
    (350, 230, 310, 210, 340),
    (230, 370, 210, 360, 270),
    (320, 220, 350, 260, 370),
    (180, 340, 260, 390, 270),
    (310, 240, 410, 310, 390),
    (200, 390, 280, 400, 290),
)


def _g_box(x: int, y: int, w: int, h: int, color: str) -> str:
    return f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill"


def _graphic_shot_chain(shot: int) -> str:
    """Filter chain for one 2-second shot: fixed chrome plus that shot's bars.

    drawbox is safe here where build_slide_clip had to fall back to geq: the
    problem there was that drawbox evaluates its position expression once at
    init, and nothing in a shot moves *within* the shot. Each shot is a still.
    """
    heights = _G_SHOT_HEIGHTS[shot]
    boxes = [
        _g_box(64, 48, 720, 56, _G_BAND),      # title band
        _g_box(64, 48, 14, 56, _G_ACCENT),     # title accent
        _g_box(64, 116, 420, 6, _G_RULE),      # subtitle rule
        _g_box(320, _G_BAR_BASE, 880, 5, _G_INK),   # axis
        _g_box(64, 660, 1152, 4, "0x2c2c2c"),  # footer rule
    ]
    for row in range(3):                        # legend: swatch + label bar
        y = 200 + row * 52
        boxes.append(_g_box(64, y, 28, 28, _G_BAR if row == 0 else _G_INK))
        boxes.append(_g_box(104, y + 9, 140, 10, _G_RULE))
    # Legend cursor walks down a row per shot. At 8x28 px it contributes ~0.0007
    # to a cut's score, i.e. nothing — it is there so the legend visibly tracks
    # the shot, not to carry any of the budget.
    boxes.append(_g_box(48, 200 + (shot % 3) * 52, 8, 28, _G_ACCENT))
    for x, h in zip(_G_BAR_X, heights):
        boxes.append(_g_box(x, _G_BAR_BASE - h, _G_BAR_W, h, _G_BAR))
    return ",".join(boxes)


def build_graphic_cuts_clip(path: Path) -> None:
    """8 infographic shots, 7 cuts at t=2,4,...,14, none of which ffmpeg calls a
    scene change at the old production threshold.

    Eight concatenated stills rather than one timeline gated by
    ``enable='between(t,a,b)'``: the last shot is a punch-in, and scale has no
    timeline support at all — ``scale=1344:756:enable='gt(t,14)'`` is rejected
    outright with "Timeline ('enable' option) not supported with filter
    'scale'". Giving each shot its own chain makes the punch a two-filter suffix
    on shot 6's own chain, which is also what makes it provably the *same
    content*: byte-identical drawbox calls, then scale+crop.

    Cuts land on exact frame boundaries because 2.0s at 30fps is 60 whole
    frames, so the scene-change frames are the ones at t=2.000, 4.000, ...
    """
    n_shots = len(_G_SHOT_HEIGHTS) + 1          # 7 rendered rows + 1 punch-in
    inputs: list[str] = []
    chains: list[str] = []
    for i in range(n_shots):
        inputs += ["-f", "lavfi", "-t", str(GRAPHIC_SHOT_SECONDS),
                   "-i", f"color=c={_G_BG}:s={GRAPHIC_W}x{GRAPHIC_H}:r={GRAPHIC_FPS}"]
        chain = _graphic_shot_chain(min(i, len(_G_SHOT_HEIGHTS) - 1))
        if i == n_shots - 1:
            ox = (GRAPHIC_PUNCH_W - GRAPHIC_W) // 2
            oy = (GRAPHIC_PUNCH_H - GRAPHIC_H) // 2
            chain += (f",scale={GRAPHIC_PUNCH_W}:{GRAPHIC_PUNCH_H}"
                      f",crop={GRAPHIC_W}:{GRAPHIC_H}:{ox}:{oy}")
        chains.append(f"[{i}:v]{chain}[s{i}]")
    concat = "".join(f"[s{i}]" for i in range(n_shots)) + f"concat=n={n_shots}:v=1:a=0[out]"
    # crf 18 rather than the default 23 to keep the within-shot noise floor down:
    # measured max non-cut score 0.000037, three orders of magnitude below the
    # 0.05 assertion, so no encoder ripple can fake an eighth "cut".
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", ";".join(chains + [concat]), "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18",
        str(path),
    ])


def _scene_probe(path: Path, expr: str) -> list[tuple[float, float]]:
    """[(pts_time, scene_score)] for the frames ``select`` passes under ``expr``.

    ``metadata=print`` can only report frames that survive ``select``, and
    ``select`` only computes ``scene`` when its expression mentions it — so the
    score has to be read out of the same pass that filters on it. It writes to
    stdout, two lines per surviving frame:

        frame:0    pts:30720   pts_time:2
        lavfi.scene_score=0.070944
    """
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"select='{expr}',metadata=print:file=-", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    out: list[tuple[float, float]] = []
    pending: float | None = None
    for line in result.stdout.splitlines():
        if line.startswith("frame:"):
            pending = float(line.rsplit("pts_time:", 1)[1])
        elif line.startswith("lavfi.scene_score=") and pending is not None:
            out.append((pending, float(line.split("=", 1)[1])))
            pending = None
    return out


def scene_change_times(path: Path, threshold: float) -> list[float]:
    """Times ffmpeg calls a scene change above ``threshold``.

    Deliberately *without* the ``eq(n\\,0)`` term extract_scene_candidates adds:
    that term makes frame 0 a guaranteed hit, which would turn "no cuts found"
    into a count of 1 and hide exactly what these tests measure.
    """
    return [t for t, _ in _scene_probe(path, f"gt(scene\\,{threshold})")]


def scene_scores(path: Path) -> list[tuple[float, float]]:
    """Every frame's scene score. ``gte(scene,0)`` is always true, so select
    passes the whole stream while still computing the metric."""
    return _scene_probe(path, "gte(scene\\,0)")


def graphic_luma_frame(path: Path, t: float) -> bytes:
    """The single frame at ``t`` as one 8-bit luma byte per pixel."""
    return subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", f"{t:.3f}", "-i", str(path),
         "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout


def graphic_bar_edges(path: Path, t: float, row: int = 580, threshold: int = 100) -> list[int]:
    """x of every dark→bright and bright→dark transition along one scanline.

    Row 580 sits 16px above the axis, below the shortest bar in any shot, so all
    five bars cross it and the returned list is 10 edges: left,right per bar.
    """
    frame = graphic_luma_frame(path, t)
    scan = frame[row * GRAPHIC_W:(row + 1) * GRAPHIC_W]
    edges: list[int] = []
    lit = False
    for x, value in enumerate(scan):
        if (value > threshold) != lit:
            edges.append(x)
            lit = not lit
    if lit:
        edges.append(GRAPHIC_W)
    return edges


@pytest.fixture(scope="session")
def graphic_cuts_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "graphic_cuts.mp4"
    build_graphic_cuts_clip(path)
    return path


# --- sparse-cuts fixture ------------------------------------------------------
# The shape the gap-fill phase exists for: a long take with a handful of cuts,
# all of them bunched at the front. The scene engine returns one frame per shot
# and stops, so a 12-minute video comes back as 12 frames against a cap of 100 —
# measured 12/12 candidates, one frame per 60 s, with 88 frames of budget left
# unspent. Everything after 470 s is a single 250-second shot the engine
# represents with one frame.
#
# (color, seconds) pairs. Durations are front-loaded on purpose — four 5s shots,
# then a roughly doubling tail — and sum to exactly 720 s.
#
# The colour *order* is not decorative. ffmpeg's `scene` score is a normalized
# SAD of the luma plane alone, so a cut between two colours of similar
# brightness scores near zero no matter how different they look. The first pass
# of this fixture ordered the palette by hue and produced two cuts at 0.25 and
# 0.20 against a 0.20 threshold — magenta→gray (luma 105→128) was passing by
# rounding. These twelve are interleaved low-luma/high-luma instead (black 0,
# olive 113, navy 15, gray 128, …), which puts every consecutive pair at least
# 98 luma apart and lifts the weakest cut to a measured 0.84. Reorder them and
# re-measure, or the fixture quietly stops having 11 cuts.
SPARSE_SHOTS: tuple[tuple[str, int], ...] = (
    ("black", 5), ("olive", 5), ("navy", 5), ("gray", 5),
    ("blue", 10), ("orange", 10), ("green", 20), ("cyan", 30),
    ("red", 60), ("yellow", 120), ("magenta", 200), ("white", 250),
)

# ffmpeg's own definitions for the names above. Solid fills survive the
# yuv420p round trip almost exactly: measured worst-case error of 1/255 on any
# channel across all 12 shots, so a tolerance of 4 is generous and still tight
# enough to tell any two of these colours apart.
SPARSE_RGB: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0), "cyan": (0, 255, 255), "yellow": (255, 255, 0),
    "blue": (0, 0, 255), "white": (255, 255, 255), "black": (0, 0, 0),
    "green": (0, 128, 0), "magenta": (255, 0, 255), "gray": (128, 128, 128),
    "orange": (255, 165, 0), "navy": (0, 0, 128), "olive": (128, 128, 0),
}


def sparse_shot_bounds() -> list[tuple[float, float, str]]:
    """(start, end, color) per shot — the intended ground truth, derived rather
    than duplicated so the durations above stay the single source of truth."""
    bounds, t = [], 0.0
    for color, dur in SPARSE_SHOTS:
        bounds.append((t, t + dur, color))
        t += dur
    return bounds


def build_sparse_cuts_clip(path: Path, size: str = "320x240", fps: int = 10) -> None:
    """A 720 s clip of 12 solid-colour shots with unevenly spread cuts.

    Same concat-of-lavfi-colors construction as :func:`build_cut_clip`, but 7200
    frames rather than 56, so the encode settings are worth stating: ``-preset
    ultrafast -crf 30 -g 300`` builds the whole 12 minutes in 0.35 s and 117 KB,
    which is why the longest clip in the suite is not the slowest fixture.

    Keyframes are forced at every shot boundary. Without them a seek into the
    250-second tail shot decodes from whatever keyframe x264 last chose, and the
    per-shot colour assertions each pay for it; with them, 36 seeks across the
    whole clip cost 0.76 s total.

    Scene detection sees all 11 cuts at threshold 0.20 — verified by decoding the
    clip and reading ``lavfi.scene_score`` (weakest cut 0.84), not assumed.
    """
    inputs: list[str] = []
    for color, dur in SPARSE_SHOTS:
        inputs += ["-f", "lavfi", "-t", str(dur), "-i", f"color=c={color}:s={size}:r={fps}"]
    n = len(SPARSE_SHOTS)
    streams = "".join(f"[{i}:v]" for i in range(n))
    filt = f"{streams}concat=n={n}:v=1:a=0[out]"
    cuts = ",".join(f"{start:g}" for start, _, _ in sparse_shot_bounds()[1:])
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", filt, "-map", "[out]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-pix_fmt", "yuv420p", "-g", "300",
        "-force_key_frames", cuts,
        str(path),
    ])


_SCENE_TS_RE = re.compile(r"pts_time:([0-9.]+)")


def scene_cut_times(path: Path, threshold: float = 0.20) -> list[float]:
    """Times ffmpeg itself calls scene changes, straight from the filter graph.

    ``metadata=print`` writes the surviving frames' pts to stdout, which is the
    same signal ``frames.extract_scene_candidates`` selects on — so a test built
    on this asserts against ffmpeg's verdict rather than against our parsing of
    showinfo. The first frame is *not* included (no ``eq(n,0)`` term), so this
    counts cuts, not shots.
    """
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"select='gt(scene\\,{threshold})',metadata=print:file=-",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return [round(float(m), 3) for m in _SCENE_TS_RE.findall(proc.stdout)]


def frame_rgb_at(path: Path, t: float) -> tuple[int, int, int]:
    """The colour of the frame at ``t``, read out of the pixels.

    ``scale=1:1`` area-averages the whole frame into one pixel, which is exact
    for a solid fill and immune to any single-pixel artefact — and meaningless on
    anything that is not a flat fill, so do not reuse it on the motion fixtures.
    ffmpeg's accurate seek lands on the frame nearest ``t``: measured, ``-ss
    4.95`` on a 10 fps clip returns frame 50 (t=5.0), so boundary probes are
    frame-exact to 0.1 s.
    """
    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-ss", f"{t:.3f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=1:1", "-pix_fmt", "rgb24",
         "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    if len(raw) < 3:
        raise AssertionError(f"no frame decoded at t={t}")
    return raw[0], raw[1], raw[2]


@pytest.fixture(scope="session")
def sparse_cuts_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "sparse_cuts.mp4"
    build_sparse_cuts_clip(path)
    return path


# --- low-contrast fade fixtures -----------------------------------------------
# A light-mode card fading in on white, at four contrasts. The point of the clip
# is that a real, plainly visible 400ms animation produces almost no per-frame
# change: at contrast 23 the card darkens 23 luma levels spread over 24 frames,
# so the largest single-cell change the frame engine sees is 2/255 — under any
# absolute floor worth having. Contrast is the parameter rather than a constant
# because it is the only thing that moves that number, and where it crosses the
# floor is a decision the suite should be able to show rather than assert.

LOWCONTRAST_SIZE = (1280, 720)
LOWCONTRAST_FPS = 60
LOWCONTRAST_DURATION = 2.0
LOWCONTRAST_CARD = (440, 300)
LOWCONTRAST_CARD_XY = (420, 210)          # centred: (1280-440)/2, (720-300)/2
LOWCONTRAST_FADE_START = 1.0
LOWCONTRAST_FADE_END = 1.4
# The clip is CFR 60, so t = frame / 60 exactly and these indices are the fade
# window: f60 (t=1.000) is the last fully-white frame, f84 (t=1.400) the first
# fully-opaque one. Verified frame by frame, not derived.
LOWCONTRAST_FADE_FRAMES = (60, 84)
LOWCONTRAST_CONTRASTS = (23, 55, 110, 222)

# Where the card's luma actually settles once the fade completes, read off the
# decoded pixels. Three of the four are exactly ``255 - contrast``; contrast 110
# lands on 146 rather than 145 because yuv420p carries luma in the limited 16-235
# range and only 220 of the 256 full-range gray levels survive the round trip.
# 145 is one of the 36 that do not — measured by encoding a 0-255 gray ramp
# losslessly and decoding it back, where 145 -> 146 while 232, 200 and 33 map to
# themselves. Forcing ``-color_range pc`` does make all 256 exact, but it would
# make these the only full-range clips in the suite and would silently change the
# very change-signal numbers the fixture exists to report if any consumer read
# the tag as limited. Keeping the nominal key and recording the one-level landing
# here is the cheaper honesty.
LOWCONTRAST_PLATEAU = {23: 232, 55: 200, 110: 146, 222: 33}


def build_lowcontrast_clip(path: Path, contrast: int, duration: float = LOWCONTRAST_DURATION) -> None:
    """A 440x300 neutral-gray card alpha-fading in on white over t=1.0 -> t=1.4.

    ``contrast`` is the luma drop the finished card makes against the white
    background, so the fill is the neutral gray ``255 - contrast``. Neutral rather
    than the tinted grays a real light-mode UI ships (#E8E8ED, #C8C8CD, #909095,
    #202025 — the shades this is modelled on): for R=G=B the BT.601 luma *is* the
    hex value, so "contrast 23" is a property of the pixels instead of a number
    you have to recompute, and chroma stays pinned at 128 where 4:2:0 subsampling
    cannot leak into a luma probe. The tinted originals differ from these neutrals
    by at most 0.6 luma, so nothing is lost.

    Built from a lavfi color source through ``fade=alpha=1`` and composited with
    ``overlay`` — not the geq that build_slide_clip needs. Both halves of that are
    measured:

    * ``fade`` does animate. Its factor is ``(n - start_frame) / nb_frames`` per
      output frame, which lands frame-exact at 60fps: contrast 23 reads 255 at f60
      (t=1.000), 243 at f72 (t=1.200) and 232 at f84 (t=1.400). The warning on
      build_slide_clip is about drawbox evaluating its position once at init; fade
      re-evaluates per frame and was checked by decoding all 120 frames.
    * geq is the *wrong* filter here, not just a slower one. geq writes the luma
      plane directly, so its numbers are limited-range Y codes: a 255->232 ramp
      written that way decodes to 255->252 with the top two thirds clipped flat —
      measured, the card sat at 255 until f83 and the animation was invisible.
      Compositing in RGB and letting swscale do the range conversion is what keeps
      the ramp intact. geq also cost 1.10s per clip against 0.15s for this.

    Left at the encoder's default CRF like every other builder here. Lossless was
    measured and buys nothing that matters: the ramp is bit-identical (the residual
    ramp error below is not codec loss), and the only difference is deblocking
    ripple in the outermost ~12 pixels of the card edge, which is why the probes
    read the card's interior.

    Deliberately short and still at both ends (1.0s white, 400ms fade, 0.6s held):
    1280x720 is the largest frame in the suite and there are four of these.
    """
    width, height = LOWCONTRAST_SIZE
    card_w, card_h = LOWCONTRAST_CARD
    card_x, card_y = LOWCONTRAST_CARD_XY
    gray = 255 - contrast
    fill = f"0x{gray:02X}{gray:02X}{gray:02X}"
    fade_seconds = LOWCONTRAST_FADE_END - LOWCONTRAST_FADE_START
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i",
        f"color=c=white:s={width}x{height}:r={LOWCONTRAST_FPS}:d={duration}",
        "-f", "lavfi", "-i",
        f"color=c={fill}:s={card_w}x{card_h}:r={LOWCONTRAST_FPS}:d={duration}",
        "-filter_complex",
        f"[1:v]format=yuva420p,"
        f"fade=t=in:st={LOWCONTRAST_FADE_START}:d={fade_seconds}:alpha=1[card];"
        f"[0:v][card]overlay=x={card_x}:y={card_y}[out]",
        "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ])


def patch_luma_series(
    path: Path, width: int, height: int, x: int, y: int
) -> list[tuple[int, int, float]]:
    """``(min, max, mean)`` luma of one rectangle, for every frame of ``path``.

    Crops before converting to gray, so the cost is the rectangle rather than the
    1280x720 frame — a 32x32 patch across 120 frames is 120KB, which is what makes
    this cheap enough to run inside a test. ``min``/``max`` come back beside the
    mean because uniformity is half the claim: a mean of 243 would equally fit a
    half-drawn card.

    ``height`` must be even; the source is 4:2:0 and ffmpeg rejects an odd crop.
    """
    result = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(path),
         "-vf", f"crop={width}:{height}:{x}:{y},format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    raw = result.stdout
    stride = width * height
    return [
        (min(cell), max(cell), sum(cell) / stride)
        for cell in (raw[i * stride:(i + 1) * stride] for i in range(len(raw) // stride))
    ]


def card_luma_series(path: Path, patch: int = 32) -> list[float]:
    """Mean luma of a ``patch``-square sample at the centre of the card, per frame.

    Samples the interior rather than the whole card on purpose: x264's in-loop
    deblocking filter smears the card's hard edge into the white background, and
    at contrast 222 the outermost pixels swing 23-52 against a true fill of 33.
    Sixteen pixels in it is uniform at every contrast (measured), and the frame
    centre is 220x150 clear of the nearest edge.
    """
    width, height = LOWCONTRAST_SIZE
    return [mean for _, _, mean in patch_luma_series(
        path, patch, patch, (width - patch) // 2, (height - patch) // 2)]


def card_bbox(path: Path, frame_index: int) -> tuple[int, int, int, int] | None:
    """Recover the card's ``(x, y, w, h)`` from the pixels of one frame.

    Reads the horizontal and vertical profiles through the frame centre and takes
    the extent of the run darker than halfway between white and the frame's own
    darkest pixel. Self-calibrating on that darkest pixel is what lets one helper
    serve every contrast *and* every point in the fade — at f72 the card is only
    half drawn, and a fixed cutoff tuned for the plateau would miss it.

    Two profiles rather than a full raster scan because the card is a rectangle
    containing the frame centre, which makes the centre row and column exact and
    turns a 921600-pixel loop into two slices. Returns ``None`` when no pixel is
    more than a couple of levels off white, i.e. nothing has been drawn yet.
    """
    width, height = LOWCONTRAST_SIZE
    result = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", str(path),
         "-vf", f"select='eq(n\\,{frame_index})',format=gray",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True,
    )
    raw = result.stdout[:width * height]
    if len(raw) < width * height:
        return None
    darkest = min(raw)
    if darkest >= 253:
        return None
    cutoff = (255 + darkest) / 2
    row = raw[(height // 2) * width:(height // 2 + 1) * width]
    column = raw[width // 2::width]

    def extent(profile: bytes) -> tuple[int, int]:
        dark = [i for i, v in enumerate(profile) if v < cutoff]
        return dark[0], dark[-1] - dark[0] + 1

    x, w = extent(row)
    y, h = extent(column)
    return x, y, w, h


def thumb_deltas(path: Path, work_dir: Path) -> list[tuple[float, float]]:
    """Per-frame ``(mean, peak)`` cell change, through the real extraction chain.

    Reproduces what the frame engine actually sees instead of sampling the source
    directly: every frame is written as a 512-wide JPEG at ``-q:v 4`` and only then
    reduced to a 16x16 gray thumbnail, so JPEG loss is inside the number rather
    than assumed away. ``mean`` averages the 256 cell differences and ``peak`` is
    the largest single one; they differ by roughly 7x here because the card covers
    14% of the frame, which is exactly the case where a whole-frame mean says
    nothing moved.

    Index 0 is ``(0.0, 0.0)`` — no previous frame to differ from.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    for stale in work_dir.glob("frame_*.jpg"):
        stale.unlink()
    pattern = str(work_dir / "frame_%04d.jpg")
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
        "-vf", "scale=w='min(512,iw)':h='min(1998,ih)':"
               "force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-q:v", "4", pattern,
    ])
    count = len(list(work_dir.glob("frame_*.jpg")))
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-start_number", "1",
         "-i", pattern, "-vf", "scale=16:16,format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    )
    raw, cells = result.stdout, 16 * 16
    if len(raw) != cells * count:
        raise RuntimeError(f"thumbnail decode mismatch: {len(raw)} bytes for {count} frames")
    thumbs = [raw[i * cells:(i + 1) * cells] for i in range(count)]
    deltas: list[tuple[float, float]] = [(0.0, 0.0)]
    for previous, current in zip(thumbs, thumbs[1:]):
        diffs = [abs(a - b) for a, b in zip(current, previous)]
        deltas.append((sum(diffs) / cells, float(max(diffs))))
    return deltas


@pytest.fixture(scope="session")
def lowcontrast_clips(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    """The same fade at four contrasts, keyed by the integer contrast. ~0.7s total."""
    clip_dir = tmp_path_factory.mktemp("clips")
    clips: dict[int, Path] = {}
    for contrast in LOWCONTRAST_CONTRASTS:
        path = clip_dir / f"lowcontrast_{contrast}.mp4"
        build_lowcontrast_clip(path, contrast)
        clips[contrast] = path
    return clips


# --- slide-deck fixture -------------------------------------------------------
# The dedup case the module docstring in frames.py used to promise and did not
# deliver: "a slide-gaining-a-bullet survives". Each state adds one text-sized
# line to a 1080p slide, which is a large, obvious change to a reader and a tiny
# one to a whole-frame average.
#
# The bar height is calibrated, not chosen. A 620x22 bar covers 0.66% of a
# 1920x1080 frame at a contrast of ~210, so the mean per-pixel delta between
# consecutive states measures 1.42 / 1.57 / 1.68 / 1.77 — reproducing the audit's
# 1.5-1.8 band, and every one of them under DEDUP_THRESHOLD's 2.0. The same
# frames' peak cell reads 55-70. Taller bars break the fixture by making it pass:
# at 620x34 the means are 2.2-2.6 and the old mean-only rule keeps every state.
SLIDE_DECK_W, SLIDE_DECK_H = 1920, 1080
SLIDE_DECK_STATES = 5
SLIDE_DECK_SECONDS = 2.0
SLIDE_DECK_BAR = (620, 22)
SLIDE_DECK_BG = "0xF2F2F2"
SLIDE_DECK_INK = "0x202020"


def slide_deck_state_times() -> list[float]:
    """The midpoint of each build state — where a frame shows that state cleanly."""
    return [s * SLIDE_DECK_SECONDS + SLIDE_DECK_SECONDS / 2 for s in range(SLIDE_DECK_STATES)]


def build_slide_deck_clip(path: Path, fps: int = 10) -> None:
    """A 5-state slide build: state N shows N+1 lines of body text, held 2s each.

    drawbox with ``enable='between(t,a,b)'`` rather than geq, for the same reason
    the graphic-cuts fixture uses it: nothing moves *within* a state, so the
    position expression being evaluated once at init costs nothing here.
    """
    bar_w, bar_h = SLIDE_DECK_BAR
    boxes = []
    for state in range(SLIDE_DECK_STATES):
        for line in range(state + 1):
            y = 300 + line * 60
            boxes.append(
                f"drawbox=x=200:y={y}:w={bar_w}:h={bar_h}:color={SLIDE_DECK_INK}:t=fill:"
                f"enable='between(t,{state * SLIDE_DECK_SECONDS},"
                f"{(state + 1) * SLIDE_DECK_SECONDS})'"
            )
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-t", str(SLIDE_DECK_STATES * SLIDE_DECK_SECONDS),
        "-i", f"color=c={SLIDE_DECK_BG}:s={SLIDE_DECK_W}x{SLIDE_DECK_H}:r={fps}",
        "-vf", ",".join(boxes),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "20",
        str(path),
    ])


@pytest.fixture(scope="session")
def slide_deck_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "slide_deck.mp4"
    build_slide_deck_clip(path)
    return path


# --- dissolve fixture ---------------------------------------------------------
# One clip carrying both kinds of transition, so a single test can watch a
# detector treat them inconsistently. A hard cut repaints the frame in one step;
# a cross-dissolve spreads the identical amount of repainting over 30 steps. Any
# rule of the form "a frame is a transition when it differs from its predecessor
# by more than X" sees the first and is blind to the second, and this clip pins
# the numbers that make that concrete rather than arguable:
#
#     hard cut        peak cell delta 205.0     over 1 frame
#     cross-dissolve  peak cell delta 2.0-4.0   over 30 frames, 102 in total
#
# So the dissolve moves *half* as much luma as the cut in total, and every one of
# its steps sits at 33-67% of a 6.0 per-frame floor. Nothing is near the boundary
# in either direction: the cut clears 6.0 by 34x, the dissolve's largest step
# misses it by 1.5x. The exact step histogram on the gray scale is 14 steps of
# 4.0, 14 of 3.0 and 2 of 2.0 (at t=3.500 and t=4.000), summing to 102 — the
# alternation is integer rounding of a 102/30 = 3.4 ramp, not a defect. On the
# coded plane the same ramp is a flatter 28 steps of 3 and 2 of 2, summing to 88;
# the two scales are related by the limited->full range expansion below.
#
# Grayscale fills, and load-bearing for the same reason the graphic-cuts fixture
# is grayscale: every signal here (ffmpeg's scene metric, the 16x16 dedup thumb,
# the JPEG the extractor writes) reduces to luma, so recolouring these to three
# hues at similar brightness would collapse the whole clip to a single shot while
# looking like a cosmetic edit in review.
DISSOLVE_W, DISSOLVE_H = 640, 360
DISSOLVE_FPS = 30
DISSOLVE_DURATION = 6.0
DISSOLVE_CUT_T = 1.5                      # hard cut: scene A -> scene B
DISSOLVE_FADE_START = 3.0                 # cross-dissolve: scene B -> scene C
DISSOLVE_FADE_END = 4.0
DISSOLVE_FADE_SPAN = DISSOLVE_FADE_END - DISSOLVE_FADE_START

# Segment lengths feeding the filter graph. 1.5 + 2.5 = 4.0s of [ab], of which
# xfade consumes offset+duration = 4.0 exactly, then 2.0s of C survives: 6.0s
# total, and every boundary is a whole frame at 30fps (45, 90 and 120).
_D_SEG_A, _D_SEG_B, _D_SEG_C = 1.5, 2.5, 3.0

_D_FILL_A = "0x202020"
_D_FILL_B = "0xececec"
_D_FILL_C = "0x868686"

# What those fills decode to, measured, on the two scales this repo reads pixels
# on. They differ because `format=gray` expands limited-range video luma back to
# full range ((Y-16)*255/219), which lands within a unit of the source hex value.
# Both are written down because the assertions below use the gray scale while
# ffmpeg's scene metric works on the coded plane, and mixing them silently turns
# a 176 into a 205.
DISSOLVE_GRAY_A, DISSOLVE_GRAY_B, DISSOLVE_GRAY_C = 31, 236, 134   # via format=gray
DISSOLVE_CODED_A, DISSOLVE_CODED_B, DISSOLVE_CODED_C = 43, 219, 131  # coded Y plane

# Separations on the gray scale: A-B 205, B-C 102, A-C 103. All three scenes are
# unambiguously distinct, and C sits near the midpoint of A and B so no pair of
# scenes can be confused by a threshold that separates another pair.

DISSOLVE_THUMB = 16          # frames.DEDUP_THUMB
DISSOLVE_READ_WIDTH = 512    # the extractor's default JPEG width


def build_dissolve_clip(path: Path) -> None:
    """640x360 / 30fps / 6.0s carrying a hard cut AND a cross-dissolve.

    Three solid grayscale scenes:

        0.000 - 1.500   A  0x202020, gray 31     |
        1.500 - 3.000   B  0xececec, gray 236    | hard cut at t=1.500
        3.000 - 4.000   A 1.000s fade from B to C
        4.000 - 6.000   C  0x868686, gray 134

    The ``settb`` calls are the whole reason this is not a two-filter graph.
    concat rewrites its output timebase to 1/1000000 while an untouched color
    input keeps 1/30, and xfade refuses to join two timebases outright:

        [Parsed_xfade_1] First input link main timebase (1/1000000) do not
        match the corresponding second input link xfade timebase (1/30)

    That is a hard build failure, not a silent degradation, so it cannot reach a
    test. Measured which side actually needs normalising: ``settb`` on the
    *concat* branch alone is sufficient and produces a byte-identical file to
    normalising both. The redundant ``settb`` on [2:v] is kept anyway — it costs
    nothing, and it states the invariant ("both xfade inputs are at 1/30")
    rather than relying on a color source's default staying 1/30 forever.

    Encoding is, measured, NOT load-bearing here — contrary to what you would
    expect from a fixture whose entire signal is a 3-unit-per-frame luma ramp.
    qp 0, crf 10, 18, 23 and 40 all produce **byte-identical** 16x16 thumbnails:
    same 31/236/134 levels, same 205-unit cut, same 2-4 unit fade steps, and
    intra-frame spread 0 on all 180 frames. Three flat fills give x264 nothing
    to ring on, so DC prediction reproduces them exactly at any quantiser. crf
    10 is kept as cheap insurance (7.9 KB, 0.13s) for anyone who later adds
    texture or a gradient to a scene — at which point this paragraph stops being
    true and the still-section test is what will say so.
    """
    inputs: list[str] = []
    for seconds, fill in ((_D_SEG_A, _D_FILL_A), (_D_SEG_B, _D_FILL_B), (_D_SEG_C, _D_FILL_C)):
        inputs += ["-f", "lavfi", "-t", str(seconds),
                   "-i", f"color=c={fill}:s={DISSOLVE_W}x{DISSOLVE_H}:r={DISSOLVE_FPS}"]
    filt = (
        f"[0:v][1:v]concat=n=2:v=1:a=0,settb=1/{DISSOLVE_FPS}[ab];"
        f"[2:v]settb=1/{DISSOLVE_FPS}[c];"
        f"[ab][c]xfade=transition=fade:duration={DISSOLVE_FADE_SPAN:g}"
        f":offset={DISSOLVE_FADE_START:g}[out]"
    )
    _run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        *inputs,
        "-filter_complex", filt, "-map", "[out]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "10",
        str(path),
    ])


def dissolve_thumbs(clip_path, size: int = DISSOLVE_THUMB) -> list[bytes]:
    """Every frame as one ``size``-square grayscale thumbnail, in order.

    Reproduces the extractor's own change signal: scale to the read width, then
    to a DEDUP_THUMB square, then to gray — the exact chain
    ``extract_*`` + ``_thumb_frames`` put a frame through. The one step skipped
    is the JPEG on disk, and that is skipped because it was measured to be a
    no-op: writing all 180 frames as ``-q:v 4`` JPEGs at 512px and thumbnailing
    those gives thumbnails **byte-identical** to this decode. Flat fills survive
    JPEG exactly, so paying for a temp directory would buy nothing.

    One decode pass for the whole clip (256 bytes/frame, 45 KB total), and the
    clip is CFR by construction, so frame ``i`` is at ``i / DISSOLVE_FPS`` with
    no seek rounding to reason about — the same argument ``eased_box_track``
    makes.
    """
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip_path),
         "-vf", f"scale={DISSOLVE_READ_WIDTH}:-2,scale={size}:{size},format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    cells = size * size
    return [raw[i * cells:(i + 1) * cells] for i in range(len(raw) // cells)]


def dissolve_frame_time(index: int) -> float:
    return index / DISSOLVE_FPS


def dissolve_frame_index(t: float) -> int:
    """Index of the frame *displayed* at ``t`` — floor, not round.

    The frame at index 90 covers [3.000, 3.033), so t=3.000 is the last pure-B
    frame and t=2.999 is also pure B. Rounding instead would map 3.020 to 91,
    which is a different picture.
    """
    return int(t * DISSOLVE_FPS + 1e-9)


def dissolve_luma(thumb: bytes) -> float:
    """Mean cell value of one thumbnail, on the 0-255 gray scale."""
    return sum(thumb) / len(thumb)


def dissolve_spread(thumb: bytes) -> int:
    """Max-minus-min within one thumbnail: 0 proves the frame is a solid fill."""
    return max(thumb) - min(thumb)


def dissolve_cell_delta(a: bytes, b: bytes) -> tuple[float, float]:
    """``(mean, peak)`` absolute cell change between two thumbnails.

    Deliberately the same arithmetic as ``frames._cell_deltas``, restated here
    rather than imported: these tests have to be able to fail when that function
    changes, so sharing an implementation with the code under test would make
    the assertions circular.
    """
    diffs = [abs(x - y) for x, y in zip(a, b)]
    return sum(diffs) / len(diffs), float(max(diffs))


def dissolve_change_signal(thumbs: list[bytes]) -> list[tuple[float, float, float]]:
    """``[(timestamp, mean_delta, peak_delta)]``, one row per frame.

    Frame 0 has no predecessor and reports ``(0.0, 0.0)`` — matching
    ``measure_motion``, which seeds its first frame the same way.
    """
    out = [(0.0, 0.0, 0.0)]
    for i in range(1, len(thumbs)):
        mean, peak = dissolve_cell_delta(thumbs[i], thumbs[i - 1])
        out.append((dissolve_frame_time(i), mean, peak))
    return out


@pytest.fixture(scope="session")
def dissolve_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("clips") / "dissolve.mp4"
    build_dissolve_clip(path)
    return path


@pytest.fixture(scope="session")
def fast_cut_clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """24 shots of 0.5s — 12s at a cut rate no whole-second label can describe.

    The regime the shot block exists for: fast cutting lives in the 0.3-1.0s
    band, and a report that samples ~100 frames out of a longer version of this
    clip reads the gaps between *kept* frames as shot lengths and lands 12x wrong.
    """
    path = tmp_path_factory.mktemp("clips") / "fast_cuts.mp4"
    build_cut_clip(path, n=24, seg=0.5)
    return path
