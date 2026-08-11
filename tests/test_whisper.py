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
