#!/usr/bin/env python3
"""Parse a WebVTT subtitle file into a clean, timestamped transcript.

YouTube's auto-generated captions are *painted on* into a rolling two-line
window, so the file repeats each spoken line rather than stating it once. A
content cue carries the settled top line plus a newly-painted bottom line, then
a 10ms "settle" cue restates the new line alone, then the next content cue
carries it again as its top line. Every spoken line therefore appears three
times, and a naive read inflates the transcript ~2x with text the speaker said
once (measured across five real auto tracks: 1.96x-2.00x).

We collapse that back down by comparing whole physical *lines*, and only when
the track actually looks rolling. Both halves matter. Whole-line equality is
what distinguishes a caption redraw from a speaker repeating themselves — the
partial word-overlap heuristics that look tempting here silently delete real
speech ("now now now now", a restated phrase, a lyric refrain). And the
rolling-mode gate keeps hand-authored subtitles on a byte-identical path
instead of relying on the collapse to no-op by luck.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

from frames import format_time  # one clock shared with the frame labels

TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[.,](\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2})[.,](\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")

# Widest caption window we look back over. Every real track observed — English
# and Spanish, auto and manual — is exactly 2 lines tall; the cap only bounds a
# pathological file.
MAX_WINDOW_LINES = 4
# Below this many cues there is not enough signal to call a track rolling.
ROLLING_MIN_CUES = 4
# Fraction of adjacent cue pairs that must share a line for the track to count
# as rolling. Measured separation is a ~55x gap: auto tracks score 0.956-0.998,
# hand-authored ones 0.000-0.017, so anything mid-range is safe.
ROLLING_OVERLAP_RATIO = 0.5


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _clean_line(raw: str) -> str:
    """Strip WebVTT markup, then decode entities.

    Order is load-bearing: unescaping first would turn a literal ``&lt;i&gt;``
    into ``<i>``, which TAG_RE would then eat as markup.
    """
    return html.unescape(TAG_RE.sub("", raw)).strip()


def _read_cues(path: str) -> list[dict]:
    """One record per VTT cue: ``{"start", "end", "lines"}``.

    ``lines`` holds the cue's physical lines, cleaned; blank padding lines (the
    bare ``' '`` YouTube emits) drop out here.
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    cues: list[dict] = []
    i = 0
    while i < len(lines):
        match = TS_RE.match(lines[i])
        if not match:
            i += 1
            continue

        start = _to_seconds(*match.groups()[:4])
        end = _to_seconds(*match.groups()[4:])
        i += 1

        # A cue block ends at a genuinely empty line. Testing .strip() instead
        # would end it at YouTube's whitespace-only padding line, which it emits
        # *before* the painted text — so the entire painted cue was dropped and
        # its text got attributed to the 10ms settle cue that follows, landing
        # every line ~2s later than it was spoken.
        body: list[str] = []
        while i < len(lines) and lines[i] != "":
            cleaned = _clean_line(lines[i])
            if cleaned:
                body.append(cleaned)
            i += 1

        cues.append({"start": round(start, 2), "end": round(end, 2), "lines": body})
        i += 1

    return cues


def _window_height(cues: list[dict]) -> int:
    """How many lines the caption window holds, bounded by MAX_WINDOW_LINES."""
    return min(max((len(c["lines"]) for c in cues), default=1) or 1, MAX_WINDOW_LINES)


def _overlap_len(tail: list[str], body: list[str], height: int) -> int:
    """Longest k where the last k of ``tail`` equal the first k of ``body``.

    An *ordered contiguous* match, which is what separates a scrolled window
    from coincidence. Set membership over the same lines would delete the second
    ``Four.`` in ``Four. / 24. Staggering! / Four.``
    """
    for n in range(min(len(body), len(tail), height), 0, -1):
        if tail[-n:] == body[:n]:
            return n
    return 0


def _looks_rolling(cues: list[dict]) -> bool:
    """True when most adjacent cue pairs share a scrolled run of whole lines.

    This is the gate that keeps hand-authored subtitles on the untouched path.
    It uses the same overlap predicate as the collapse rather than just
    comparing one line to one line: a two-line window scrolls so that the shared
    run is a single line, but a three-line window shares two, and a
    first-line-to-last-line test misses that entirely and would send a genuinely
    rolling track down the passthrough path.

    Measured separation on real tracks is a ~55x gap — auto 0.956-0.998,
    hand-authored 0.000-0.017 — so the 0.5 threshold sits in open space.
    """
    if len(cues) < ROLLING_MIN_CUES:
        return False
    height = _window_height(cues)
    pairs = overlaps = 0
    for prev, cur in zip(cues, cues[1:]):
        if not prev["lines"] or not cur["lines"]:
            continue
        pairs += 1
        if _overlap_len(prev["lines"], cur["lines"], height):
            overlaps += 1
    return pairs > 0 and overlaps >= ROLLING_OVERLAP_RATIO * pairs


def _collapse_rolling(cues: list[dict]) -> list[dict]:
    """Drop the scrolled-up prefix of each cue, keeping one segment per cue.

    For each cue we find the longest run of leading lines that matches the last
    lines we emitted **in order**, and drop exactly that. An ordered contiguous
    match is what makes this safe: set membership over the same window would
    delete the second ``Four.`` in ``Four. / 24. Staggering! / Four.``

    Segment boundaries stay at the original cue boundaries so ``filter_range``
    keeps its precision for ``--start``/``--end``.
    """
    height = _window_height(cues)
    tail: list[str] = []  # the last `height` lines actually emitted
    out: list[dict] = []
    for cue in cues:
        body = cue["lines"]
        fresh = body[_overlap_len(tail, body, height):]
        if not fresh:  # a pure settle cue contributes nothing
            continue
        tail = (tail + fresh)[-height:]
        out.append({"start": cue["start"], "end": cue["end"], "text": " ".join(fresh)})
    return out


def parse_vtt(path: str) -> list[dict]:
    cues = _read_cues(path)
    if _looks_rolling(cues):
        return _collapse_rolling(cues)
    return [
        {"start": c["start"], "end": c["end"], "text": " ".join(c["lines"])}
        for c in cues
        if c["lines"]
    ]


def filter_range(
    segments: list[dict],
    start_seconds: float | None,
    end_seconds: float | None,
) -> list[dict]:
    """Return segments whose time range overlaps [start, end]."""
    if start_seconds is None and end_seconds is None:
        return segments
    lo = start_seconds if start_seconds is not None else float("-inf")
    hi = end_seconds if end_seconds is not None else float("inf")
    return [seg for seg in segments if seg["end"] >= lo and seg["start"] <= hi]


def format_transcript(segments: list[dict]) -> str:
    """Render segments as ``[stamp] text`` lines.

    The stamp comes from ``frames.format_time`` rather than a local formatter:
    the model cross-references these lines against the ``t=`` labels on the
    frames, and two independently-maintained formatters is exactly how those
    clocks drifted apart (this used to print ``[61:40]`` where a frame at the
    same instant read ``1:01:40``).
    """
    return "\n".join(f"[{format_time(seg['start'])}] {seg['text']}" for seg in segments)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: transcribe.py <vtt-path>", file=sys.stderr)
        raise SystemExit(2)
    print(format_transcript(parse_vtt(sys.argv[1])))
