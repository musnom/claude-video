"""WebVTT parsing: rolling-window collapse, manual-subtitle passthrough, and
timestamp formatting.

The fixtures here are structurally real rather than hand-approximated — the
rolling ones are built by _rolling() in YouTube's actual painted-on shape (a
content cue carrying the settled top line plus a word-tagged bottom line, then
a 10ms settle cue), and REAL_AUTO_VTT is a verbatim excerpt of a downloaded
auto-caption track. A simplified fixture would pass while the real format still
broke.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import frames
import transcribe

# --- helpers -----------------------------------------------------------------


def _write_vtt(
    tmp_path: Path, body: str, name: str = "captions.vtt", dedent: bool = True
) -> str:
    """Write a VTT fixture and return its path.

    ``dedent=False`` for fixtures whose exact whitespace matters:
    textwrap.dedent normalizes whitespace-only lines to empty, which would
    delete YouTube's " " padding lines — the very thing REAL_AUTO_VTT and
    _rolling() exist to reproduce, and the difference between a cue block that
    ends where WebVTT says it does and one that ends early.
    """
    path = tmp_path / name
    path.write_text(
        textwrap.dedent(body).lstrip("\n") if dedent else body, encoding="utf-8"
    )
    return str(path)


def _parse(tmp_path: Path, body: str, dedent: bool = True) -> list[dict]:
    return transcribe.parse_vtt(_write_vtt(tmp_path, body, dedent=dedent))


def _parse_raw(tmp_path: Path, body: str) -> list[dict]:
    return _parse(tmp_path, body, dedent=False)


def _texts(segments: list[dict]) -> list[str]:
    return [seg["text"] for seg in segments]


def _words(segments: list[dict]) -> int:
    return sum(len(seg["text"].split()) for seg in segments)


def _stamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _rolling(lines: list[str], t0: float = 1.0, step: float = 2.0) -> str:
    """Build a track in YouTube's real two-line painted-on shape.

    Each spoken line appears three times, exactly as the renderer emits it:
    painted into the bottom of a content cue, restated alone by a 10ms settle
    cue, then carried as the top line of the next content cue.
    """
    out = ["WEBVTT", "Kind: captions", "Language: en", ""]
    t = t0
    for i, line in enumerate(lines):
        top = lines[i - 1] if i > 0 else None
        words = line.split()
        painted = "".join(
            f"<{_stamp(t + 0.2 * j)}><c> {w}</c>" for j, w in enumerate(words[1:], 1)
        )
        out.append(f"{_stamp(t)} --> {_stamp(t + step)} align:start position:0%")
        if top:
            out.append(top)
        out += [f"{words[0]}{painted}", ""]
        out += [
            f"{_stamp(t + step)} --> {_stamp(t + step + 0.01)} align:start position:0%",
            line,
            " ",
            "",
        ]
        t += step + 0.01
    return "\n".join(out)


# Verbatim from a downloaded 3Blue1Brown auto-caption track, padding lines and
# word tags preserved.
REAL_AUTO_VTT = (
    "WEBVTT\n"
    "Kind: captions\n"
    "Language: en\n"
    "\n"
    "00:00:00.000 --> 00:00:04.390 align:start position:0%\n"
    " \n"
    "[Music]\n"
    "\n"
    "00:00:04.390 --> 00:00:04.400 align:start position:0%\n"
    " \n"
    " \n"
    "\n"
    "00:00:04.400 --> 00:00:06.869 align:start position:0%\n"
    " \n"
    "This<00:00:04.799><c> is</c><00:00:04.960><c> a</c><00:00:05.200><c> three.</c>"
    "<00:00:05.920><c> It's</c><00:00:06.080><c> sloppily</c><00:00:06.640><c> written</c>\n"
    "\n"
    "00:00:06.869 --> 00:00:06.879 align:start position:0%\n"
    "This is a three. It's sloppily written\n"
    " \n"
    "\n"
    "00:00:06.879 --> 00:00:08.549 align:start position:0%\n"
    "This is a three. It's sloppily written\n"
    "and<00:00:07.120><c> rendered</c><00:00:07.440><c> at</c><00:00:07.680><c> an</c>"
    "<00:00:07.839><c> extremely</c><00:00:08.320><c> low</c>\n"
    "\n"
    "00:00:08.549 --> 00:00:08.559 align:start position:0%\n"
    "and rendered at an extremely low\n"
    " \n"
    "\n"
)

# 21 words. The last two lines share "out of the building", and the second line
# repeats "now" four times — both are speech, not caption artifacts.
REPEATED_SPEECH = [
    "we need to go now",
    "now now now now",
    "everybody out of the building",
    "out of the building right this second",
]


# --- real rolling structure ---------------------------------------------------


def test_rolling_window_emits_each_line_once(tmp_path):
    segments = _parse_raw(tmp_path, REAL_AUTO_VTT)
    joined = " ".join(_texts(segments))
    for line in (
        "This is a three.",
        "It's sloppily written",
        "and rendered at an extremely low",
    ):
        assert joined.count(line) == 1, f"{line!r} repeated in: {joined!r}"


def test_rolling_window_reconstructs_the_sentence(tmp_path):
    segments = _parse_raw(tmp_path, REAL_AUTO_VTT)
    assert " ".join(_texts(segments)) == (
        "[Music] This is a three. It's sloppily written "
        "and rendered at an extremely low"
    )


def test_rolling_timestamps_stay_at_cue_boundaries(tmp_path):
    """filter_range depends on tight per-cue windows for --start/--end."""
    segments = _parse_raw(tmp_path, REAL_AUTO_VTT)
    starts = [seg["start"] for seg in segments]
    assert starts == sorted(starts)
    assert 4.4 in starts, "the painted cue's own start must survive"
    for seg in segments:
        assert seg["end"] - seg["start"] < 5.0, "segments must not be merged into blocks"


def test_settle_cues_emit_nothing(tmp_path):
    segments = _parse_raw(tmp_path, REAL_AUTO_VTT)
    assert not [s for s in segments if 0 < (s["end"] - s["start"]) <= 0.02]


def test_three_line_rolling_window(tmp_path):
    """A 1-back lookback would re-emit here; the ordered-suffix match does not."""
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        line one here
        line two here
        line three here

        00:00:03.010 --> 00:00:05.000
        line two here
        line three here
        line four here

        00:00:05.010 --> 00:00:07.000
        line three here
        line four here
        line five here

        00:00:07.010 --> 00:00:09.000
        line four here
        line five here
        line six here
    """
    joined = " ".join(_texts(_parse(tmp_path, body)))
    for n in ("one", "two", "three", "four", "five", "six"):
        assert joined.count(f"line {n} here") == 1, f"'{n}' repeated in {joined!r}"


def test_settle_cue_carrying_both_window_lines(tmp_path):
    """Defensive: a renderer whose settle cue restates the whole window."""
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        so today we're going to talk about

        00:00:03.010 --> 00:00:05.000
        so today we're going to talk about
        the new release

        00:00:05.010 --> 00:00:05.020
        so today we're going to talk about
        the new release

        00:00:05.030 --> 00:00:07.000
        the new release
        and what changed
    """
    joined = " ".join(_texts(_parse(tmp_path, body)))
    assert joined.count("so today we're going to talk about") == 1
    assert joined.count("the new release") == 1


def test_non_english_rolling_track(tmp_path):
    """The rule compares whole lines, so it is language-agnostic."""
    body = _rolling([
        "Este es su noticiero digital de",
        "Telemundo 52 con las noticias mas",
        "importantes de la tarde",
    ])
    joined = " ".join(_texts(_parse(tmp_path, body)))
    assert joined.count("Telemundo 52 con las noticias mas") == 1
    assert "importantes de la tarde" in joined


# --- genuine repetition must survive ------------------------------------------
# Three upstream attempts at the rolling fix were rejected because they delete
# real speech: word-overlap heuristics cannot tell "the caption redrew this"
# from "the speaker said it twice".


def test_repeated_speech_survives_in_plain_cues(tmp_path):
    body = ["WEBVTT", ""]
    t = 1.0
    for line in REPEATED_SPEECH:
        body += [f"{_stamp(t)} --> {_stamp(t + 2)}", line, ""]
        t += 2.01
    segments = transcribe.parse_vtt(_write_vtt(tmp_path, "\n".join(body), dedent=False))
    assert _words(segments) == 21
    assert _texts(segments) == REPEATED_SPEECH


def test_repeated_speech_survives_inside_a_rolling_window(tmp_path):
    """The load-bearing regression test for this whole change."""
    segments = _parse_raw(tmp_path, _rolling(REPEATED_SPEECH))
    assert _words(segments) == 21, f"lost speech: {_texts(segments)}"
    assert _texts(segments) == REPEATED_SPEECH


def test_consecutive_identical_cues_survive(tmp_path):
    """A speaker reading numbers aloud: "Four." then "Four." again."""
    body = """
        WEBVTT

        00:03:08.837 --> 00:03:10.038
        Four.

        00:03:10.038 --> 00:03:11.204
        Four.

        00:03:11.204 --> 00:03:14.094
        24. Staggering!
    """
    assert _texts(_parse(tmp_path, body)).count("Four.") == 2


def test_repeated_lyric_line_survives(tmp_path):
    body = """
        WEBVTT

        00:02:01.000 --> 00:02:03.000
        ♪ (Ooh, give you up) ♪

        00:02:03.000 --> 00:02:05.000
        ♪ (Ooh, give you up) ♪
    """
    assert len(_parse(tmp_path, body)) == 2


# --- manual subtitles take the untouched path ---------------------------------


def test_manual_two_line_cue_joins_with_space(tmp_path):
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:04.000
        A few years ago,
        I broke into my own house.
    """
    assert _texts(_parse(tmp_path, body)) == [
        "A few years ago, I broke into my own house."
    ]


def test_manual_track_is_one_segment_per_cue(tmp_path):
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        first thing said

        00:00:03.000 --> 00:00:05.000
        second thing said

        00:00:05.000 --> 00:00:07.000
        third thing said

        00:00:07.000 --> 00:00:09.000
        fourth thing said
    """
    assert len(_parse(tmp_path, body)) == 4


def test_manual_track_is_not_detected_as_rolling(tmp_path):
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        alpha line

        00:00:03.000 --> 00:00:05.000
        beta line

        00:00:05.000 --> 00:00:07.000
        gamma line

        00:00:07.000 --> 00:00:09.000
        delta line
    """
    path = _write_vtt(tmp_path, body)
    assert transcribe._looks_rolling(transcribe._read_cues(path)) is False


def test_auto_track_is_detected_as_rolling(tmp_path):
    path = _write_vtt(tmp_path, REAL_AUTO_VTT, dedent=False)
    assert transcribe._looks_rolling(transcribe._read_cues(path)) is True


def test_short_track_is_never_rolling(tmp_path):
    """Too few cues to call it, even if the lines do overlap."""
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        same line

        00:00:03.000 --> 00:00:05.000
        same line
    """
    path = _write_vtt(tmp_path, body)
    assert transcribe._looks_rolling(transcribe._read_cues(path)) is False


# --- entities and tags --------------------------------------------------------


def test_html_entities_are_unescaped(tmp_path):
    body = """
        WEBVTT

        00:07:07.490 --> 00:07:13.609
        Finally, we need lots of advanced R&amp;D
    """
    assert _texts(_parse(tmp_path, body)) == [
        "Finally, we need lots of advanced R&D"
    ]


def test_escaped_angle_brackets_survive_tag_stripping(tmp_path):
    """Unescape must run after tag-stripping, or this becomes a tag and vanishes."""
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        use &lt;div&gt; here
    """
    assert _texts(_parse(tmp_path, body)) == ["use <div> here"]


def test_voice_and_style_tags_are_stripped(tmp_path):
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000
        <v Roger>Hello</v> <i>there</i>
    """
    assert _texts(_parse(tmp_path, body)) == ["Hello there"]


# --- format_transcript --------------------------------------------------------


def test_stamp_matches_frames_format_time():
    """Pins the transcript clock to the frame clock permanently."""
    for t in [0, 0.4, 0.5, 0.6, 59.4, 59.5, 59.7, 60, 125, 3599.5, 3600, 3700, 7199.6, 86399]:
        rendered = transcribe.format_transcript([{"start": t, "end": t, "text": "x"}])
        assert rendered == f"[{frames.format_time(t)}] x", f"drift at t={t}"


def test_hour_rollover():
    rendered = transcribe.format_transcript([{"start": 3700, "end": 3705, "text": "hi"}])
    assert rendered == "[1:01:40] hi"
    assert "[61:40]" not in rendered


def test_sub_hour_is_two_part():
    assert transcribe.format_transcript(
        [{"start": 125, "end": 130, "text": "hi"}]
    ) == "[02:05] hi"


def test_empty_segments_render_empty_string():
    assert transcribe.format_transcript([]) == ""


# --- filter_range -------------------------------------------------------------

SEGS = [
    {"start": 0.0, "end": 5.0, "text": "a"},
    {"start": 5.0, "end": 10.0, "text": "b"},
    {"start": 10.0, "end": 15.0, "text": "c"},
]


def test_filter_range_none_is_identity():
    assert transcribe.filter_range(SEGS, None, None) == SEGS


def test_filter_range_includes_overlapping_segments():
    """A segment straddling the boundary is still in range."""
    assert _texts(transcribe.filter_range(SEGS, 4.0, 6.0)) == ["a", "b"]


def test_filter_range_open_ended():
    assert _texts(transcribe.filter_range(SEGS, 9.0, None)) == ["b", "c"]
    assert _texts(transcribe.filter_range(SEGS, None, 6.0)) == ["a", "b"]


# --- degenerate input ---------------------------------------------------------
# Note: TS_RE requires the 3-part HH:MM:SS.mmm form and rejects the WebVTT-legal
# 2-part MM:SS.mmm. yt-dlp always writes 3-part, so no real input hits it;
# widening the regex is a separate change.


def test_empty_file(tmp_path):
    assert _parse(tmp_path, "") == []


def test_header_only(tmp_path):
    assert _parse(tmp_path, "WEBVTT\n\n") == []


def test_cue_with_no_text(tmp_path):
    body = """
        WEBVTT

        00:00:01.000 --> 00:00:03.000

        00:00:03.000 --> 00:00:05.000
        real text
    """
    assert _texts(_parse(tmp_path, body)) == ["real text"]


def test_crlf_line_endings(tmp_path):
    path = tmp_path / "crlf.vtt"
    path.write_bytes(b"WEBVTT\r\n\r\n00:00:01.000 --> 00:00:03.000\r\nhello there\r\n")
    assert _texts(transcribe.parse_vtt(str(path))) == ["hello there"]


def test_comma_millisecond_separator(tmp_path):
    body = """
        WEBVTT

        00:00:01,000 --> 00:00:03,000
        srt style
    """
    assert _texts(_parse(tmp_path, body)) == ["srt style"]


def test_cue_identifier_lines_are_ignored(tmp_path):
    body = """
        WEBVTT

        1
        00:00:01.000 --> 00:00:03.000
        numbered cue
    """
    assert _texts(_parse(tmp_path, body)) == ["numbered cue"]
