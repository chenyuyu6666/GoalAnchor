from __future__ import annotations

import importlib.util
import re
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
SKILL_FILES = sorted((ROOT / "skills").rglob("SKILL.md"))
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
QUICK_VALIDATE_PATH = (
    ROOT
    / "skills"
    / ".system"
    / "skill-creator"
    / "scripts"
    / "quick_validate.py"
)


def _frontmatter(skill_file: Path) -> dict:
    content = skill_file.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(content)
    assert match, f"{skill_file} must start with YAML frontmatter"
    payload = yaml.safe_load(match.group(1))
    assert isinstance(payload, dict), f"{skill_file} frontmatter must be a mapping"
    return payload


def _load_quick_validator():
    spec = importlib.util.spec_from_file_location("quick_validate_under_test", QUICK_VALIDATE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_expected_skill_inventory_is_present() -> None:
    names = {skill_file.parent.name for skill_file in SKILL_FILES}
    expected = {
        "deeply",
        "graphify",
        "goalanchor",
        "imagegen",
        "openai-docs",
        "plugin-creator",
        "review-agent",
        "skill-creator",
        "skill-installer",
    }
    assert expected <= names


def test_skill_frontmatter_is_valid_and_matches_directory() -> None:
    assert SKILL_FILES, "no SKILL.md files found"
    for skill_file in SKILL_FILES:
        payload = _frontmatter(skill_file)
        name = payload.get("name")
        description = payload.get("description")
        assert isinstance(name, str) and SKILL_NAME_RE.fullmatch(name), skill_file
        assert name == skill_file.parent.name, skill_file
        assert isinstance(description, str) and description.strip(), skill_file


def test_bundled_quick_validator_accepts_all_skills() -> None:
    validator = _load_quick_validator()
    for skill_file in SKILL_FILES:
        valid, message = validator.validate_skill(skill_file.parent)
        assert valid, f"{skill_file}: {message}"


def test_agent_metadata_is_valid_when_present() -> None:
    for skill_file in SKILL_FILES:
        agent_file = skill_file.parent / "agents" / "openai.yaml"
        if not agent_file.is_file():
            continue

        payload = yaml.safe_load(agent_file.read_text(encoding="utf-8"))
        assert isinstance(payload, dict), agent_file
        interface = payload.get("interface")
        assert isinstance(interface, dict), agent_file
        assert isinstance(interface.get("display_name"), str)
        short_description = interface.get("short_description")
        assert isinstance(short_description, str) and short_description.strip()

        for field in ("icon_small", "icon_large"):
            icon = interface.get(field)
            if icon:
                assert (agent_file.parent.parent / icon).is_file(), (agent_file, icon)
