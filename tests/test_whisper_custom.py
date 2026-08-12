"""Self-hosted / local transcription via an OpenAI-compatible endpoint.

Driven against a real stub HTTP server on 127.0.0.1 rather than a mocked
urlopen, so the whole path is exercised: audio extraction, multipart framing,
the request headers actually sent, verbose_json parsing, and segment stitching.
No API key and no network egress.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import setup as setup_mod
import whisper

RESPONSE = {
    "text": "hello from localhost",
    "segments": [
        {"start": 0.0, "end": 1.5, "text": "hello from"},
        {"start": 1.5, "end": 3.0, "text": " localhost"},
    ],
}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type", ""),
            "user_agent": self.headers.get("User-Agent"),
            "body_len": len(body),
            "model": _field(body, "model"),
            "response_format": _field(body, "response_format"),
        })
        payload = json.dumps(RESPONSE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep pytest output clean
        pass


def _field(body: bytes, name: str) -> str | None:
    """Pull a multipart form field value out of the raw request body."""
    marker = f'name="{name}"'.encode()
    idx = body.find(marker)
    if idx == -1:
        return None
    start = body.find(b"\r\n\r\n", idx) + 4
    end = body.find(b"\r\n", start)
    return body[start:end].decode()


@pytest.fixture
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    server.url = f"http://{host}:{port}/v1/audio/transcriptions"
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def custom_env(monkeypatch, stub_server):
    monkeypatch.setenv(whisper.CUSTOM_ENDPOINT_VAR, stub_server.url)
    monkeypatch.delenv(whisper.CUSTOM_MODEL_VAR, raising=False)
    return stub_server


# --- backend resolution -------------------------------------------------------


def test_custom_endpoint_is_detected(custom_env):
    assert whisper.load_api_key() == ("custom", "")


def test_custom_wins_over_cloud_keys(custom_env, monkeypatch):
    """Someone who pointed this at localhost meant it."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real")
    assert whisper.load_api_key()[0] == "custom"


def test_explicit_backend_still_wins(custom_env, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real")
    assert whisper.load_api_key("groq") == ("groq", "gsk_real")


def test_no_endpoint_falls_back_to_cloud(monkeypatch):
    monkeypatch.delenv(whisper.CUSTOM_ENDPOINT_VAR, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_real")
    assert whisper.load_api_key() == ("groq", "gsk_real")


def test_forcing_custom_without_an_endpoint_is_an_error(monkeypatch, tmp_path):
    monkeypatch.delenv(whisper.CUSTOM_ENDPOINT_VAR, raising=False)
    assert whisper.load_api_key("custom") == (None, None)


# --- the request actually sent ------------------------------------------------


def _make_clip(tmp_path: Path) -> Path:
    """A clip with an actual audio track.

    conftest's cut_clip is built with `a=0` — video only — so extract_audio has
    nothing to work with. A sine tone is enough; the stub returns a canned
    transcript regardless of what it hears.
    """
    import subprocess

    clip = tmp_path / "clip.mp4"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-t", "2", "-i", "color=c=blue:s=160x120:r=10",
            "-f", "lavfi", "-t", "2", "-i", "sine=frequency=440:sample_rate=44100",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(clip),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return clip


def test_end_to_end_against_a_local_server(custom_env, tmp_path):
    segments, backend = whisper.transcribe_video(
        str(_make_clip(tmp_path)), tmp_path / "audio.mp3"
    )
    assert backend == "custom"
    assert [s["text"] for s in segments] == ["hello from", "localhost"]
    assert custom_env.seen, "the stub server was never called"


def test_no_authorization_header_when_keyless(custom_env, tmp_path):
    """A placeholder bearer token is the kind of thing that gets logged."""
    whisper.transcribe_video(str(_make_clip(tmp_path)), tmp_path / "audio.mp3")
    assert custom_env.seen[0]["auth"] is None


def test_request_shape_is_openai_compatible(custom_env, tmp_path):
    whisper.transcribe_video(str(_make_clip(tmp_path)), tmp_path / "audio.mp3")
    seen = custom_env.seen[0]
    assert seen["path"] == "/v1/audio/transcriptions"
    assert seen["content_type"].startswith("multipart/form-data; boundary=")
    assert seen["response_format"] == "verbose_json"
    assert seen["model"] == whisper.CUSTOM_MODEL_DEFAULT
    assert seen["body_len"] > 1000, "audio should actually be attached"


def test_model_is_overridable(custom_env, tmp_path, monkeypatch):
    monkeypatch.setenv(whisper.CUSTOM_MODEL_VAR, "ggml-large-v3")
    whisper.transcribe_video(str(_make_clip(tmp_path)), tmp_path / "audio.mp3")
    assert custom_env.seen[0]["model"] == "ggml-large-v3"


def test_cloud_backends_still_send_authorization(monkeypatch):
    """Regression guard on the header now being conditional."""
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(RESPONSE).encode()

    def fake_urlopen(request, *a, **k):
        captured.update(request.headers)
        return _Resp()

    monkeypatch.setattr(whisper, "urlopen", fake_urlopen)
    whisper._post_whisper(whisper.GROQ_ENDPOINT, "gsk_real", "m", Path(__file__))
    assert captured.get("Authorization") == "Bearer gsk_real"


# --- config plumbing ----------------------------------------------------------


def test_endpoint_is_read_from_the_config_file(tmp_path, monkeypatch):
    """And inline comments are stripped — a private parser missed that."""
    home = tmp_path / "home"
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text(
        "WATCH_WHISPER_ENDPOINT=http://127.0.0.1:9000/v1/audio/transcriptions  # local box\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv(whisper.CUSTOM_ENDPOINT_VAR, raising=False)
    assert whisper.custom_endpoint() == "http://127.0.0.1:9000/v1/audio/transcriptions"


def test_setup_treats_a_custom_endpoint_as_configured(tmp_path, monkeypatch):
    """Otherwise a self-hosted user is nagged for a cloud key on every run.

    Isolated via HOME rather than by monkeypatching setup's CONFIG_FILE: setup
    no longer has a private read path — it resolves through config.read_setting
    like whisper.py, which is the whole point of the shared resolver.
    """
    home = tmp_path / "home"
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True)
    (cfg / ".env").write_text(
        "WATCH_WHISPER_ENDPOINT=http://127.0.0.1:9000/v1/audio/transcriptions\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WATCH_WHISPER_ENDPOINT", raising=False)
    assert setup_mod._have_api_key() == (True, "custom")
