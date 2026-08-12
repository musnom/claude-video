"""Release-level invariants: version sync, archive contents, doc-vs-code drift.

These test what actually ships. The archive tests read HEAD — in release CI,
HEAD is the tag commit, which is exactly the tree the bundle is built from.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

import config

REPO = Path(__file__).resolve().parent.parent
SKILL_MD = REPO / "skills" / "watch" / "SKILL.md"

git_repo = pytest.mark.skipif(
    not (REPO / ".git").exists(), reason="needs the git repository (archive tests read HEAD)"
)


def _frontmatter(field: str) -> str:
    match = re.search(rf"^{field}:\s*(.+)$", SKILL_MD.read_text(encoding="utf-8"), re.M)
    assert match, f"SKILL.md frontmatter has no {field}:"
    return match.group(1).strip().strip('"')


def _archive_names(treeish: str) -> list[str]:
    data = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", treeish],
        capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return tar.getnames()


# --- version sync ---------------------------------------------------------------


def test_version_is_synced_across_the_manifest_trio():
    """The AGENTS.md release rule, as a test: SKILL.md frontmatter and both
    plugin manifests must carry the same version or an install surface lies."""
    skill_version = _frontmatter("version")
    claude = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert skill_version == claude["version"] == codex["version"]


# --- what the archives ship ------------------------------------------------------


@git_repo
def test_skill_bundle_ships_only_runtime_files():
    """The claude.ai .skill bundle is `git archive HEAD:skills/watch`. Subtree
    archives resolve export-ignore relative to the SUBTREE, so the repo-root
    .gitattributes never matched and the bundle shipped build-skill.sh and
    .skillignore for two releases — this pins the fix (skills/watch/.gitattributes)."""
    names = [n for n in _archive_names("HEAD:skills/watch") if not n.endswith("/")]
    assert names.count("SKILL.md") == 1
    forbidden = {"scripts/build-skill.sh", ".skillignore", ".gitattributes"}
    assert not (forbidden & set(names)), sorted(forbidden & set(names))
    assert "references/setup.md" in names
    assert "references/motion.md" in names
    assert "scripts/watch.py" in names
    assert len(names) <= 200, "build-skill.sh's bundle cap"


@git_repo
def test_full_repo_archive_top_level_is_an_allowlist():
    """What /plugin install fetches. An allowlist catches the NEXT stray dev
    doc structurally — a tracked planning doc shipped to every install once
    because three exclusion lists drifted."""
    top = {name.split("/", 1)[0] for name in _archive_names("HEAD")}
    assert top == {
        ".claude-plugin", ".codex-plugin", ".skillignore",
        "AGENTS.md", "CHANGELOG.md", "CLAUDE.md", "LICENSE", "README.md",
        "hooks", "requirements-dev.txt", "skills",
    }, sorted(top)


# --- doc-vs-code drift ------------------------------------------------------------


def test_skill_md_caps_match_config():
    text = SKILL_MD.read_text(encoding="utf-8")
    assert config.frame_cap("efficient") == 50
    assert config.frame_cap("balanced") == 100
    assert re.search(r"`efficient` → up to \*\*50\*\*", text)
    assert re.search(r"`balanced` \(default\) → up to \*\*100\*\*", text)


def test_uncapped_fill_floor_matches_balanced_cap():
    """frames.py deliberately does not import config; this is the cross-module
    pin its UNCAPPED_FILL_TARGET comment promises."""
    import frames

    assert frames.UNCAPPED_FILL_TARGET == config.frame_cap("balanced")


def test_description_is_a_valid_autonomous_trigger():
    """The description is the ONLY text the model sees before deciding to fire
    the skill. It must stay a single-line unquoted YAML scalar within the Agent
    Skills 1024-char cap, and keep the trigger nouns the routing depends on."""
    description = _frontmatter("description")
    assert "\n" not in description
    assert len(description) <= 1024
    assert ": " not in description, "colon-space breaks an unquoted YAML scalar"
    for term in (
        "motion", "easing", "pacing", "screen recording", "audio",
        "editing style", "transcrib", "animation",
    ):
        assert term in description, f"trigger term {term!r} fell out of the description"
