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

from config import ensure_utf8_console, frame_cap, get_config, read_setting  # noqa: E402

# Before the sibling imports below, so a failure raised during them also prints
# safely. The report contains U+2192 and an em dash, and video titles routinely
# carry emoji or CJK — all fatal on a piped Windows console otherwise, after
# the download and every frame extraction have already succeeded.
ensure_utf8_console()
from download import download, fetch_captions, is_url, refuse_if_playlist  # noqa: E402
from frames import MAX_FPS, MOTION_HARD_MAX, MOTION_TOKEN_CEILING, MOTION_TOKEN_HARD_CEILING, SCENE_THRESHOLD, frame_dimensions, measure_motion, motion_envelope, motion_token_estimate, parse_crop, validate_crop, auto_fps, auto_fps_focus, extract_at_timestamps, extract_motion, extract_keyframes, extract_scene_or_uniform, format_time, format_time_ms, get_metadata, merge_frames, parse_time, parse_timestamps  # noqa: E402
from transcribe import filter_range, format_transcript, parse_vtt  # noqa: E402
from whisper import LongAudioRefusal, load_api_key, transcribe_video  # noqa: E402


# Every frame kind now carries a sub-second label. Whole seconds were kept for
# the scene, keyframe and uniform engines on the grounds that those frames are
# navigation aids — which is true right up until the video cuts faster than once
# a second, and then the label stops being able to describe the thing it points
# at. Measured on a 120-shot clip that fit *under* the frame cap: 20% of
# consecutive frames printed the identical timestamp. Fast cutting lives in the
# 0.3-1.0s band, which is precisely the band whole seconds cannot represent, and
# it is the band an editing-style question is asking about.
#
# format_time rounds, so the old labels were not merely coarse, they were
# placed wrong: at 50ms spacing t=0.55 printed 00:01 while t=0.50 printed 00:00,
# putting the implied boundary a frame away from the real cut.
#
# Precision is honest for all of them because every timestamp is measured.
# Scene, keyframe and uniform frames carry ffmpeg's own pts_time; cue and
# gap-fill frames were requested at an exact instant and the decoder returns the
# nearest frame at or after it. `uniform` used to be the exception — it carried
# `i / fps`, where the sampler asked rather than where the pixels were, early by
# up to half a sampling period — until extract() stopped resampling and started
# selecting source frames.
PRECISE_FRAME_REASONS = {
    "transcript-cue", "motion", "scene-change", "first-frame", "keyframe",
    "uniform", "gap-fill",
}


def _frame_stamp(frame: dict) -> str:
    """Render a frame's timestamp at the precision its sampling method warrants."""
    if frame.get("reason") in PRECISE_FRAME_REASONS:
        return format_time_ms(frame["timestamp_seconds"])
    return format_time(frame["timestamp_seconds"])


def _whisper_miss_reason(transcription_error: str | None) -> str:
    """The honest tail of a "no transcript" report line.

    With a configured key whose request FAILED, the old unconditional "no API
    key set, or --no-whisper was used" stated two falsehoods and routed the
    reader to setup they had already done.
    """
    if transcription_error:
        detail = transcription_error.strip().replace("\n", " ")
        if len(detail) > 300:
            detail = detail[:300] + "…"
        return (
            f"the Whisper request failed: {detail} "
            "Retry with the other backend (`--whisper openai` / `--whisper groq`), "
            "or re-run later."
        )
    setup_py = SCRIPT_DIR / "setup.py"
    return (
        "the Whisper fallback was unavailable (no API key set, or `--no-whisper` "
        f"was used). Run `python3 {setup_py}` to enable Whisper, then re-run."
    )


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


def refuse_if_live(info: dict) -> None:
    """Stop before downloading a stream that has no end.

    yt-dlp handed a live URL records until the broadcast finishes, so a 24/7
    channel is an unbounded download and /watch appears to hang — no output, no
    error, forever. ``fetch_captions`` already runs ``--skip-download
    --write-info-json``, so the flags that answer this are on disk before the
    download starts and cost nothing.

    ``post_live`` is deliberately allowed: the broadcast has ended and the
    platform is still assembling the VOD, so the artifact is bounded even though
    the video is not a normal upload yet.

    Tolerates ``{}`` — ``fetch_captions`` passes ``--ignore-errors`` and never
    checks yt-dlp's return code, so an empty info dict is a normal outcome and
    must not be read as "not live".

    Deliberately does NOT suggest --start/--end as a workaround. Those flags trim
    during extraction, after the download has already completed, so on a live
    source they would bound nothing — the advice would sound actionable and hang
    exactly as before.
    """
    status = info.get("live_status")
    if status == "post_live":
        return
    if info.get("is_live") or status == "is_live":
        raise SystemExit(
            "This URL is a live broadcast. yt-dlp would record it until it ends, "
            "which for an ongoing stream means /watch never returns.\n"
            "Re-run once the stream has finished — the same URL then resolves to a "
            "normal recording — or point /watch at an already-archived clip."
        )
    if status == "is_upcoming":
        raise SystemExit(
            "This URL is a scheduled broadcast that has not started, so there is "
            "nothing to download yet. Re-run after it has aired."
        )


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
            "mode": (
                "every-source-frame"
                if not frame_meta.get("interval") and not frame_meta.get("even_sampled")
                else "thinned"
            ),
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
                # The monotone curve the envelope was read off. Included because
                # per-frame deltas alone cannot be used to re-derive it: on a slow
                # fade every one of them rounds to zero while this still climbs.
                "cum_delta": f["cum_delta"],
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
    ap.add_argument(
        "--cookies-from-browser",
        type=str,
        default=None,
        metavar="BROWSER[+KEYRING][:PROFILE][::CONTAINER]",
        help="Read cookies from a local browser profile so yt-dlp can reach a "
             "login-walled or age-gated video (e.g. `chrome`, `firefox:default`). "
             "Opt-in only — nothing is read unless you pass this.",
    )
    ap.add_argument(
        "--cookies",
        type=str,
        default=None,
        metavar="FILE",
        help="Netscape-format cookie file for yt-dlp. Alternative to "
             "--cookies-from-browser when you have exported cookies already.",
    )
    ap.add_argument(
        "--sub-langs",
        type=str,
        default=None,
        metavar="LIST",
        help="Comma-separated yt-dlp caption-language patterns (also settable as "
             "WATCH_SUB_LANGS). Use when the video's only track is a regional "
             "variant (en-CA) or a non-English language with no -orig track. Broad "
             "regexes fan out across YouTube's dozens of auto-translated tracks and "
             "collect rate-limit rejections — prefer exact codes.",
    )
    ap.add_argument(
        "--transcribe-anyway",
        action="store_true",
        help="Proceed with a cloud Whisper upload past the ~60-minute duration "
             "guard. Ask the user before passing this — it is their API bill.",
    )
    ap.add_argument(
        "--scene-threshold",
        type=float,
        default=None,
        help=f"How different two frames must be to count as a cut, 0-1 (default "
             f"{SCENE_THRESHOLD}). Lower finds more cuts. Motion graphics and slide decks "
             "change part of the frame rather than all of it and score around 0.05-0.10; "
             "camera cuts score 0.8+. Scene engine only (`--detail balanced`/`token-burner`).",
    )
    args = ap.parse_args()
    if args.scene_threshold is not None and not 0.0 < args.scene_threshold <= 1.0:
        raise SystemExit(
            f"--scene-threshold must be between 0 and 1 (got {args.scene_threshold})"
        )
    # A non-positive rate is not clampable intent — with the old bare min() it
    # sailed through to the select filter, where interval 0 passes EVERY decoded
    # frame (~18,000 JPEGs on a 10-minute 30fps clip) before thinning.
    if args.fps is not None and args.fps <= 0:
        raise SystemExit(f"--fps must be positive (got {args.fps:g})")

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

    sub_langs = args.sub_langs or read_setting("WATCH_SUB_LANGS") or None

    if url_source:
        print("[watch] checking metadata/captions via yt-dlp…", file=sys.stderr)
        dl = fetch_captions(
            args.source, work / "download",
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
            sub_langs=sub_langs,
        )
        if dl.get("subtitle_path"):
            try:
                transcript_segments = parse_vtt(dl["subtitle_path"])
                transcript_text = format_transcript(transcript_segments)
                transcript_source = "captions"
            except Exception as exc:
                print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)
                transcript_segments = []
        if dl.get("fetch_failed"):
            # Say when the guards below cannot see. The download itself still
            # carries a --match-filter '!is_live' belt, so a live URL fails in
            # seconds with the same message instead of recording forever.
            print(
                f"[watch] metadata fetch failed (yt-dlp exit "
                f"{dl.get('fetch_returncode')}) — cannot verify this URL is not "
                "a live stream or playlist before downloading; if it is live, "
                "yt-dlp will refuse it at download time instead of recording "
                "forever.",
                file=sys.stderr,
            )

    refuse_if_live(dl.get("info") or {})
    refuse_if_playlist(dl.get("info") or {})

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
                cookies_from_browser=args.cookies_from_browser,
                cookies_file=args.cookies,
                sub_langs=sub_langs,
            )
        else:
            print("[watch] using local file…", file=sys.stderr)
            dl = download(args.source, work / "download")
        # Re-checked on the post-download info: when the metadata fetch failed,
        # the pre-download check saw {} and passed vacuously — but the download
        # (bounded to one entry by --playlist-items) writes an info.json whose
        # playlist markers now answer the question.
        refuse_if_playlist(dl.get("info") or {})
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

    # An .m4a, an .mp3, or any container with no decodable video stream used to
    # abort with a raw ffmpeg dump, zero stdout and exit 1 — in the one case where
    # the transcript path would have worked perfectly.
    #
    # `frame_source` rather than clearing `video_path`: the Whisper gate below
    # requires `video_path` to be truthy, so nulling it here would also kill the
    # transcript, i.e. delete the entire reason for the fallback. Every frame
    # engine reads `frame_source`; everything audio reads `video_path`.
    # A still image is not a video with a problem, it is a different kind of
    # thing, and the useful answer is "read it directly" rather than a frame
    # budget. Today it dies inside the mjpeg encoder with a raw ffmpeg trace and
    # exit 1, which tells the reader nothing about what to do instead.
    if str(meta.get("format_name") or "").endswith("_pipe"):
        raise SystemExit(
            f"{Path(video_path).name} is a still image, not a video — there is nothing "
            "to sample over time. Read the file directly with the Read tool; it renders "
            "as an image without going through /watch."
        )

    has_video = bool(meta.get("width") and meta.get("height"))
    frame_source = video_path if has_video else None
    # `audio_only` means *we asked yt-dlp for audio* because transcript detail
    # covered the request — the source itself may be a perfectly ordinary video.
    # Saying "this source has no video stream" there states something false about
    # the video rather than about the run.
    source_lacks_video = bool(video_path) and not has_video and not audio_only
    if source_lacks_video:
        print(
            f"[watch] {Path(video_path).name} has no video stream — "
            "no frames to extract, continuing with the transcript",
            file=sys.stderr,
        )

    start_sec = parse_time(args.start)
    end_sec = parse_time(args.end)

    if start_sec is not None and start_sec < 0:
        raise SystemExit("--start must be non-negative")
    if end_sec is not None and start_sec is not None and end_sec <= start_sec:
        raise SystemExit("--end must be greater than --start")
    # A bare `--end 0` used to slip past the check above (it requires --start)
    # and produce a zero-length window: focused=True, an empty Frames section,
    # and no error. An empty window is a mistake, not a request.
    if end_sec is not None and start_sec is None and end_sec <= 0:
        raise SystemExit(f"--end must be greater than 0 (got {end_sec:.1f}s)")
    if full_duration > 0 and start_sec is not None and start_sec >= full_duration:
        raise SystemExit(f"--start {start_sec:.1f}s is past end of video ({full_duration:.1f}s)")
    # Clamp rather than reject: a long --end most plausibly means "through the
    # end". Unclamped it printed a Focus-range line for a range that does not
    # exist and budgeted frames against the phantom duration — which lands in a
    # SPARSER auto_fps_focus band, inverting the documented denser-focus
    # contract — and asked Whisper for audio past end-of-stream.
    if end_sec is not None and full_duration > 0 and end_sec > full_duration:
        print(
            f"[watch] --end {end_sec:.1f}s is past the end of the video "
            f"({full_duration:.1f}s) — clamped to the end",
            file=sys.stderr,
        )
        end_sec = full_duration

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
        # The clamp itself must be reported: the "--fps had no effect" warning
        # below cannot cover it, because on a uniform run the (clamped) rate DID
        # apply — the user asked for 25, sampling ran at 2, and nothing said so.
        # Worded conditionally: which engine runs is not known yet, so "sampling
        # at 2 fps" would be untrue on a scene/keyframe run.
        elif args.fps > MAX_FPS:
            print(
                f"[watch] --fps {args.fps:g} exceeds the {MAX_FPS:g} fps auto-sampling "
                f"ceiling — any uniform sampling will run at {MAX_FPS:g} fps",
                file=sys.stderr,
            )
        fps = min(args.fps, MAX_FPS)
        # Bounded by the budget cap like every auto-derived target: unbounded,
        # --fps 2 on a 10-minute token-burner run set a gap-fill target of 1200
        # while the report said the flag "had no effect".
        target = max(1, min(budget_cap, int(round(fps * effective_duration))))

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
    if cue_timestamps and frame_source:
        cue_frames, cue_meta = extract_at_timestamps(
            frame_source,
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
    if args.motion and frame_source:
        motion_cap = args.max_frames if args.max_frames is not None else MOTION_HARD_MAX
        # Blocking, not advisory. The existing warning below fires only when the
        # cap *thinned* the run — i.e. when it got cheaper — and it prints after
        # the JPEGs are already on disk and about to be read.
        source_fps = meta.get("fps")
        # An unknown frame rate is not a reason to skip the guard — it is a
        # reason to assume the worst: motion_interval does no thinning without
        # a rate, so the run extracts every source frame up to the cap.
        estimated, per_frame, estimated_tokens = motion_token_estimate(
            effective_duration, source_fps, motion_cap,
            meta.get("width"), meta.get("height"), args.resolution, crop,
        )
        dims = frame_dimensions(meta.get("width"), meta.get("height"), args.resolution, crop)
        if args.max_frames is None:
            if per_frame and estimated_tokens > MOTION_TOKEN_CEILING:
                raise SystemExit(
                    f"--motion over {scope} would extract ~{estimated} frames at "
                    f"{dims[0]}x{dims[1]} — roughly {estimated_tokens // 1000}k image tokens "
                    f"to read, against a {MOTION_TOKEN_CEILING // 1000}k ceiling.\n"
                    "Motion mode samples every source frame on purpose, so it wants one "
                    "transition rather than a scene:\n"
                    + (
                        f"  * narrow the window — `--start`/`--end` around the transition "
                        f"(at ~{source_fps:.0f} fps, "
                        f"{MOTION_TOKEN_CEILING // max(1, per_frame) / source_fps:.1f}s fits)\n"
                        if source_fps else
                        "  * narrow the window — `--start`/`--end` around the transition "
                        "(this source reports no frame rate, so the estimate assumes the "
                        f"{MOTION_HARD_MAX}-frame cap)\n"
                    ) +
                    "  * or `--crop x,y,w,h` to isolate the moving component, which cuts "
                    "the per-frame cost as well as making the motion measurable\n"
                    f"  * or pass `--max-frames N` to accept the cost deliberately"
                )
        elif per_frame and estimated_tokens > MOTION_TOKEN_HARD_CEILING:
            # An explicit --max-frames chooses the frame COUNT; it does not
            # bound the cost of reading the frames, which scales with
            # --resolution and the source size as well.
            raise SystemExit(
                f"--motion --max-frames {args.max_frames} over {scope} would be "
                f"~{estimated} frames at {dims[0]}x{dims[1]} — roughly "
                f"{estimated_tokens // 1000}k image tokens to read, over the absolute "
                f"{MOTION_TOKEN_HARD_CEILING // 1000}k ceiling, which an explicit "
                "frame cap does not lift.\n"
                "Lower --resolution, add --crop x,y,w,h to isolate the moving "
                "component, or narrow --start/--end."
            )
        if per_frame:
            # The cost prints unconditionally and BEFORE extraction — when it can
            # still inform a decision, not after the JPEGs exist.
            print(
                f"[watch] motion: ~{estimated} frames at {dims[0]}x{dims[1]} ≈ "
                f"{max(1, estimated_tokens // 1000)}k image tokens to read",
                file=sys.stderr,
            )
        print(
            f"[watch] motion: sampling source frames over {scope} "
            f"(source ~{meta.get('fps') or 0:.1f} fps, cap {min(motion_cap, MOTION_HARD_MAX)})…",
            file=sys.stderr,
        )
        frames, frame_meta = extract_motion(
            frame_source,
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
    elif args.motion:
        # There was no else here, so --motion with no video produced zero frames
        # and said nothing about it — the silent no-op this dispatch is ordered
        # to prevent, reached by a different route. Both ways of having no pixels
        # land here: no file at all, and a file with no video stream. The second
        # one used to fall through and still print a full motion block — sampled
        # fps, gap range, envelope — computed from an empty frame list.
        raise SystemExit(
            "--motion needs a video stream and this source has none "
            f"({Path(video_path).name if video_path else 'no video file was resolved'}). "
            "Point it at a local video file, or at the file printed in the footer of a "
            "previous run's report."
        )
    elif detail != "transcript" and frame_source and detail_budget != 0:
        cap_note = "uncapped" if detail_budget is None else f"cap {detail_budget}"
        engine_label = "keyframes" if detail == "efficient" else "scene-aware frames"
        # No "target N" here: which engine runs is only known after detection,
        # and the duration target binds solely on the uniform fallback. The
        # report below states the cap and budget the engine actually applied.
        print(
            f"[watch] extracting {engine_label} over {scope} ({cap_note})…",
            file=sys.stderr,
        )
        if detail == "efficient":
            frames, frame_meta = extract_keyframes(
                frame_source,
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
                frame_source,
                work / "frames",
                fps=fps,
                target_frames=target,
                resolution=args.resolution,
                max_frames=detail_budget,
                start_seconds=start_sec,
                end_seconds=end_sec,
                dedup=not args.no_dedup,
                crop=crop,
                scene_threshold=(
                    args.scene_threshold if args.scene_threshold is not None
                    else SCENE_THRESHOLD
                ),
                full_duration=full_duration,
            )

    # --scene-threshold has no meaning outside the scene engine: the keyframe
    # engine asks the decoder which frames are I-frames and never computes a
    # scene metric at all, and motion mode takes every frame. Same rule as --fps
    # below — this codebase treats a silently ignored flag as a bug.
    if args.scene_threshold is not None and (args.motion or detail in ("efficient", "transcript")):
        why = (
            "--motion samples every source frame" if args.motion
            else "--detail transcript extracts no frames at all" if detail == "transcript"
            else "--detail efficient selects keyframes, which the decoder marks and "
                 "which carry no scene score"
        )
        print(
            f"[watch] --scene-threshold {args.scene_threshold} had no effect: {why}. "
            "It binds only on `--detail balanced` and `--detail token-burner`.",
            file=sys.stderr,
        )

    # --fps reaches only a uniform sampler running at the caller's rate. The
    # scene and keyframe engines select on content; the keyframe engine's own
    # uniform fallback derives its rate from auto_fps rather than from the
    # caller. Keyed on what the engine reports rather than on its name, because
    # two different code paths both call themselves "uniform" and only one of
    # them honours the flag — that gap is why `--detail efficient --fps 1`
    # silently did nothing on a static clip.
    if args.fps is not None and not args.motion and frames and not frame_meta.get("fps_applied"):
        engine = frame_meta.get("engine", "this")
        why = (
            f"the {engine} engine picks frames by content, not at a fixed rate"
            if engine in ("scene", "keyframe")
            else "this uniform fallback derives its own rate from the clip duration"
        )
        print(
            f"[watch] --fps {args.fps} had no effect: {why}. Use --max-frames to change how "
            "many frames you get, --detail token-burner to keep every detected frame, or "
            "--start/--end to sample a window densely.",
            file=sys.stderr,
        )

    if cue_frames:
        # Order is load-bearing under --motion: motion.json was serialized before
        # this merge, so the measurement artifact stays cue-free while the report
        # lists both. Cue dicts carry no motion fields (gap_ms etc.) on purpose.
        frames = merge_frames(frames, cue_frames)

    if not transcript_segments and dl.get("subtitle_path"):
        try:
            all_segments = parse_vtt(dl["subtitle_path"])
            transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
            transcript_text = format_transcript(transcript_segments)
            transcript_source = "captions"
        except Exception as exc:
            print(f"[watch] subtitle parse failed: {exc}", file=sys.stderr)

    transcription_refused_long = False
    transcription_error: str | None = None
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
                    # Encode and upload only the window. The segments come back
                    # already shifted into absolute source time, which is what
                    # filter_range below selects on.
                    start_seconds=start_sec,
                    end_seconds=end_sec,
                    allow_long=args.transcribe_anyway,
                )
                transcript_segments = filter_range(all_segments, start_sec, end_sec) if focused else all_segments
                transcript_text = format_transcript(transcript_segments)
                transcript_source = f"whisper ({used_backend})"
            except LongAudioRefusal as exc:
                # Remembered so the report states the real reason — the generic
                # "no API key set" text would be false here. The message names
                # --transcribe-anyway; SKILL.md tells the model to relay the
                # estimate via AskUserQuestion and re-run on a yes.
                transcription_refused_long = True
                print(f"[watch] {exc}", file=sys.stderr)
            except SystemExit as exc:
                # Remembered for the report: with a configured key, "no API key
                # set" — the generic no-transcript text — is a false statement
                # about a request the server actually refused.
                transcription_error = str(exc)
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
        # Frame rate belongs here, not only under --motion. 24p / 30p / 60p is a
        # first-order property of how a video was made and it cannot be recovered
        # from the frames — they arrive as stills with no spacing information —
        # so a question about pacing or feel had no way to reach it.
        # %g so 30 prints as "30" and 30000/1001 as "29.97", rather than one of
        # them carrying meaningless trailing zeros.
        rate = meta.get("fps")
        rate_note = f" @ {rate:g} fps" if rate else ""
        print(
            f"- **Resolution:** {meta['width']}x{meta['height']}{rate_note} "
            f"({meta.get('codec') or 'unknown codec'})"
        )
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
        # `interval` covers thinning decided up front from the fps probe;
        # `even_sampled` covers thinning done afterwards because the probe was
        # wrong and the cap bit anyway. Claiming "every source frame" while the
        # second one happened describes a run that discarded most of its frames.
        if frame_meta.get("interval"):
            coverage = f"thinned to 1 frame per {frame_meta['interval'] * 1000:.0f} ms"
        elif frame_meta.get("even_sampled"):
            coverage = (
                f"thinned to {frame_meta.get('selected_count')} of "
                f"{frame_meta.get('candidate_count')} source frames"
            )
        else:
            coverage = "every source frame"
        print(
            f"- **Motion window:** {format_time_ms(effective_start)} → "
            f"{format_time_ms(effective_end)} ({effective_duration:.3f}s)"
        )
        # The section below lists motion + cue frames, so the count here must
        # account for both or the line disagrees with the list under it.
        cue_note = (
            f" + {len(cue_frames)} cue frame{'s' if len(cue_frames) != 1 else ''}"
            if cue_frames else ""
        )
        print(
            f"- **Frames:** {detail_count}{cue_note} (motion, "
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
            # A duration that stops at the edge of the window is a lower bound,
            # and saying so is the whole point: the docs tell you to use a tight
            # window, and a tight window is exactly what truncates the answer.
            edges = [
                name for name, flag in (
                    ("start", motion_env.get("clipped_start")),
                    ("end", motion_env.get("clipped_end")),
                ) if flag
            ]
            if edges:
                where = " and ".join(edges)
                print(
                    f"- **Envelope clipped:** motion was already underway at the {where} of the "
                    f"window, so {motion_env['duration_ms']:.0f} ms is a **lower bound**, not the "
                    "duration. Widen `--start`/`--end` past the transition on that side and re-run."
                )
        else:
            print(
                "- **Motion envelope:** no change detected — the largest run of change in this "
                f"window totalled {motion_env.get('event_change', 0.0):.1f}, under the "
                f"{motion_env.get('min_change') or 0.0:.1f} needed to count as motion "
                f"(thumbnail-cell units, 0-255; per-frame noise floor "
                f"{motion_env.get('floor') or 0.0:.1f})"
            )
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
    elif source_lacks_video:
        # Reported as a property of the source, not as a failure of the run: the
        # transcript below is a complete answer for an audio file, and this used
        # to be a raw ffmpeg dump on stderr with exit 1 and no stdout at all.
        print(
            "- **Frames:** none — this source has no video stream "
            f"({'audio only' if meta.get('has_audio') else 'no decodable streams'}). "
            "The transcript below is the whole report."
        )
    elif detail != "transcript":
        engine = frame_meta.get("engine", "scene")
        # The fallback engine is already named "uniform"; appending "with uniform
        # fallback" to it printed "uniform with uniform fallback".
        if frame_meta.get("fallback"):
            shots = frame_meta.get("scene_count")
            why = f", too few shots ({shots})" if shots is not None else ""
            engine = f"{engine} fallback{why}"
        fallback = ""
        deduped = frame_meta.get("deduped_count", 0)
        dedup_note = f", {deduped} near-duplicate{'s' if deduped != 1 else ''} dropped" if deduped else ""
        blank = frame_meta.get("blank_dropped", 0)
        blank_note = (
            f", {blank} trailing black frame{'s' if blank != 1 else ''} dropped (end card)"
            if blank else ""
        )
        # Say when frames were added, or the count silently disagrees with the
        # candidate count and the reader has no way to tell which frames landed
        # on a cut and which were placed to cover a hole. Say when filling
        # STOPPED, too — a fill count under the cap is not a failure, it means
        # the remaining holes proved static.
        filled = frame_meta.get("gap_filled", 0)
        rejected = frame_meta.get("gap_fill_rejected", 0)
        fill_failed = frame_meta.get("gap_fill_failed", 0)
        fill_note = ""
        if filled or rejected:
            fill_note = f", {filled} added to fill gaps"
            if rejected:
                # "stopped early" only when the fill actually did — the engine
                # reports its stop cause, and a run whose rejections were
                # followed by a fully spent budget did NOT stop early. Saying it
                # did told the reader the remaining holes were static when
                # raising --max-frames would have added coverage.
                early = (
                    "fill stopped early: "
                    if frame_meta.get("gap_fill_stop") in ("saturated", "min-gap")
                    else ""
                )
                fill_note += (
                    f" ({early}{rejected} near-duplicate or blank "
                    f"candidate{'s' if rejected != 1 else ''} rejected)"
                )
        if fill_failed:
            fill_note += f", {fill_failed} fill decode{'s' if fill_failed != 1 else ''} failed"
        # Cap and budget are read back from the engine that ran, not from the
        # duration `target` computed above. `target`/`fps` reach only the uniform
        # fallback, so the old unconditional "budget {target}" described a code
        # path that had not executed on any scene- or keyframe-engine run — the
        # number was real, it just governed nothing.
        effective_cap = frame_meta.get("effective_cap", detail_budget)
        cap_note = "uncapped" if effective_cap is None else f"cap {effective_cap}"
        engine_budget = frame_meta.get("budget")
        budget_note = f", budget {engine_budget}" if engine_budget is not None else ""
        # The effective uniform rate, stated only when the caller's --fps really
        # governed the sampling — the flag's silent clamp used to be invisible
        # precisely because no line ever said what rate actually ran.
        rate_note = f", sampled at {fps:g} fps" if frame_meta.get("fps_applied") else ""
        print(
            f"- **Frames:** {detail_count} selected from {frame_meta.get('candidate_count', detail_count)} "
            f"candidates ({engine}{fallback}{dedup_note}{blank_note}{fill_note}, "
            f"{range_mode} range{budget_note}{rate_note}, {cap_note})"
        )
        # Cut rhythm, from the full detected list rather than from whichever
        # frames survived sampling. Reading the gaps between kept frames as shot
        # lengths under-counted a fast-cut clip by 12x — and looked authoritative
        # doing it, because the timestamps themselves were correct.
        shots = frame_meta.get("shots") or {}
        if shots.get("cuts"):
            rate = f"{shots['per_minute']:.1f}/min" if shots.get("per_minute") else "rate unknown"
            # "at least", and the threshold named: detection misses cuts below
            # the scene threshold (measured -16% on ordinary footage, -65% on
            # low-contrast), so the count is a lower bound and quoting it as
            # exact was the report's own "state measured numbers" rule violated.
            threshold_used = (
                args.scene_threshold if args.scene_threshold is not None
                else SCENE_THRESHOLD
            )
            print(
                f"- **Shots:** at least {shots['cuts']} cuts (detected at scene threshold "
                f"{threshold_used:g}), {rate} — median {shots['median_s']:.2f}s, "
                f"p10 {shots['p10_s']:.2f}s, p90 {shots['p90_s']:.2f}s "
                f"(shortest {shots['shortest_s']:.2f}s, longest {shots['longest_s']:.2f}s)"
            )
            if deduped:
                print(
                    f"  Counted before near-duplicate removal, so up to {deduped} of these "
                    "may be cuts between visually similar shots rather than distinct ones."
                )
            if detail == "token-burner":
                times = ", ".join(format_time_ms(t) for t in shots["cut_times"])
                print(f"  Cuts at: {times}")
    elif not cue_frames:
        print("- **Frames:** skipped (transcript detail)")
    if cue_frames or cue_meta.get("extraction_failed"):
        dropped = cue_meta.get("dropped_out_of_window", 0)
        drop_note = f", {dropped} dropped outside range" if dropped else ""
        failed = cue_meta.get("extraction_failed", 0)
        failed_note = f", {failed} failed to decode" if failed else ""
        print(
            f"- **Cue frames:** {len(cue_frames)} at transcript-flagged timestamps "
            f"(transcript-cue{drop_note}{failed_note})"
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
            "Frames are in chronological order; `t=MM:SS.mmm` is the absolute timestamp in the "
            "source video. `scene-change`, `first-frame`, `keyframe` and `uniform` frames carry "
            "ffmpeg's measured presentation time, so the label is where the pixels are. "
            "`transcript-cue` and `gap-fill` frames carry the time that was *requested* — the "
            "decoder returns the nearest frame at or after it."
        )
        estimated_count = sum(1 for f in frames if f.get("estimated"))
        if estimated_count:
            print()
            print(
                f"{estimated_count} frame{'s are' if estimated_count != 1 else ' is'} "
                "marked `estimated` — no measured time was available for them. "
                "A uniform frame's estimate is its requested slot time; a "
                "scene/keyframe frame's estimate repeats the last measured "
                "timestamp, so several estimated frames may share one label. "
                "Treat these labels as approximate."
            )
        print()
        for frame in frames:
            estimated_note = ", estimated" if frame.get("estimated") else ""
            print(
                f"- `{frame['path']}` "
                f"(t={_frame_stamp(frame)}{estimated_note}, reason={frame.get('reason', 'selected')})"
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
    elif transcription_refused_long:
        # BEFORE the transcript-detail branch: a transcript-detail run refused
        # by the duration guard used to fall into that branch's "Whisper was
        # unavailable or failed — re-run with --detail balanced" text, which is
        # false (a key exists) and misdirects (balanced hits the same guard).
        print(
            "_No transcript: skipped by the Whisper duration guard — this source "
            "exceeds ~60 minutes of audio, which is a real API bill. Ask the user, "
            "then re-run with `--transcribe-anyway` to transcribe it all, or with "
            "`--start`/`--end` to transcribe a section._"
        )
    elif detail == "transcript":
        print(
            "_No transcript available at transcript detail. Captions were missing and Whisper was "
            "unavailable or failed, so there is no visual fallback here. Re-run with "
            "`--detail balanced` for frames._"
        )
    elif focused and dl.get("subtitle_path"):
        print(f"_No transcript lines fell inside {format_time(effective_start)} → {format_time(effective_end)}._")
    elif source_lacks_video:
        # "Proceed with frames only" is the standard advice and it is nonsense
        # here: this source has no frames either, so following it would send the
        # reader looking for images that do not exist. On an audio-only source a
        # missing transcript means the run produced nothing at all, and the
        # report should say that rather than imply a fallback.
        print(
            "_No transcript available, and this source has no video stream — so "
            f"there is nothing to report. Captions were missing and {_whisper_miss_reason(transcription_error)}_"
        )
    else:
        print(
            "_No transcript available — proceed with frames only. "
            f"Captions were missing and {_whisper_miss_reason(transcription_error)}_"
        )

    print()
    print("---")
    print(f"_Work dir: `{work}` — delete when done._")
    if video_path:
        # The resolved path, not a guessed one. yt-dlp writes `video.%(ext)s`, so
        # the extension depends on what the site served — .webm and .mkv are
        # routine — and every doc that hardcoded `download/video.mp4` for the
        # second pass broke on exactly those.
        print(f"_Video file: `{video_path}` — pass this to a second run, not the URL._")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
