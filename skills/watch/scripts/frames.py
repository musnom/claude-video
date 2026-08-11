#!/usr/bin/env python3
"""Probe video metadata and extract frames at an auto-scaled fps.

Auto-fps targets a frame budget, not a fixed rate. Token cost scales with frame
count, so budget-by-duration keeps short videos dense and long videos capped.
When a user-specified range is passed, focused-mode budgets denser (they are
zooming in for detail).
"""
from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


MAX_FPS = 2.0
SCENE_THRESHOLD = 0.20
# Keep scene-detection results once we have at least this many distinct shots.
# Below this the video is effectively static (screen recording, talking head),
# so we fall back to uniform sampling. Matching the reference fork's behaviour,
# this is a low floor — NOT the frame budget — so normal videos with cuts use
# the (single-pass) scene engine instead of paying for a wasted second decode.
SCENE_MIN_FRAMES = 8
# Below this many decoded keyframes a clip is too sparse for keyframe coverage
# (very short or oddly encoded), so the cheap tier falls back to uniform.
KEYFRAME_MIN = 4
MAX_READ_DIMENSION = 1998
# Frame-delta dedup: downscale each frame to a DEDUP_THUMB x DEDUP_THUMB
# grayscale thumbnail and treat two frames as near-identical when their mean
# per-pixel difference (0-255) is at or below DEDUP_THRESHOLD. Conservative on
# purpose: only collapses frames that are visually the same shot, so a code diff
# / scrolling terminal / slide-gaining-a-bullet survives. Unlike a within-frame
# perceptual hash, this distinguishes flat frames (solid slides, fades) by luma.
DEDUP_THUMB = 16
DEDUP_THRESHOLD = 2.0
SHOWINFO_TS_RE = re.compile(r"pts_time:([0-9.]+)")

# --- ffmpeg frame-sync flag compatibility ------------------------------------
# ffmpeg 5.1 renamed the global -vsync to the per-file -fps_mode, and 9.0 REMOVED
# -vsync outright. -fps_mode does not exist before 5.1, and Ubuntu 22.04 LTS
# still ships 4.4.2 (supported through 2027) — so neither spelling works
# everywhere and we have to ask the local binary which one it takes.
#
# The flag is load-bearing, not cosmetic: without it the image2 muxer runs our
# sparse select/keyframe output through constant-frame-rate sync and writes one
# JPEG per *source* frame instead of one per *selected* frame. Measured on a 9s
# three-cut clip through the real scene filter: 224 JPEGs with no flag, 3 with
# either spelling.
FRAME_SYNC_MODE = "vfr"
_FRAME_SYNC_OPTIONS = ("fps_mode", "vsync")  # preference order: modern first


@functools.lru_cache(maxsize=None)
def _ffmpeg_accepts_option(name: str) -> bool:
    """True if the local ffmpeg recognizes ``-<name> <FRAME_SYNC_MODE>``.

    Runs a ~17ms one-frame null job with the exact flag/value pair we intend to
    emit, so a success proves the pair works end to end.

    On a non-zero exit we deny support only when ffmpeg explicitly named the
    option as unrecognized. Two reasons that beats checking the exit code:
    argument splitting happens before any input is opened, so the message
    appears even on a build without lavfi; and the code itself is not stable
    (unrecognized-option exits 8 on 8.1.1 but 1 on 4.x), which is the exact
    version-coupling this function exists to remove.

    Every other failure is inconclusive and we keep the flag. If it truly were
    unsupported the extraction call fails exactly as it does today, whereas
    guessing "unsupported" would hand an ffmpeg 9 a removed option and
    guarantee the failure this probe exists to prevent.

    Deliberately not a version-banner or ``-h full`` parse: banners are
    unparseable on git and distro builds (``N-109871-g<sha>``,
    ``n4.4.2-0ubuntu0.22.04.1``) and defeated by backports, while ``-h full`` is
    ~1MB of help text whose formatting is not a stable API — and a naive
    ``vsync`` grep there also matches the unrelated ``avsynctest`` filter.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi", "-i", "nullsrc=d=0.04",
        "-frames:v", "1",
        f"-{name}", FRAME_SYNC_MODE,
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError:
        # Cannot spawn at all; the callers' shutil.which guard reports it with a
        # far better message than anything we could raise from here.
        return True
    if result.returncode == 0:
        return True
    return f"Unrecognized option '{name}'" not in (result.stderr or "")


@functools.lru_cache(maxsize=None)
def frame_sync_args() -> tuple[str, ...]:
    """The frame-sync argv pair this ffmpeg understands, or ``()`` if neither.

    Prefers ``-fps_mode``. Both call sites run at ``-loglevel info`` because they
    parse ``showinfo`` out of stderr, and at that level ``-vsync`` writes
    "-vsync is deprecated. Use -fps_mode" into the very stream SHOWINFO_TS_RE
    scans. It does not corrupt parsing today, but it is needless coupling and it
    pollutes the stderr we surface in SystemExit messages.

    Memoized per process: watch.py is one process per /watch and this module
    spawns ffmpeg 3-5 times per run, so one probe amortizes away. Deliberately
    not persisted to disk — such a cache goes stale the moment the user upgrades
    ffmpeg, reintroducing the exact fatal mismatch this prevents.

    Returns a tuple rather than a list so the cached value cannot be mutated by
    a caller.
    """
    for name in _FRAME_SYNC_OPTIONS:
        if _ffmpeg_accepts_option(name):
            return (f"-{name}", FRAME_SYNC_MODE)
    # No released ffmpeg lands here. Degrade rather than raise: extraction still
    # succeeds, it just emits one JPEG per source frame. Those duplicates are
    # byte-identical, so dedupe_perceptual (on by default) collapses them and
    # _even_sample enforces the cap. The cost is decode time, not correctness.
    print(
        "[watch] this ffmpeg accepts neither -fps_mode nor -vsync; frame "
        "selection may emit duplicate frames (dedup collapses them)",
        file=sys.stderr,
    )
    return ()


# --- motion mode -------------------------------------------------------------
# Runaway guard only, NOT a token budget. Motion runs are deliberately allowed to
# be expensive — the whole point is to see every frame of a short window. This
# exists so that pointing --motion at a two-hour video fails with a message
# instead of trying to extract 200,000 JPEGs.
MOTION_HARD_MAX = 2000


def parse_frame_rate(value: str | None) -> float | None:
    """Parse ffprobe's rational frame rate. ``"30000/1001"`` -> 29.97.

    Returns None for the several ways ffprobe says "I don't know": ``"0/0"``,
    an empty string, or a missing key.
    """
    if not value:
        return None
    text = str(value).strip()
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            denominator = float(den)
            if denominator == 0:
                return None
            rate = float(num) / denominator
        else:
            rate = float(text)
    except ValueError:
        return None
    return rate if rate > 0 else None


def motion_interval(window_seconds: float, source_fps: float | None, cap: int) -> float:
    """Minimum seconds between kept frames; ``0.0`` means keep every source frame.

    Probing first matters on bursty sources. A screen recording that holds a
    frame for 300ms has far fewer real frames than its wall-clock duration
    suggests, so computing ``window/cap`` unconditionally would decimate a clip
    that was never near the cap.
    """
    if window_seconds <= 0 or cap <= 0:
        return 0.0
    if source_fps is None or source_fps <= 0:
        return 0.0
    if source_fps * window_seconds <= cap:
        return 0.0
    return window_seconds / cap


def extract_motion(
    video_path: str,
    out_dir: Path,
    start_seconds: float,
    end_seconds: float,
    resolution: int = 512,
    max_frames: int = MOTION_HARD_MAX,
    source_fps: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> tuple[list[dict], dict]:
    """Extract the source's own frames across a window, with measured timestamps.

    Built for measuring motion — how long a transition takes, what its easing
    curve looks like — which needs two things the other engines cannot give.

    **No resampling.** The other dense path (``extract``) uses ``-vf fps=N``, a
    constant-frame-rate resampler: for each output slot it duplicates or drops a
    source frame and labels the copy with the *slot* time rather than the time of
    the pixels. On a screen recording that holds a frame between changes, that is
    catastrophic. Measured on a 48-frame clip with 317ms holds:

        -vf fps=60, label = i/fps     174 JPEGs, 126 duplicates, 304ms max error
        -vf fps=avg_frame_rate         48 JPEGs,  30 duplicates, 279ms max error
        select + measured pts          48 JPEGs,   0 duplicates,  ~0ms error

    Note the middle row: matching the resample rate to the source's average rate
    does not fix it, because an average is meaningless on a bursty source. The
    fix is to not resample at all.

    **No dedup.** There is deliberately no ``dedup`` parameter, so a caller
    cannot re-enable it by accident. ``dedupe_perceptual`` compares 16x16
    grayscale thumbnails against the last *kept* frame, which makes it a
    motion-dependent resampler: it emits a frame only once enough pixels have
    changed. On a moving-bar clip it collapsed 180 frames to 15, and the
    survivors landed 350/267/200/200/183ms apart — it deletes precisely the slow
    ends of an ease curve, which is the shape being measured.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    window = max(0.0, end_seconds - start_seconds)
    cap = max(1, min(max_frames, MOTION_HARD_MAX))
    interval = motion_interval(window, source_fps, cap)

    # ffmpeg's documented "one frame every N seconds" idiom. interval=0 passes
    # every source frame, so native capture and decimation share one code path.
    select = (
        rf"select='isnan(prev_selected_t)+gte(t-prev_selected_t\,{interval:.6f})'"
    )
    # select BEFORE scale, so frames we discard are never scaled or encoded;
    # showinfo LAST, so it reports only what actually gets written.
    vf = f"{select},{_crop_filter(crop)}{_scale_filter(resolution)},showinfo"

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",          # showinfo writes at info level
        "-y",
        "-ss", f"{start_seconds:.3f}",
        "-to", f"{end_seconds:.3f}",
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
    ]
    # Load-bearing, not an optimization. Without it the image2 muxer expands the
    # selection back to constant frame rate and the JPEG list desyncs from the
    # showinfo list: measured 188 JPEGs against 48 stamps on the same clip.
    cmd += frame_sync_args()
    # Deliberately no -frames:v. That flag stops ffmpeg after N frames, which
    # truncates the tail of the window rather than thinning across it.
    cmd += ["-q:v", "4", str(out_dir / "frame_%04d.jpg")]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    files = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0 and not files:
        raise SystemExit(f"ffmpeg motion extraction failed: {result.stderr.strip()}")
    if not files:
        raise SystemExit(
            f"motion extraction produced no frames for "
            f"{format_time_ms(start_seconds)}-{format_time_ms(end_seconds)}"
        )

    timestamps = [round(float(m.group(1)), 6) for m in SHOWINFO_TS_RE.finditer(result.stderr)]
    if len(timestamps) != len(files):
        # Never paper over this with the `timestamps[i] if i < len(...)` idiom the
        # other engines use — a mislabeled motion frame is a wrong measurement
        # presented as a right one.
        raise SystemExit(
            f"motion extraction desynced: {len(files)} frames written but "
            f"{len(timestamps)} timestamps reported. This usually means ffmpeg "
            f"rejected the frame-sync flag {frame_sync_args() or '(none available)'} "
            "and re-expanded the selection to constant frame rate."
        )

    offset = start_seconds or 0.0
    candidates = [
        {
            "index": i,
            "timestamp_seconds": round(offset + ts, 3),
            "path": str(path),
            "reason": "motion",
        }
        for i, (path, ts) in enumerate(zip(files, timestamps))
    ]

    candidate_count = len(candidates)
    # Net for a bad probe (an unknown or wrong source_fps). Even-samples across
    # the window rather than dropping the tail.
    selected = _even_sample(candidates, cap) if candidate_count > cap else candidates

    stamps = [f["timestamp_seconds"] for f in selected]
    gaps = [round((b - a) * 1000) for a, b in zip(stamps, stamps[1:])]
    span = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0.0
    return selected, {
        "engine": "motion",
        "candidate_count": candidate_count,
        "selected_count": len(selected),
        "deduped_count": 0,
        "fallback": False,
        "window": (round(start_seconds, 3), round(end_seconds, 3)),
        "interval": interval,
        "source_fps": source_fps,
        "sampled_fps": round((len(stamps) - 1) / span, 2) if span > 0 else None,
        "min_gap_ms": min(gaps) if gaps else None,
        "max_gap_ms": max(gaps) if gaps else None,
        "even_sampled": candidate_count > cap,
        "cap": cap,
        "crop": crop,
    }


def _scale_filter(resolution: int) -> str:
    return (
        f"scale=w='min({resolution},iw)':h='min({MAX_READ_DIMENSION},ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse ``x,y,w,h`` in source pixels into a crop rect.

    Source coordinates, not scaled ones — the user reads them off the video, and
    the scale factor is an implementation detail they should not have to know.
    """
    if not value:
        return None
    parts = [p.strip() for p in str(value).split(",")]
    if len(parts) != 4:
        raise SystemExit(
            f"--crop expects x,y,w,h in source pixels (got {value!r})"
        )
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise SystemExit(f"--crop values must be whole pixels (got {value!r})")
    if w <= 0 or h <= 0:
        raise SystemExit(f"--crop width and height must be positive (got {w}x{h})")
    if x < 0 or y < 0:
        raise SystemExit(f"--crop x and y must be non-negative (got {x},{y})")
    return x, y, w, h


def validate_crop(
    crop: tuple[int, int, int, int] | None,
    width: int | None,
    height: int | None,
) -> tuple[int, int, int, int] | None:
    """Check a crop fits inside the source. A clear error beats an ffmpeg trace."""
    if crop is None or not width or not height:
        return crop
    x, y, w, h = crop
    if x + w > width or y + h > height:
        raise SystemExit(
            f"--crop {x},{y},{w},{h} extends past the {width}x{height} source "
            f"(needs {x + w}x{y + h}). Check the rect against the video's own resolution."
        )
    return crop


def _crop_filter(crop: tuple[int, int, int, int] | None) -> str:
    """Crop clause for a filter chain, or empty. Always precedes scale, so the
    region fills the output frame instead of being a few pixels inside it."""
    if crop is None:
        return ""
    x, y, w, h = crop
    return f"crop={w}:{h}:{x}:{y},"


def _clamp_fps(fps: float, duration_seconds: float, max_frames: int) -> tuple[float, int]:
    fps = min(fps, MAX_FPS)
    target = min(max_frames, max(1, int(round(fps * duration_seconds))))
    return fps, target


def parse_time(value: str | float | int | None) -> float | None:
    """Parse SS, MM:SS, or HH:MM:SS (with optional .ms) into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"Cannot parse time value: {value!r} (expected SS, MM:SS, or HH:MM:SS)")


def format_time(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def format_time_ms(seconds: float) -> str:
    """``MM:SS.mmm`` (``H:MM:SS.mmm`` past an hour) — format_time's clock, one order finer.

    Not a competing formatter: same origin, same field layout, same hour rule, so
    ``format_time`` is exactly this value rounded to the second. A test pins that.

    Used only where several frames can land inside one second — cue frames and
    motion frames. Everything else stays whole-second on purpose. ``format_time``
    is shared with ``transcribe.format_transcript`` so the frame and transcript
    clocks agree; a caption cue is a ±1s truth and rendering it as ``[00:12.317]``
    would be precision the source does not have.

    Why this matters: ``format_time`` *rounds*, so at 50ms sampling a frame at
    t=0.55 prints ``00:01`` while the one at t=0.50 prints ``00:00`` — putting the
    implied boundary a frame away from where the change actually is. Read off a
    report like that, a 300ms transition becomes an unanswerable question.

    Milliseconds rather than centiseconds: 120fps content collides at two
    decimals, and ffmpeg's ``pts_time`` and the ``-ss`` argv are already 3-decimal.
    """
    # Integer milliseconds throughout, so .9996 carries into the next second
    # instead of rendering as ".1000".
    total_ms = int(round(seconds * 1000))
    return f"{format_time(total_ms // 1000)}.{total_ms % 1000:03d}"


def get_metadata(video_path: str) -> dict:
    if shutil.which("ffprobe") is None:
        raise SystemExit("ffprobe is not installed. Install with: brew install ffmpeg")

    result = subprocess.run(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(Path(video_path).resolve()),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"ffprobe failed: {result.stderr.strip()}")

    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = float(fmt.get("duration") or video_stream.get("duration") or 0)
    return {
        "duration_seconds": duration,
        # Used by motion mode to decide whether the window fits under the cap at
        # native rate. Never used as a resample rate — see extract_motion.
        "fps": parse_frame_rate(
            video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        ),
        "width": video_stream.get("width"),
        "height": video_stream.get("height"),
        "codec": video_stream.get("codec_name"),
        "size_bytes": int(fmt.get("size") or 0),
        "has_audio": audio_stream is not None,
    }


def auto_fps(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Pick fps that targets a sensible frame budget for full-video scans."""
    if duration_seconds <= 0:
        return 1.0, 1

    if duration_seconds <= 30:
        target = min(max_frames, max(12, int(round(duration_seconds))))
    elif duration_seconds <= 60:
        target = min(max_frames, 40)
    elif duration_seconds <= 180:  # 3 min
        target = min(max_frames, 60)
    elif duration_seconds <= 600:  # 10 min
        target = min(max_frames, 80)
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def auto_fps_focus(duration_seconds: float, max_frames: int = 100) -> tuple[float, int]:
    """Denser budget for user-specified ranges — they are zooming in for detail."""
    if duration_seconds <= 0:
        return min(MAX_FPS, 2.0), 2

    if duration_seconds <= 5:
        target = min(max_frames, max(10, int(round(duration_seconds * 6))))
    elif duration_seconds <= 15:
        target = min(max_frames, max(30, int(round(duration_seconds * 4))))
    elif duration_seconds <= 30:
        target = min(max_frames, 60)
    elif duration_seconds <= 60:
        target = min(max_frames, 80)
    elif duration_seconds <= 180:
        target = max_frames
    else:
        target = max_frames

    return _clamp_fps(target / duration_seconds, duration_seconds, max_frames)


def extract(
    video_path: str,
    out_dir: Path,
    fps: float,
    resolution: int = 512,
    max_frames: int = 100,
    start_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    end_seconds: float | None = None,
) -> list[dict]:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
    ]

    # -ss before -i = fast seek (keyframe-snap, good enough for preview frames).
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", f"fps={fps},{_crop_filter(crop)}{_scale_filter(resolution)}",
        "-frames:v", str(max_frames),
        "-q:v", "4",
        output_pattern,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg frame extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [
        {
            "index": i,
            "timestamp_seconds": round(offset + (i / fps if fps > 0 else 0.0), 2),
            "path": str(p),
            "reason": "uniform",
        }
        for i, p in enumerate(frames)
    ]


def extract_scene_candidates(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    max_frames: int | None = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    threshold: float = SCENE_THRESHOLD,
) -> list[dict]:
    """Extract first frame plus ffmpeg scene-change frames.

    When ``max_frames`` is set, ``-frames:v`` lets ffmpeg stop decoding once it
    has emitted that many frames (early exit) and avoids writing extras that we
    would only delete afterwards. ``None`` (uncapped "complete" detail) keeps
    every detected shot, as the user explicitly opted in.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",
    ]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]

    vf = f"select='eq(n\\,0)+gt(scene\\,{threshold})',{_crop_filter(crop)}{_scale_filter(resolution)},showinfo"
    cmd += [
        "-i", str(Path(video_path).resolve()),
        "-vf", vf,
    ]
    cmd += frame_sync_args()
    if max_frames is not None:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [
        "-q:v", "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise SystemExit(f"ffmpeg scene extraction failed: {result.stderr.strip()}")

    offset = start_seconds or 0.0
    timestamps = [round(offset + float(match.group(1)), 2) for match in SHOWINFO_TS_RE.finditer(result.stderr)]
    frames = sorted(out_dir.glob("frame_*.jpg"))
    out: list[dict] = []
    for i, path in enumerate(frames):
        ts = timestamps[i] if i < len(timestamps) else offset
        out.append({
            "index": i,
            "timestamp_seconds": ts,
            "path": str(path),
            "reason": "first-frame" if i == 0 else "scene-change",
        })
    return out


def _even_indices(count: int, n: int) -> list[int]:
    """Indices of ``n`` evenly-spaced items out of ``count`` (first + last kept).

    ``n >= count`` returns every index; ``n == 1`` returns just the first.
    """
    if n >= count:
        return list(range(count))
    if n <= 1:
        return [0]
    return [round(i * (count - 1) / (n - 1)) for i in range(n)]


def parse_timestamps(value: str | None) -> list[float]:
    """Parse a comma-separated list of times (SS, MM:SS, HH:MM:SS) into a
    sorted, de-duplicated list of seconds. Empty/blank tokens are skipped;
    an unparseable token raises (via :func:`parse_time`)."""
    if not value:
        return []
    out: list[float] = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        seconds = parse_time(token)
        if seconds is not None:
            out.append(float(seconds))
    return sorted(set(out))


def merge_frames(primary: list[dict], pinned: list[dict]) -> list[dict]:
    """Combine two frame lists into one chronological list and reindex 0..n-1.

    ``pinned`` frames (transcript cues) are never dropped — this is a plain
    union, so the cap is enforced upstream by reserving budget for the cues.
    """
    merged = sorted([*primary, *pinned], key=lambda f: f["timestamp_seconds"])
    for i, frame in enumerate(merged):
        frame["index"] = i
    return merged


def extract_at_timestamps(
    video_path: str,
    out_dir: Path,
    timestamps: list[float],
    resolution: int = 512,
    max_frames: int | None = None,
    start_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    end_seconds: float | None = None,
) -> tuple[list[dict], dict]:
    """Grab exactly one frame at each requested timestamp (transcript cues).

    Timestamps are absolute source seconds. Any falling outside an active
    ``[start, end]`` focus window are dropped. Files use a ``cue_*.jpg`` prefix
    so they sit alongside detail-engine ``frame_*.jpg`` output without either
    clobbering the other. When more cues than ``max_frames`` survive, they are
    even-sampled (first + last kept) before extraction.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("cue_*.jpg"):
        existing.unlink()

    lo = start_seconds or 0.0
    hi = end_seconds if end_seconds is not None else float("inf")
    # 3 decimals: these are precision-targeted frames rendered as MM:SS.mmm,
    # so rounding the request to 10ms would quantise below one frame period
    # at 120fps and show up as a label that disagrees with the argv.
    requested = sorted(set(round(float(t), 3) for t in timestamps))
    in_window = [t for t in requested if lo <= t <= hi]
    dropped = len(requested) - len(in_window)

    if max_frames is not None and len(in_window) > max_frames:
        points = [in_window[i] for i in _even_indices(len(in_window), max_frames)]
    else:
        points = in_window

    out: list[dict] = []
    for t in points:
        path = out_dir / f"cue_{len(out):04d}.jpg"
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-y",
            "-ss", f"{t:.3f}",
            "-i", str(Path(video_path).resolve()),
            "-frames:v", "1",
            "-vf", f"{_crop_filter(crop)}{_scale_filter(resolution)}",
            "-q:v", "4",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0 and path.exists():
            out.append({
                "index": len(out),
                "timestamp_seconds": t,
                "path": str(path),
                "reason": "transcript-cue",
            })

    meta = {
        "engine": "timestamps",
        "candidate_count": len(requested),
        "selected_count": len(out),
        "dropped_out_of_window": dropped,
        "fallback": False,
    }
    return out, meta


def _even_sample(candidates: list[dict], n: int) -> list[dict]:
    """Pick ``n`` evenly-spaced candidates (always including first and last),
    delete the JPEGs we drop, and reindex the survivors 0..len-1.

    Shared by every capped engine so all detail modes sample the same way:
    detect all candidates across the full range, then thin down to the cap.
    ``n >= len(candidates)`` keeps everything (the uncapped / under-cap case).
    """
    selected = [candidates[i] for i in _even_indices(len(candidates), n)]

    keep_paths = {sel["path"] for sel in selected}
    for cand in candidates:
        if cand["path"] not in keep_paths:
            try:
                Path(cand["path"]).unlink()
            except OSError:
                pass
    for i, frame in enumerate(selected):
        frame["index"] = i
    return selected


def _frame_delta(a: bytes, b: bytes) -> float:
    """Mean absolute per-pixel difference (0-255) between two grayscale
    thumbnails. Mismatched lengths are treated as maximally different so a
    decode hiccup never collapses distinct frames."""
    if not a or len(a) != len(b):
        return float("inf")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _thumb_frames(paths: list[Path]) -> list[bytes]:
    """Decode every frame in ``paths`` to a small grayscale thumbnail via one
    ffmpeg pass over the JPEG sequence.

    ffmpeg does the pixel decode (keeps us pure-stdlib); we slice the raw
    grayscale stream into one ``DEDUP_THUMB``-square thumbnail per frame.
    Fail-open: any ffmpeg error, an unrecognized name, or a byte-count mismatch
    returns ``[]`` so the caller skips dedup rather than breaking extraction.
    """
    if not paths:
        return []
    paths = [Path(p) for p in paths]
    m = re.match(r"(.*?)(\d+)(\.[A-Za-z0-9]+)$", paths[0].name)
    if m is None:
        return []
    prefix, digits, ext = m.group(1), m.group(2), m.group(3)
    pattern = str(paths[0].parent / f"{prefix}%0{len(digits)}d{ext}")

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-start_number", str(int(digits)),
        "-i", pattern,
        "-vf", f"scale={DEDUP_THUMB}:{DEDUP_THUMB},format=gray",
        "-f", "rawvideo",
        "-",
    ]
    # Intentionally no text=/encoding= here, unlike every other call in this
    # module: stdout is raw grayscale pixel data that gets sliced by byte
    # offset below. Decoding it as text would silently corrupt dedup.
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        return []

    chunk = DEDUP_THUMB * DEDUP_THUMB
    data = result.stdout
    if len(data) != chunk * len(paths):
        return []
    return [data[i * chunk:(i + 1) * chunk] for i in range(len(paths))]


def dedupe_perceptual(
    candidates: list[dict], threshold: float = DEDUP_THRESHOLD
) -> tuple[list[dict], int]:
    """Drop near-identical frames from a chronological candidate list.

    Thumbnails the extracted JPEGs and greedily removes frames whose mean
    per-pixel difference from the last kept one is within ``threshold``. Returns
    ``(survivors, dropped_count)``; a no-op (unchanged list) when thumbnails are
    unavailable or there are fewer than two candidates.
    """
    if len(candidates) <= 1:
        return candidates, 0
    thumbs = _thumb_frames([Path(c["path"]) for c in candidates])
    return _dedupe_by_deltas(candidates, thumbs, threshold)


def _dedupe_by_deltas(
    candidates: list[dict], thumbs: list[bytes], threshold: float = DEDUP_THRESHOLD
) -> tuple[list[dict], int]:
    """Greedily drop frames within ``threshold`` mean per-pixel difference of the
    last *kept* frame. Deletes dropped JPEGs and reindexes survivors 0..n-1 (same
    cleanup contract as :func:`_even_sample`). Fail-open: if ``thumbs`` does not
    line up 1:1 with ``candidates``, return them unchanged.
    """
    if len(thumbs) != len(candidates) or len(candidates) <= 1:
        return candidates, 0

    kept = [candidates[0]]
    last = thumbs[0]
    dropped: list[dict] = []
    for cand, thumb in zip(candidates[1:], thumbs[1:]):
        if _frame_delta(thumb, last) <= threshold:
            dropped.append(cand)
        else:
            kept.append(cand)
            last = thumb

    for cand in dropped:
        try:
            Path(cand["path"]).unlink()
        except OSError:
            pass
    for i, frame in enumerate(kept):
        frame["index"] = i
    return kept, len(dropped)


def extract_scene_or_uniform(
    video_path: str,
    out_dir: Path,
    fps: float,
    target_frames: int,
    resolution: int = 512,
    max_frames: int | None = 100,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    dedup: bool = True,
) -> tuple[list[dict], dict]:
    """Prefer scene selection, falling back to uniform only when the video is
    effectively static (fewer than ``SCENE_MIN_FRAMES`` detected shots).

    Scene cuts are detected across the *whole* range (uncapped), near-identical
    frames are dropped (:func:`dedupe_perceptual`, unless ``dedup`` is False),
    and the survivors are even-sampled down to ``max_frames`` via
    :func:`_even_sample`, exactly like the keyframe engine. This costs a full
    decode, but it guarantees coverage spans the entire clip — capping detection
    with ``-frames:v`` instead would keep only the first ``max_frames`` cuts and
    drop the tail of long videos (and could even fall below ``SCENE_MIN_FRAMES``
    and misfire the uniform fallback on a cut-heavy clip).
    """
    scene_frames = extract_scene_candidates(
        video_path,
        out_dir,
        resolution=resolution,
        max_frames=None,
        crop=crop,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    scene_count = len(scene_frames)
    if scene_count >= SCENE_MIN_FRAMES:
        deduped, n_dropped = dedupe_perceptual(scene_frames) if dedup else (scene_frames, 0)
        cap = len(deduped) if max_frames is None else max_frames
        selected = _even_sample(deduped, cap)
        return selected, {
            "engine": "scene",
            "candidate_count": scene_count,
            "deduped_count": n_dropped,
            "selected_count": len(selected),
            "fallback": False,
        }

    fallback_cap = target_frames if max_frames is None else min(max_frames, target_frames)
    frames = extract(
        video_path,
        out_dir,
        fps=fps,
        resolution=resolution,
        max_frames=fallback_cap,
        crop=crop,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    n_dropped = 0
    if dedup:
        frames, n_dropped = dedupe_perceptual(frames)
    return frames, {
        "engine": "uniform",
        "candidate_count": scene_count,
        "deduped_count": n_dropped,
        "selected_count": len(frames),
        "fallback": True,
    }


def extract_keyframes(
    video_path: str,
    out_dir: Path,
    resolution: int = 512,
    max_frames: int | None = 50,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    crop: tuple[int, int, int, int] | None = None,
    dedup: bool = True,
) -> tuple[list[dict], dict]:
    """Decode only keyframes (I-frames) — the cheap, near-instant tier.

    ``-skip_frame nokey`` makes ffmpeg reconstruct only keyframes, skipping all
    P/B frames. Encoders emit keyframes at scene cuts, so these already
    approximate "distinct moments". Near-identical frames are dropped
    (:func:`dedupe_perceptual`, unless ``dedup`` is False); over-cap →
    even-sample first→last; too few keyframes → uniform fallback.
    """
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not installed. Install with: brew install ffmpeg")

    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("frame_*.jpg"):
        existing.unlink()

    output_pattern = str(out_dir / "frame_%04d.jpg")
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-y",
    ]
    if start_seconds is not None:
        cmd += ["-ss", f"{start_seconds:.3f}"]
    if end_seconds is not None:
        cmd += ["-to", f"{end_seconds:.3f}"]
    cmd += [
        "-skip_frame", "nokey",
        "-i", str(Path(video_path).resolve()),
        "-vf", f"{_crop_filter(crop)}{_scale_filter(resolution)},showinfo",
    ]
    cmd += frame_sync_args()
    cmd += [
        "-q:v", "4",
        output_pattern,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")

    # A range containing no keyframe (e.g. --start past the only one on a static
    # screen recording) starves the mjpeg encoder, so ffmpeg fails at encoder
    # init instead of exiting 0 with no output — which made the
    # `len(candidates) < KEYFRAME_MIN` fallback below unreachable in exactly the
    # case it was written for, while `balanced` handled the same window fine.
    #
    # Gate on "produced no files" rather than on ffmpeg's wording: the message
    # is itself version-coupled (8.x says "Could not open encoder before EOF",
    # older builds said "No filtered frames for output stream"). The glob is
    # trustworthy because every frame_*.jpg in out_dir was deleted above, and
    # cue frames use a cue_*.jpg prefix — so a non-empty glob provably means
    # this invocation wrote something. Partial output still raises, unchanged.
    files = sorted(out_dir.glob("frame_*.jpg"))
    if result.returncode != 0:
        if files:
            raise SystemExit(f"ffmpeg keyframe extraction failed: {result.stderr.strip()}")
        detail = (result.stderr or "").strip().splitlines()
        print(
            "[watch] no keyframes decoded in range — falling back to uniform "
            f"sampling ({detail[-1] if detail else 'no ffmpeg output'})",
            file=sys.stderr,
        )

    offset = start_seconds or 0.0
    timestamps = [round(offset + float(m.group(1)), 2) for m in SHOWINFO_TS_RE.finditer(result.stderr)]
    candidates: list[dict] = []
    for i, path in enumerate(files):
        ts = timestamps[i] if i < len(timestamps) else offset
        candidates.append({
            "index": i,
            "timestamp_seconds": ts,
            "path": str(path),
            "reason": "keyframe",
        })

    # Too few keyframes → uniform fallback over the same range.
    if len(candidates) < KEYFRAME_MIN:
        for cand in candidates:
            try:
                Path(cand["path"]).unlink()
            except OSError:
                pass
        meta = get_metadata(video_path)
        full_duration = meta["duration_seconds"]
        eff_start = start_seconds or 0.0
        eff_end = end_seconds if end_seconds is not None else full_duration
        eff_duration = max(0.0, eff_end - eff_start)
        budget = max_frames if max_frames is not None else 100
        fps, _ = auto_fps(eff_duration, max_frames=budget)
        frames_out = extract(
            video_path,
            out_dir,
            fps=fps,
            resolution=resolution,
            max_frames=budget,
            crop=crop,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        n_dropped = 0
        if dedup:
            frames_out, n_dropped = dedupe_perceptual(frames_out)
        return frames_out, {
            "engine": "uniform",
            "candidate_count": len(candidates),
            "deduped_count": n_dropped,
            "selected_count": len(frames_out),
            "fallback": True,
        }

    # Detect-all, drop near-duplicates, then even-sample down to the cap (first +
    # last always kept). ``max_frames is None`` (uncapped) keeps every keyframe.
    candidate_count = len(candidates)
    deduped, n_dropped = dedupe_perceptual(candidates) if dedup else (candidates, 0)
    cap = len(deduped) if max_frames is None else max_frames
    selected = _even_sample(deduped, cap)
    return selected, {
        "engine": "keyframe",
        "candidate_count": candidate_count,
        "deduped_count": n_dropped,
        "selected_count": len(selected),
        "fallback": False,
    }


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: frames.py <video-path> <out-dir> [--fps F] [--resolution W] "
            "[--max-frames N] [--start T] [--end T] [--no-dedup]",
            file=sys.stderr,
        )
        raise SystemExit(2)

    video = sys.argv[1]
    out = Path(sys.argv[2])
    args = sys.argv[3:]

    fps_override = None
    resolution = 512
    max_frames = 100
    start_arg = None
    end_arg = None
    dedup = True
    i = 0
    while i < len(args):
        if args[i] == "--fps":
            fps_override = float(args[i + 1]); i += 2
        elif args[i] == "--resolution":
            resolution = int(args[i + 1]); i += 2
        elif args[i] == "--max-frames":
            max_frames = int(args[i + 1]); i += 2
        elif args[i] == "--start":
            start_arg = args[i + 1]; i += 2
        elif args[i] == "--end":
            end_arg = args[i + 1]; i += 2
        elif args[i] == "--no-dedup":
            dedup = False; i += 1
        else:
            i += 1

    meta = get_metadata(video)
    start_sec = parse_time(start_arg)
    end_sec = parse_time(end_arg)
    full_duration = meta["duration_seconds"]

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)

    focused = start_sec is not None or end_sec is not None
    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=max_frames)
    else:
        fps, target = auto_fps(effective_duration, max_frames=max_frames)
    if fps_override is not None:
        fps = fps_override
        target = max(1, int(round(fps * effective_duration)))

    frames = extract(
        video, out,
        fps=fps,
        resolution=resolution,
        max_frames=max_frames,
        start_seconds=start_sec,
        end_seconds=end_sec,
    )
    deduped_count = 0
    if dedup:
        frames, deduped_count = dedupe_perceptual(frames)
    print(json.dumps(
        {
            "meta": meta, "fps": fps, "target": target, "focused": focused,
            "deduped_count": deduped_count, "frames": frames,
        },
        indent=2,
    ))
