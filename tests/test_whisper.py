"""Whisper auto-chunking: plan, split, and timestamp stitching."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import pytest

import whisper


MB = 1024 * 1024


class TestPlanChunks:
    def test_under_limit_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=600.0, total_bytes=5 * MB, max_bytes=24 * MB)
        assert plan == [(0.0, 600.0)]

    def test_at_limit_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=600.0, total_bytes=24 * MB, max_bytes=24 * MB)
        assert plan == [(0.0, 600.0)]

    def test_over_limit_splits_into_enough_chunks(self):
        # 71 MB against a 24 MB cap → ceil(71/24) = 3 chunks.
        plan = whisper.plan_chunks(total_seconds=3600.0, total_bytes=71 * MB, max_bytes=24 * MB)
        assert len(plan) == 3

    def test_chunks_are_contiguous_and_cover_full_duration(self):
        total = 3600.0
        plan = whisper.plan_chunks(total_seconds=total, total_bytes=71 * MB, max_bytes=24 * MB)
        # Offsets start at 0 and each picks up where the previous ended.
        assert plan[0][0] == 0.0
        for (off, dur), (next_off, _) in zip(plan, plan[1:]):
            assert math.isclose(off + dur, next_off)
        last_off, last_dur = plan[-1]
        assert math.isclose(last_off + last_dur, total)

    def test_each_chunk_estimated_under_limit(self):
        total_seconds, total_bytes, cap = 3600.0, 71 * MB, 24 * MB
        plan = whisper.plan_chunks(total_seconds, total_bytes, cap)
        bytes_per_second = total_bytes / total_seconds
        for _off, dur in plan:
            assert dur * bytes_per_second <= cap

    def test_zero_duration_is_single_chunk(self):
        plan = whisper.plan_chunks(total_seconds=0.0, total_bytes=0, max_bytes=24 * MB)
        assert plan == [(0.0, 0.0)]


class TestShiftSegments:
    def test_adds_offset_to_start_and_end(self):
        segs = [{"start": 0.0, "end": 2.5, "text": "hi"}, {"start": 2.5, "end": 4.0, "text": "there"}]
        shifted = whisper.shift_segments(segs, 1800.0)
        assert shifted == [
            {"start": 1800.0, "end": 1802.5, "text": "hi"},
            {"start": 1802.5, "end": 1804.0, "text": "there"},
        ]

    def test_zero_offset_is_identity(self):
        segs = [{"start": 1.0, "end": 2.0, "text": "x"}]
        assert whisper.shift_segments(segs, 0.0) == segs

    def test_does_not_mutate_input(self):
        segs = [{"start": 0.0, "end": 1.0, "text": "x"}]
        whisper.shift_segments(segs, 10.0)
        assert segs[0]["start"] == 0.0


def _make_mp3(path: Path, seconds: float) -> None:
    """Synthesize a mono 16k 64k mp3 of a sine tone — mirrors extract_audio's format."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-t", str(seconds), "-i", "sine=frequency=440:sample_rate=16000",
            "-acodec", "libmp3lame", "-ar", "16000", "-ac", "1", "-b:a", "64k",
            str(path),
        ],
        check=True,
    )


class TestSplitAudio:
    def test_creates_one_file_per_plan_entry(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        assert len(chunks) == 2
        for chunk_path, _offset in chunks:
            assert chunk_path.exists() and chunk_path.stat().st_size > 0

    def test_returns_plan_offsets(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        assert [offset for _path, offset in chunks] == [0.0, 3.0]

    def test_chunks_are_smaller_than_full(self, tmp_path: Path):
        full = tmp_path / "audio.mp3"
        _make_mp3(full, 6.0)
        plan = [(0.0, 3.0), (3.0, 3.0)]

        chunks = whisper.split_audio(full, tmp_path, plan)

        full_size = full.stat().st_size
        for chunk_path, _offset in chunks:
            assert chunk_path.stat().st_size < full_size


class TestAudioDuration:
    def test_reads_duration_of_synthesized_clip(self, tmp_path: Path):
        audio = tmp_path / "audio.mp3"
        _make_mp3(audio, 5.0)
        assert whisper.audio_duration(audio) == pytest.approx(5.0, abs=0.5)


class TestTranscribeChunks:
    def test_shifts_and_concatenates_each_chunk(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def fake_transcribe(path: Path) -> list[dict]:
            return [{"start": 0.0, "end": 2.0, "text": path.stem}]

        out = whisper.transcribe_chunks(chunks, fake_transcribe)

        assert out == [
            {"start": 0.0, "end": 2.0, "text": "a"},
            {"start": 100.0, "end": 102.0, "text": "b"},
        ]

    def test_keeps_successful_chunks_when_one_fails(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def flaky(path: Path) -> list[dict]:
            if path.stem == "b":
                raise SystemExit("chunk b failed")
            return [{"start": 1.0, "end": 2.0, "text": "a"}]

        out = whisper.transcribe_chunks(chunks, flaky)

        assert out == [{"start": 1.0, "end": 2.0, "text": "a"}]

    def test_raises_when_every_chunk_fails(self):
        chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0)]

        def always_fail(path: Path) -> list[dict]:
            raise SystemExit("boom")

        with pytest.raises(SystemExit):
            whisper.transcribe_chunks(chunks, always_fail)


class TestExtractAudioRange:
    """A focused run used to upload the whole video's audio.

    Measured on a 10-minute clip with --start 5:00 --end 5:20: 4689 kB over the
    wire against ~156 kB of actual window. Everything outside the window was
    transcribed, paid for, and then thrown away by filter_range.
    """

    def _argv(self, monkeypatch) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))

            class _Result:
                returncode = 0
                stdout = stderr = ""

            return _Result()

        monkeypatch.setattr(whisper.subprocess, "run", fake_run)
        monkeypatch.setattr(whisper.Path, "exists", lambda self: True)
        monkeypatch.setattr(whisper.Path, "stat", lambda self: type("S", (), {"st_size": 1})())
        # These tests are about argv construction, so the emptiness guard — which
        # really does shell out to ffprobe — is stubbed rather than exercised. It
        # has its own test below.
        monkeypatch.setattr(whisper, "audio_duration", lambda path: 1.0)
        return calls

    def test_no_range_argv_is_unchanged(self, monkeypatch, tmp_path):
        calls = self._argv(monkeypatch)
        whisper.extract_audio("v.mp4", tmp_path / "a.mp3")
        assert "-ss" not in calls[0] and "-t" not in calls[0]

    def test_start_seeks_before_the_input(self, monkeypatch, tmp_path):
        """Fast seek. After -i it would decode and discard everything first."""
        calls = self._argv(monkeypatch)
        whisper.extract_audio("v.mp4", tmp_path / "a.mp3", start_seconds=300.0)
        argv = calls[0]
        assert argv[argv.index("-ss") + 1] == "300.000"
        assert argv.index("-ss") < argv.index("-i")

    def test_range_uses_a_duration_not_an_absolute_end(self, monkeypatch, tmp_path):
        """THE trap. With -ss before -i the input clock is rebased to the seek
        point, so `-to 320` on a --start 300 run would cut at 620s of source —
        five extra minutes of the wrong material, silently."""
        calls = self._argv(monkeypatch)
        whisper.extract_audio("v.mp4", tmp_path / "a.mp3", start_seconds=300.0, end_seconds=320.0)
        argv = calls[0]
        assert "-to" not in argv
        assert argv[argv.index("-t") + 1] == "20.000"
        assert argv.index("-t") > argv.index("-i")

    def test_end_without_start_is_a_duration_from_zero(self, monkeypatch, tmp_path):
        calls = self._argv(monkeypatch)
        whisper.extract_audio("v.mp4", tmp_path / "a.mp3", end_seconds=45.0)
        argv = calls[0]
        assert "-ss" not in argv
        assert argv[argv.index("-t") + 1] == "45.000"

    def test_clipped_audio_is_proportional_to_the_window(self, tmp_path):
        """End to end through real ffmpeg, since the point is the byte count."""
        source = tmp_path / "long.mp3"
        _make_mp3(source, 60.0)
        whole = whisper.extract_audio(str(source), tmp_path / "whole.mp3")
        window = whisper.extract_audio(
            str(source), tmp_path / "window.mp3", start_seconds=20.0, end_seconds=25.0
        )
        assert whisper.audio_duration(window) == pytest.approx(5.0, abs=0.5)
        assert window.stat().st_size < whole.stat().st_size / 5


class TestTranscribeVideoRange:
    """The shift back into source time, which is what keeps filter_range from
    silently discarding the whole transcript."""

    def _stub(self, monkeypatch, tmp_path, segments, audio_bytes=1024):
        audio = tmp_path / "audio.mp3"
        audio.write_bytes(b"x" * audio_bytes)
        monkeypatch.setattr(
            whisper, "extract_audio",
            lambda video, out, start_seconds=None, end_seconds=None: audio,
        )
        monkeypatch.setattr(whisper, "_transcribe_file", lambda b, k, p: list(segments))
        return audio

    def test_segments_come_back_in_absolute_source_time(self, monkeypatch, tmp_path):
        self._stub(monkeypatch, tmp_path, [{"start": 0.0, "end": 2.0, "text": "hi"}])
        segments, _ = whisper.transcribe_video(
            "v.mp4", tmp_path / "audio.mp3", backend="groq", api_key="k",
            start_seconds=300.0, end_seconds=320.0,
        )
        assert segments == [{"start": 300.0, "end": 302.0, "text": "hi"}]

    def test_no_window_means_no_shift(self, monkeypatch, tmp_path):
        self._stub(monkeypatch, tmp_path, [{"start": 1.0, "end": 2.0, "text": "hi"}])
        segments, _ = whisper.transcribe_video(
            "v.mp4", tmp_path / "audio.mp3", backend="groq", api_key="k",
        )
        assert segments == [{"start": 1.0, "end": 2.0, "text": "hi"}]

    def test_the_shift_survives_filter_range(self, monkeypatch, tmp_path):
        """The failure this prevents, stated end to end: clip without shifting
        and every segment lands near t=0, so filter_range drops all of them and
        the report says "no transcript" for a video that has one."""
        import transcribe

        self._stub(monkeypatch, tmp_path, [{"start": 0.0, "end": 2.0, "text": "hi"}])
        segments, _ = whisper.transcribe_video(
            "v.mp4", tmp_path / "audio.mp3", backend="groq", api_key="k",
            start_seconds=300.0, end_seconds=320.0,
        )
        assert transcribe.filter_range(segments, 300.0, 320.0) == segments
        # Unshifted, the same segments vanish.
        assert transcribe.filter_range([{"start": 0.0, "end": 2.0, "text": "hi"}], 300.0, 320.0) == []

    def test_chunk_offsets_and_the_window_shift_compose(self, monkeypatch, tmp_path):
        """transcribe_chunks already shifts by each chunk's offset within the
        clip. The window shift stacks on top; it must not be applied twice."""
        audio = self._stub(
            monkeypatch, tmp_path, [{"start": 0.0, "end": 1.0, "text": "x"}],
            audio_bytes=whisper.MAX_UPLOAD_BYTES + 1,
        )
        monkeypatch.setattr(whisper, "audio_duration", lambda p: 100.0)
        monkeypatch.setattr(
            whisper, "split_audio",
            lambda full, work, plan: [(audio, offset) for offset, _dur in plan],
        )
        segments, _ = whisper.transcribe_video(
            "v.mp4", tmp_path / "audio.mp3", backend="groq", api_key="k",
            start_seconds=600.0,
        )
        assert segments[0]["start"] == 600.0
        assert all(s["start"] >= 600.0 for s in segments)
        # Two chunks at offsets 0 and 50 within the clip -> 600 and 650 absolute.
        assert [s["start"] for s in segments] == [600.0, 650.0]


def test_a_window_past_the_end_of_the_audio_is_refused(tmp_path):
    """A non-zero file size is not proof of audio.

    Measured: asking for 10-20s of a 3s clip writes a 333-byte mp3 with
    headers and no MPEG frames. The size guard passed it, ffprobe cannot find
    a duration in it, and that empty payload was uploaded to the Whisper API
    and billed.
    """
    source = tmp_path / "short.mp3"
    _make_mp3(source, 3.0)
    with pytest.raises(SystemExit, match="no audio in the requested range"):
        whisper.extract_audio(
            str(source), tmp_path / "past_end.mp3",
            start_seconds=10.0, end_seconds=20.0,
        )

def test_a_window_inside_the_audio_is_accepted(tmp_path):
    source = tmp_path / "short.mp3"
    _make_mp3(source, 3.0)
    out = whisper.extract_audio(
        str(source), tmp_path / "ok.mp3", start_seconds=1.0, end_seconds=2.0,
    )
    assert whisper.audio_duration(out) == pytest.approx(1.0, abs=0.3)


# --- Retry-After is honored only up to a ceiling --------------------------------
# Per-minute rate limits send seconds; daily-quota exhaustion sends HOURS, and
# sleeping through one meant /watch printed a single line and then blocked the
# whole session. Past the ceiling the caller gets the server's number and
# decides — a sleeping process cannot.


def _http_429(retry_after: str | None):
    import io as _io
    import urllib.error

    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "https://api.example/v1", 429, "Too Many Requests", headers, _io.BytesIO(b"")
    )


def _post_with(monkeypatch, tmp_path, exc_sequence):
    """Drive _post_whisper against a scripted sequence of urlopen outcomes."""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    calls = {"n": 0}
    sleeps: list[float] = []

    class _OK:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"segments": [], "text": "hi"}'

    def fake_urlopen(request, *a, **k):
        outcome = exc_sequence[min(calls["n"], len(exc_sequence) - 1)]
        calls["n"] += 1
        if outcome is None:
            return _OK()
        raise outcome

    monkeypatch.setattr(whisper, "urlopen", fake_urlopen)
    monkeypatch.setattr(whisper.time, "sleep", lambda s: sleeps.append(s))
    result = whisper._post_whisper("https://api.example/v1", "key", "model", audio)
    return result, sleeps


def test_huge_retry_after_fails_fast_instead_of_sleeping(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"mp3")
    sleeps: list[float] = []
    monkeypatch.setattr(whisper, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_429("7200")))
    monkeypatch.setattr(whisper.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(SystemExit) as exc:
        whisper._post_whisper("https://api.example/v1", "key", "model", audio)
    message = str(exc.value)
    assert "7200" in message
    assert "ceiling" in message
    assert "quota" in message
    assert sleeps == [], "must fail fast, not sleep through a quota window"


def test_small_retry_after_is_still_honored(monkeypatch, tmp_path):
    result, sleeps = _post_with(monkeypatch, tmp_path, [_http_429("30"), None])
    assert sleeps == [30.0]
    assert result["text"] == "hi"


def test_missing_retry_after_keeps_the_backoff_formula(monkeypatch, tmp_path):
    result, sleeps = _post_with(monkeypatch, tmp_path, [_http_429(None), None])
    # RETRY_BASE_DELAY * 2**0 + 1 for the first 429 with no header.
    assert sleeps == [whisper.RETRY_BASE_DELAY + 1]
    assert result["text"] == "hi"


def test_non_numeric_retry_after_parses_to_none():
    assert whisper._retry_after(_http_429("sixty")) is None


# --- cloud duration guard --------------------------------------------------------
# A captionless 4-hour video used to upload ~4 hours of audio with no estimate
# and no confirmation. The guard refuses BEFORE the encode with the estimate and
# the exact override flag; --motion set the house precedent that expensive runs
# block rather than warn.


def _stub_transcription(monkeypatch, tmp_path, source_seconds: float):
    audio = tmp_path / "a.mp3"
    clip = {"seconds": source_seconds}

    def fake_extract(video_path, out_path, start_seconds=None, end_seconds=None):
        # Mirror the real encoder: the mp3's duration is the window's, so the
        # post-encode guard sees the clip length, not the source length.
        if end_seconds is not None:
            clip["seconds"] = max(0.0, end_seconds - (start_seconds or 0.0))
        elif start_seconds:
            clip["seconds"] = max(0.0, source_seconds - start_seconds)
        audio.write_bytes(b"x" * 100)
        return audio

    def fake_duration(path):
        return clip["seconds"] if str(path) == str(audio) else source_seconds

    monkeypatch.setattr(whisper, "audio_duration", fake_duration)
    monkeypatch.setattr(whisper, "extract_audio", fake_extract)
    monkeypatch.setattr(
        whisper, "_transcribe_file",
        lambda backend, key, path: [{"start": 0.0, "end": 1.0, "text": "hi"}],
    )


def test_long_audio_is_refused_with_the_override_flag_named(monkeypatch, tmp_path):
    _stub_transcription(monkeypatch, tmp_path, 2 * 3600.0)
    with pytest.raises(whisper.LongAudioRefusal) as exc:
        whisper.transcribe_video("v.mp4", tmp_path / "a.mp3", backend="groq", api_key="k")
    message = str(exc.value)
    assert "120 minutes" in message
    assert "--transcribe-anyway" in message
    assert "--start" in message


def test_under_the_guard_proceeds(monkeypatch, tmp_path):
    _stub_transcription(monkeypatch, tmp_path, 59 * 60.0)
    segments, backend = whisper.transcribe_video(
        "v.mp4", tmp_path / "a.mp3", backend="groq", api_key="k"
    )
    assert backend == "groq" and segments


def test_focus_window_is_what_gets_guarded(monkeypatch, tmp_path):
    """A 4-hour source with a 2-minute window uploads 2 minutes — no refusal."""
    _stub_transcription(monkeypatch, tmp_path, 4 * 3600.0)
    segments, _ = whisper.transcribe_video(
        "v.mp4", tmp_path / "a.mp3", backend="groq", api_key="k",
        start_seconds=600.0, end_seconds=720.0,
    )
    assert segments


def test_transcribe_anyway_lifts_the_guard(monkeypatch, tmp_path):
    _stub_transcription(monkeypatch, tmp_path, 2 * 3600.0)
    segments, _ = whisper.transcribe_video(
        "v.mp4", tmp_path / "a.mp3", backend="groq", api_key="k", allow_long=True,
    )
    assert segments


def test_custom_backend_is_exempt(monkeypatch, tmp_path, capsys):
    """localhost is free per-minute; the guard would only block a local server
    doing exactly what it was set up for. An informational line still prints."""
    monkeypatch.setenv("WATCH_WHISPER_ENDPOINT", "http://127.0.0.1:9000/v1/audio/transcriptions")
    _stub_transcription(monkeypatch, tmp_path, 2 * 3600.0)
    segments, backend = whisper.transcribe_video(
        "v.mp4", tmp_path / "a.mp3", backend="custom", api_key="",
    )
    assert backend == "custom" and segments
    assert "self-hosted endpoint" in capsys.readouterr().err


def test_refusal_subclasses_systemexit():
    """Existing `except SystemExit` call sites must keep working; watch.py
    catches the subclass FIRST to report the honest reason."""
    assert issubclass(whisper.LongAudioRefusal, SystemExit)


def test_guard_holds_when_the_container_reports_no_duration(monkeypatch, tmp_path):
    """Headerless sources (piped output, OBS recordings) return duration 0 from
    the pre-encode probe, which used to bypass the guard entirely. The
    post-encode check reads the mp3's real duration and still refuses."""
    audio = tmp_path / "a.mp3"
    durations = {}

    def fake_duration(path):
        # The video container reports nothing; the encoded mp3 reports 2 hours.
        return 0.0 if str(path).endswith(".mp4") else 2 * 3600.0

    def fake_extract(video_path, out_path, start_seconds=None, end_seconds=None):
        audio.write_bytes(b"x" * 100)
        return audio

    monkeypatch.setattr(whisper, "audio_duration", fake_duration)
    monkeypatch.setattr(whisper, "extract_audio", fake_extract)
    monkeypatch.setattr(
        whisper, "_transcribe_file",
        lambda backend, key, path: [{"start": 0.0, "end": 1.0, "text": "hi"}],
    )
    with pytest.raises(whisper.LongAudioRefusal, match="--transcribe-anyway"):
        whisper.transcribe_video("v.mp4", audio, backend="groq", api_key="k")


def test_quota_exhaustion_aborts_the_whole_chunked_run():
    """A ceiling-exceeding 429 is not a per-chunk hiccup: the remaining ~24MB
    chunks must NOT be posted to an endpoint that just said 'hours'."""
    chunks = [(Path("a.mp3"), 0.0), (Path("b.mp3"), 100.0), (Path("c.mp3"), 200.0)]
    attempted: list[str] = []

    def transcribe_one(path: Path) -> list[dict]:
        attempted.append(path.stem)
        raise whisper.QuotaExhausted("server asked for 7200s")

    with pytest.raises(whisper.QuotaExhausted):
        whisper.transcribe_chunks(chunks, transcribe_one)
    assert attempted == ["a"], "remaining chunks were still uploaded"
