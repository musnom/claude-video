"""setup.py --json surfaces the resolved watch detail."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SETUP = Path(__file__).resolve().parent.parent / "skills" / "watch" / "scripts" / "setup.py"


def _run(args, *, home=None, extra_env=None):
    env = dict(os.environ)
    env.pop("WATCH_DETAIL", None)
    # Don't let a real key in the developer's shell env leak into the test.
    env.pop("GROQ_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    env.pop("SETUP_COMPLETE", None)
    if home is not None:
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SETUP), *args],
        capture_output=True, text=True, env=env,
    )


def _write_env(home: Path, body: str) -> None:
    cfg = home / ".config" / "watch"
    cfg.mkdir(parents=True, exist_ok=True)
    f = cfg / ".env"
    f.write_text(body, encoding="utf-8")
    f.chmod(0o600)


def test_json_reports_watch_detail():
    proc = _run(["--json"])
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["watch_detail"] == "balanced"


def test_keyless_completed_setup_proceeds_silently(tmp_path):
    """A user who finished setup without a key must NOT be nagged forever."""
    _write_env(tmp_path, "GROQ_API_KEY=\nOPENAI_API_KEY=\nSETUP_COMPLETE=true\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 0, f"keyless-complete should pass --check; got {chk.returncode}: {chk.stderr}"
    assert chk.stdout == "" and chk.stderr == ""

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["can_proceed"] is True
    assert js["first_run"] is False
    assert js["setup_complete"] is True
    # status still encourages a key even though we can proceed
    assert js["status"] == "needs_key"


def test_keyless_first_run_is_encouraged(tmp_path):
    """Genuine first run with no key: --check reports exit 3 (encourage a key)."""
    _write_env(tmp_path, "GROQ_API_KEY=\nOPENAI_API_KEY=\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 3, chk.stderr

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["can_proceed"] is False
    assert js["first_run"] is True


def test_key_present_is_ready(tmp_path):
    _write_env(tmp_path, "GROQ_API_KEY=sk-test-abc\n")
    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 0, chk.stderr

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["status"] == "ready"
    assert js["can_proceed"] is True
    assert js["whisper_backend"] == "groq"


# --- shared .env resolution (config.read_setting) -------------------------------
# whisper.py always honored a project-local ./.env; setup did not, so a user
# whose GROQ_API_KEY lives in the project transcribed fine while --check said
# needs_key and the hook nagged every session.


def test_project_local_key_counts(tmp_path):
    """A GROQ_API_KEY in ./.env makes --check pass and --json name the backend."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("GROQ_API_KEY=sk-local-abc\n", encoding="utf-8")
    env = dict(os.environ)
    for name in ("WATCH_DETAIL", "GROQ_API_KEY", "OPENAI_API_KEY", "SETUP_COMPLETE"):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    chk = subprocess.run(
        [sys.executable, str(SETUP), "--check"],
        capture_output=True, text=True, env=env, cwd=project,
    )
    assert chk.returncode == 0, chk.stderr
    js = json.loads(subprocess.run(
        [sys.executable, str(SETUP), "--json"],
        capture_output=True, text=True, env=env, cwd=project,
    ).stdout)
    assert js["has_api_key"] is True
    assert js["whisper_backend"] == "groq"


def test_setup_complete_is_never_read_from_the_project_env(tmp_path):
    """A cloned repo shipping SETUP_COMPLETE=true must not suppress another
    user's first-run flow — the marker is about the machine, not the project."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SETUP_COMPLETE=true\n", encoding="utf-8")
    env = dict(os.environ)
    for name in ("WATCH_DETAIL", "GROQ_API_KEY", "OPENAI_API_KEY", "SETUP_COMPLETE"):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    js = json.loads(subprocess.run(
        [sys.executable, str(SETUP), "--json"],
        capture_output=True, text=True, env=env, cwd=project,
    ).stdout)
    assert js["first_run"] is True
    assert js["setup_complete"] is False


# --- setup.py --complete: the keyless decision gets recorded --------------------
# The installer only wrote SETUP_COMPLETE when a key was present, so a user who
# declined Whisper had no tool-supported way to stop the nag — SKILL.md's
# "never nagged again" promise depended on the model hand-editing the file.


def test_complete_records_the_keyless_decision(tmp_path):
    first = _run(["--complete"], home=tmp_path)
    assert first.returncode == 0, first.stderr
    assert "setup complete" in first.stdout

    chk = _run(["--check"], home=tmp_path)
    assert chk.returncode == 0, f"--check must pass after --complete: {chk.stderr}"
    assert chk.stdout == "" and chk.stderr == ""

    js = json.loads(_run(["--json"], home=tmp_path).stdout)
    assert js["setup_complete"] is True
    assert js["first_run"] is False
    assert js["can_proceed"] is True


def test_complete_is_idempotent(tmp_path):
    _run(["--complete"], home=tmp_path)
    _run(["--complete"], home=tmp_path)
    body = (tmp_path / ".config" / "watch" / ".env").read_text(encoding="utf-8")
    assert body.count("SETUP_COMPLETE=true") == 1
