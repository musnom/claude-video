#!/usr/bin/env python3
"""/watch entry point: download video, extract frames, parse transcript.

Prints a markdown report to stdout listing frame paths + transcript. Claude
then Reads each frame path to see the video.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path


SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from config import ensure_utf8_console, frame_cap, get_config  # noqa: E402

# Before the sibling imports below, so a failure raised during them also prints
# safely. The report contains U+2192 and an em dash, and video titles routinely
# carry emoji or CJK — all fatal on a piped Windows console otherwise, after
# the download and every frame extraction have already succeeded.
ensure_utf8_console()
from download import download, fetch_captions, is_url  # noqa: E402
from frames import MAX_FPS, MOTION_HARD_MAX, measure_motion, motion_envelope, parse_crop, validate_crop, auto_fps, auto_fps_focus, extract_at_timestamps, extract_motion, extract_keyframes, extract_scene_or_uniform, format_time, format_time_ms, get_metadata, merge_frames, parse_time, parse_timestamps  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import load_api_key, transcribe_video  # noqa: E402


# Frame kinds sampled densely enough that several land inside one second. Whole
# -second labels are actively wrong for these: at 50ms spacing, format_time
# rounds t=0.55 up to 00:01 while t=0.50 stays 00:00, putting the implied
# boundary a frame away from the real one. The scene, keyframe and uniform
# engines keep whole-second labels — they are navigation aids, and changing them
# would alter every existing report.
PRECISE_FRAME_REASONS = {"transcript-cue", "motion"}


def _frame_stamp(frame: dict) -> str:
    """Render a frame's timestamp at the precision its sampling method warrants."""
    if frame.get("reason") in PRECISE_FRAME_REASONS:
        return format_time_ms(frame["timestamp_seconds"])
    return format_time(frame["timestamp_seconds"])


def needs_pixels(cue_timestamps: list, motion: bool) -> bool:
    """Whether this run must download real video rather than audio-only.

    Extracted from main() because it is the sharpest edge in the whole frame
    pipeline and is otherwise only reachable with a captioned URL, which the
    test suite deliberately cannot use (no network).

    Transcript detail skips the video download when captions already cover the
    request. Any mode that grabs actual frames has to override that, or it
    silently produces nothing — the same class of failure that makes --fps
    useless. Both --timestamps and --motion need pixels.
    """
    return bool(cue_timestamps) or bool(motion)


def write_motion_data(
    path: Path,
    measured: list[dict],
    envelope: dict,
    frame_meta: dict,
    meta: dict,
    crop: tuple[int, int, int, int] | None,
) -> Path:
    """Write the measurements as JSON beside the frames.

    Deliberately stack-agnostic: durations, timestamps, geometry and a change
    signal, with no CSS, no keyframes, no easing names. The consumer decides
    whether this becomes a cubic-bezier, a spring config or a GSAP timeline, and
    that choice depends on the target stack and on what the user asked for —
    neither of which this script knows or should guess.
    """
    cx, cy, cw, ch = crop if crop else (None, None, None, None)
    payload = {
        "source": {
            "width": meta.get("width"),
            "height": meta.get("height"),
            "fps": meta.get("fps"),
            "duration": round(meta.get("duration_seconds") or 0.0, 3),
        },
        "crop": None if crop is None else {"x": cx, "y": cy, "w": cw, "h": ch},
        "window": {
            "start": frame_meta.get("window", (None, None))[0],
            "end": frame_meta.get("window", (None, None))[1],
        },
        "sampling": {
            "mode": "every-source-frame" if not frame_meta.get("interval") else "thinned",
            "interval_ms": round((frame_meta.get("interval") or 0.0) * 1000, 1),
            "sampled_fps": frame_meta.get("sampled_fps"),
            "source_fps": frame_meta.get("source_fps"),
            "min_gap_ms": frame_meta.get("min_gap_ms"),
            "max_gap_ms": frame_meta.get("max_gap_ms"),
            "dedup": False,
            "even_sampled": frame_meta.get("even_sampled", False),
        },
        "envelope": envelope,
        "frames": [
            {
                "i": f["index"],
                "t": f["timestamp_seconds"],
                "gap_ms": f["gap_ms"],
                "mean_delta": f["mean_delta"],
                "peak_delta": f["peak_delta"],
                "path": f["path"],
            }
            for f in measured
        ],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="watch",
        description="Download a video, extract auto-scaled frames, and surface the transcript.",
    )
    ap.add_argument("source", help="Video URL or local file path")
    ap.add_argument("--max-frames", type=int, default=None, help="Override frame cap")
    ap.add_argument("--resolution", type=int, default=512, help="Frame width in pixels (default 512)")
    ap.add_argument("--fps", type=float, default=None, help="Override auto-fps")
    ap.add_argument(
        "--detail",
        choices=["transcript", "efficient", "balanced", "token-burner"],
        default=None,
        help="Fidelity/speed dial: transcript (no frames), efficient (fast keyframes, cap 50), "
             "balanced (scene, cap 100), token-burner (scene, uncapped).",
    )
    ap.add_argument(
        "--timestamps",
        type=str,
        default=None,
        help="Comma-separated absolute timestamps (SS, MM:SS, HH:MM:SS) to grab a frame at, "
             "e.g. transcript-flagged 'look here' moments. Added on top of the detail frames "
             "(reserved against the cap); with --detail transcript these become the only frames.",
    )
    ap.add_argument("--start", type=str, default=None, help="Range start (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--end", type=str, default=None, help="Range end (SS, MM:SS, or HH:MM:SS)")
    ap.add_argument("--out-dir", type=str, default=None, help="Working directory (default: tmp)")
    ap.add_argument(
        "--no-whisper",
        action="store_true",
        help="Disable Whisper fallback. Report frames-only if no captions available.",
    )
    ap.add_argument(
        "--whisper",
        choices=["groq", "openai", "custom"],
        default=None,
        help="Force a specific Whisper backend. Default: a self-hosted endpoint if "
             "WATCH_WHISPER_ENDPOINT is set, else Groq, else OpenAI.",
    )
    ap.add_argument(
        "--crop",
        type=str,
        default=None,
        help="Crop to a region before scaling, as x,y,w,h in SOURCE pixels. Makes a small "
             "UI component fill the frame instead of being a few pixels inside it, so its "
             "position is measurable — and costs fewer tokens, not more.",
    )
    ap.add_argument(
        "--motion",
        action="store_true",
        help="Frame-by-frame motion analysis. Samples the source's OWN frames (no fps "
             "resampling), labels each with its measured timestamp to the millisecond, and "
             "never dedups. For measuring how fast an animation moves, how long a transition "
             "takes, or recreating easing. Overrides --detail; wants a short --start/--end.",
    )
    ap.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable near-duplicate frame removal. Keeps visually identical "
             "frames (static screen recordings, held slides) instead of collapsing them.",
    )
    args = ap.parse_args()

    config = get_config()
    detail = args.detail or str(config["detail"])
    configured_cap = frame_cap(detail)
    if args.max_frames is not None:
        max_frames = args.max_frames
    else:
        max_frames = configured_cap
    if max_frames is not None and max_frames < 1:
        raise SystemExit("--max-frames must be greater than zero")
    budget_cap = max_frames if max_frames is not None else 100
    cue_timestamps = parse_timestamps(args.timestamps)
    crop = parse_crop(args.crop)

    if args.out_dir:
        work = Path(args.out_dir).expanduser().resolve()
    else:
        work = Path(tempfile.mkdtemp(prefix="watch-"))
    work.mkdir(parents=True, exist_ok=True)
    print(f"[watch] working dir: {work}", file=sys.stderr)

    url_source = is_url(args.source)
    dl: dict = {"subtitle_path": None, "info": {}, "downloaded": False}
    transcript_segments: list[dict] = []
    transcript_text: str | None = None
    transcript_source: str | None = None
    video_path: str | None = None

    if url_source:
        print("[watch] checking metadata/captions via yt-dlp…", file=sys.stderr)
        dl = fetch_captions(args.source, work / "download")
        if dl.get("subtitle_path"):
            try:
                transcript_segments = parse_vtt(dl["subtitle_path"])
                transcript_text = format_transcript(transcript_segments)
                transcript_source = "captions"
            except Exception as exc:
                print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)
                transcript_segments = []

    # --timestamps and --motion both need the video for frame grabs, so either
    # overrides the transcript-mode download skip (and forces a full, not
    # audio-only, fetch). Without --motion in both conditions, a user with
    # WATCH_DETAIL=transcript gets an audio-only download and zero frames — the
    # exact silent no-op that makes --fps useless.
    wants_pixels = needs_pixels(cue_timestamps, args.motion)
    audio_only = detail == "transcript" and not wants_pixels
    if detail == "transcript" and transcript_segments and not wants_pixels:
        video_path = None
    else:
        if url_source:
            print(
                "[watch] downloading audio via yt-dlp…" if audio_only
                else "[watch] downloading video via yt-dlp…",
                file=sys.stderr,
            )
            dl = download(
                args.source,
                work / "download",
                audio_only=audio_only,
            )
        else:
            print("[watch] using local file…", file=sys.stderr)
            dl = download(args.source, work / "download")
        video_path = dl["video_path"]

    meta = get_metadata(video_path) if video_path else {
        "duration_seconds": float((dl.get("info") or {}).get("duration") or 0),
        "width": None,
        "height": None,
        "codec": None,
        "has_audio": False,
    }
    full_duration = meta["duration_seconds"]
    crop = validate_crop(crop, meta.get("width"), meta.get("height"))

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)

    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")

    effective_start = start_sec if start_sec is not None else 0.0
    effective_end = end_sec if end_sec is not None else full_duration
    effective_duration = max(0.0, effective_end - effective_start)
    focused = start_sec is not None or end_sec is not None

    if focused:
        fps, target = auto_fps_focus(effective_duration, max_frames=budget_cap)
    else:
        fps, target = auto_fps(effective_duration, max_frames=budget_cap)
    if args.fps is not None:
        if args.motion:
            # Loud, because a silent no-op is the disease this mode cures.
            print(
                "[watch] --fps is ignored with --motion: motion samples the source's own "
                "frames rather than resampling to a rate. Narrow --start/--end to control "
                "the frame count instead.",
                file=sys.stderr,
            )
        fps = min(args.fps, MAX_FPS)
        target = max(1, int(round(fps * effective_duration)))

    if transcript_segments and focused:
        transcript_segments = filter_range(transcript_segments, start_sec, end_sec)
        transcript_text = format_transcript(transcript_segments)

    scope = (
        f"{format_time(effective_start)}-{format_time(effective_end)} ({effective_duration:.1f}s)"
        if focused else f"full {effective_duration:.1f}s"
    )
    frames: list[dict] = []
    frame_meta: dict = {"engine": "none", "candidate_count": 0, "selected_count": 0, "fallback": False}
    cue_frames: list[dict] = []
    cue_meta: dict = {}
    motion_env: dict = {}
    motion_data_path: Path | None = None

    # Transcript cues are pinned: extracted first and counted against the cap so
    # the detail engine never evicts the moments the user explicitly asked for.
    if cue_timestamps and video_path:
        cue_frames, cue_meta = extract_at_timestamps(
            video_path,
            work / "frames",
            cue_timestamps,
            resolution=args.resolution,
            max_frames=max_frames,
            start_seconds=start_sec,
            end_seconds=end_sec,
            crop=crop,
        )
        if cue_meta.get("dropped_out_of_window"):
            print(
                f"[watch] {cue_meta['dropped_out_of_window']} cue timestamp(s) outside the "
                "focus range — dropped",
                file=sys.stderr,
            )

    detail_budget = max_frames if max_frames is None else max(0, max_frames - len(cue_frames))

    # Motion mode replaces the detail engines entirely. Dispatched BEFORE the
    # `detail != "transcript"` guard below so it can never become a silent no-op
    # under a detail mode — the failure that makes --fps useless today.
    if args.motion and video_path:
        motion_cap = args.max_frames if args.max_frames is not None else MOTION_HARD_MAX
        print(
            f"[watch] motion: sampling source frames over {scope} "
            f"(source ~{meta.get('fps') or 0:.1f} fps, cap {min(motion_cap, MOTION_HARD_MAX)})…",
            file=sys.stderr,
        )
        frames, frame_meta = extract_motion(
            video_path,
            work / "frames",
            start_seconds=effective_start,
            end_seconds=effective_end,
            resolution=args.resolution,
            max_frames=motion_cap,
            source_fps=meta.get("fps"),
            crop=crop,
        )
        frames = measure_motion(frames)
        motion_env = motion_envelope(frames)
        motion_data_path = write_motion_data(
            work / "motion.json", frames, motion_env, frame_meta, meta, crop,
        )
    elif detail != "transcript" and video_path and detail_budget != 0:
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine_label = "keyframes" if detail == "efficient" else "scene-aware frames"
        print(
            f"[watch] extracting {engine_label} over {scope} "
            f"(target {target}, cap {cap_label})…",
            file=sys.stderr,
        )
        if detail == "efficient":
            frames, frame_meta = extract_keyframes(
                video_path,
                work / "frames",
                resolution=args.resolution,
                max_frames=detail_budget,
                start_seconds=start_sec,
                end_seconds=end_sec,
                dedup=not args.no_dedup,
                crop=crop,
            )
        else:  # balanced, token-burner
            frames, frame_meta = extract_scene_or_uniform(
                video_path,
                work / "frames",
                fps=fps,
                target_frames=target,
                resolution=args.resolution,
                max_frames=detail_budget,
                start_seconds=start_sec,
                end_seconds=end_sec,
                dedup=not args.no_dedup,
                crop=crop,
            )

    if cue_frames:
        frames = merge_frames(frames, cue_frames)

    if not transcript_segments and dl.get("subtitle_path"):
        try:
            all_segments = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)

    if not transcript_segments and not args.no_whisper and video_path and meta.get("has_audio"):
        backend, api_key = load_api_key(args.whisper)
        # A self-hosted endpoint has no key, so `custom` is enough on its own.
        if backend and (api_key or backend == "custom"):
            try:
                all_segments, used_backend = transcribe_video(
                    video_path,
                    work / "audio.mp3",
                    backend=backend,
                    api_key=api_key,
                )
                transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
                transcript_text = format_transcript(transcript_segments)
                transcript_source = f"whisper ({used_backend})"
            except SystemExit as exc:
                print(f"[watch] whisper fallback failed: {exc}", file=sys.stderr)
        else:
            hint = (
                f"--whisper {args.whisper} was set but the matching API key is missing"
                if args.whisper else
                "no subtitles and no Whisper API key found"
            )
            setup_py = SCRIPT_DIR / "setup.py"
            print(
                f"[watch] {hint} — run `python3 {setup_py}` to enable the Whisper fallback",
                file=sys.stderr,
            )
    elif not transcript_segments and video_path and not meta.get("has_audio"):
        print("[watch] no audio stream found — proceeding without transcription", file=sys.stderr)

    info = dl.get("info") or {}

    print()
    print("# watch: video report")
    print()
    print(f"- **Source:** {args.source}")
    if info.get("title"):
        print(f"- **Title:** {info['title']}")
    if info.get("uploader"):
        print(f"- **Uploader:** {info['uploader']}")
    print(f"- **Duration:** {format_time(full_duration)} ({full_duration:.1f}s)")
    if focused:
        print(
            f"- **Focus range:** {format_time(effective_start)} → {format_time(effective_end)} "
            f"({effective_duration:.1f}s)"
        )
    if meta.get("width") and meta.get("height"):
        print(f"- **Resolution:** {meta['width']}x{meta['height']} ({meta.get('codec') or 'unknown codec'})")
    if crop:
        cx, cy, cw, ch = crop
        # Source coordinates are stated so measured pixels can be converted back
        # to the real layout (and to CSS units) without guessing the scale.
        print(
            f"- **Crop:** {cw}x{ch} at ({cx},{cy}) in source pixels — frame coordinates "
            f"are offset by that origin"
        )
    range_mode = "focused" if focused else "full"
    if args.motion:
        print(f"- **Detail:** motion (overrides `{detail}`)")
    else:
        print(f"- **Detail:** {detail}")
    detail_count = frame_meta.get("selected_count", 0)
    if args.motion:
        src = frame_meta.get("source_fps")
        sampled = frame_meta.get("sampled_fps")
        lo, hi = frame_meta.get("min_gap_ms"), frame_meta.get("max_gap_ms")
        source_note = f"source ~{src:.1f} fps" if src else "source rate unknown"
        coverage = (
            "every source frame" if not frame_meta.get("interval")
            else f"thinned to 1 frame per {frame_meta['interval'] * 1000:.0f} ms"
        )
        print(
            f"- **Motion window:** {format_time_ms(effective_start)} → "
            f"{format_time_ms(effective_end)} ({effective_duration:.3f}s)"
        )
        print(
            f"- **Frames:** {detail_count} (motion, "
            f"sampled {sampled if sampled is not None else '?'} fps, {source_note}, "
            f"gaps {lo}-{hi} ms, {coverage}, dedup off, cap {frame_meta.get('cap')})"
        )
        if motion_env.get("first_motion") is not None:
            print(
                f"- **Motion envelope:** first change {format_time_ms(motion_env['first_motion'])}, "
                f"last {format_time_ms(motion_env['last_motion'])}, "
                f"**{motion_env['duration_ms']:.0f} ms**, peak at "
                f"{format_time_ms(motion_env['peak_at'])}"
            )
        else:
            print("- **Motion envelope:** no change detected above the noise floor")
        if motion_data_path:
            print(f"- **Motion data:** `{motion_data_path}` (per-frame times and change signal)")
        if frame_meta.get("even_sampled") or frame_meta.get("interval"):
            print()
            print(
                f"> **Warning:** the window exceeded the {frame_meta.get('cap')}-frame cap, so "
                f"motion sampled {sampled} fps against a source of ~{src:.1f} fps. Timestamps are "
                f"still measured, but motion faster than {hi} ms is not resolved. Narrow "
                "`--start`/`--end` to capture every frame."
                if src else
                f"> **Warning:** the window exceeded the {frame_meta.get('cap')}-frame cap and was "
                "thinned. Narrow `--start`/`--end` to capture every frame."
            )
    elif detail != "transcript":
        cap_label = "unlimited" if detail_budget is None else str(detail_budget)
        engine = frame_meta.get("engine", "scene")
        fallback = " with uniform fallback" if frame_meta.get("fallback") else ""
        deduped = frame_meta.get("deduped_count", 0)
        dedup_note = f", {deduped} near-duplicate{'s' if deduped != 1 else ''} dropped" if deduped else ""
        print(
            f"- **Frames:** {detail_count} selected from {frame_meta.get('candidate_count', detail_count)} "
            f"candidates ({engine}{fallback}{dedup_note}, {range_mode} range, budget {target}, cap {cap_label})"
        )
    elif not cue_frames:
        print("- **Frames:** skipped (transcript detail)")
    if cue_frames:
        dropped = cue_meta.get("dropped_out_of_window", 0)
        drop_note = f", {dropped} dropped outside range" if dropped else ""
        print(
            f"- **Cue frames:** {len(cue_frames)} at transcript-flagged timestamps "
            f"(transcript-cue{drop_note})"
        )
    if frames:
        print(f"- **Frame size:** max {args.resolution}px wide, max 1998px tall")
    if transcript_segments:
        in_range = " in range" if focused else ""
        print(
            f"- **Transcript:** {len(transcript_segments)} segments{in_range} "
            f"(via {transcript_source or 'captions'})"
        )
    else:
        print("- **Transcript:** none available")

    if detail == "token-burner" and len(frames) > 250:
        print()
        print(
            f"> **Warning:** token-burner detail selected {len(frames)} frames. "
            "This may use a large number of image tokens."
        )

    if not focused and full_duration > 600 and detail not in ("transcript", "token-burner"):
        mins = int(full_duration // 60)
        print()
        print(
            f"> **Warning:** This is a {mins}-minute video. Frame coverage is sparse at this length "
            f"under `{detail}` detail — its cap spreads thin across the full clip. For better results, "
            "re-run with `--start HH:MM:SS --end HH:MM:SS` to zoom into a section, or use "
            "`--detail token-burner` to keep every scene-change frame across the whole video."
        )

    print()
    print("## Frames")
    print()
    if frames:
        print(f"Frames live at: `{work / 'frames'}`")
        print()
        print(
            "**Read each frame path below with the Read tool to view the image.** "
            "Frames are in chronological order; `t=MM:SS` is the absolute timestamp in the source "
            "video (`MM:SS.mmm` on precision-targeted frames, where several can share a second)."
        )
        print()
        for frame in frames:
            print(
                f"- `{frame['path']}` "
                f"(t={_frame_stamp(frame)}, reason={frame.get('reason', 'selected')})"
            )
    else:
        print("_No frames extracted._")

    print()
    print("## Transcript")
    print()
    if transcript_text:
        label = transcript_source or "captions"
        if focused:
            print(f"_Source: {label}. Filtered to {format_time(effective_start)} → {format_time(effective_end)}:_")
        else:
            print(f"_Source: {label}._")
        print()
        print("```")
        print(transcript_text)
        print("```")
    elif detail == "transcript":
        print(
            "_No transcript available at transcript detail. Captions were missing and Whisper was "
            "unavailable or failed, so there is no visual fallback here. Re-run with "
            "`--detail balanced` for frames._"
        )
    elif focused and dl.get("subtitle_path"):
        print(f"_No transcript lines fell inside {format_time(effective_start)} → {format_time(effective_end)}._")
    else:
        setup_py = SCRIPT_DIR / "setup.py"
        print(
            "_No transcript available — proceed with frames only. "
            "Captions were missing and the Whisper fallback was unavailable "
            "(no API key set, or `--no-whisper` was used). "
            f"Run `python3 {setup_py}` to enable Whisper, then re-run._"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
