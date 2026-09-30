from __future__ import annotations

from pathlib import Path

import pytest

from goalanchor.compat import (
    HOSTS,
    SKILL_ID,
    check_install,
    find_skill_source,
    install_skill,
    main,
    resolve_destination,
    validate_skill_directory,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "goalanchor"


def test_supported_hosts_have_explicit_project_and_user_mapping() -> None:
    assert {
        "codex",
        "agents",
        "claude",
        "dsh",
        "cursor",
        "gemini",
        "opencode",
        "pi",
        "zcode",
        "custom",
    } <= set(HOSTS)
    for spec in HOSTS.values():
        assert spec.name
        assert spec.notes


def test_user_destinations_use_host_environment_overrides(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = resolve_destination(
        "codex",
        scope="user",
        home=home,
        environ={"CODEX_HOME": str(tmp_path / "codex-home")},
    )
    claude = resolve_destination("claude", scope="user", home=home, environ={})
    dsh = resolve_destination(
        "dsh",
        scope="user",
        home=home,
        environ={"DSH_HOME": str(tmp_path / "dsh-home")},
    )
    cursor = resolve_destination("cursor", scope="user", home=home, environ={})
    gemini = resolve_destination("gemini", scope="user", home=home, environ={})
    opencode = resolve_destination("opencode", scope="user", home=home, environ={})
    pi = resolve_destination("pi", scope="user", home=home, environ={})

    assert codex == tmp_path / "codex-home" / "skills" / SKILL_ID
    assert claude == home / ".claude" / "skills" / SKILL_ID
    assert dsh == tmp_path / "dsh-home" / "skills" / SKILL_ID
    assert cursor == home / ".cursor" / "skills" / SKILL_ID
    assert gemini == home / ".gemini" / "skills" / SKILL_ID
    assert opencode == home / ".config" / "opencode" / "skills" / SKILL_ID
    assert pi == home / ".pi" / "agent" / "skills" / SKILL_ID


def test_windows_userprofile_wins_over_shell_home_override(tmp_path: Path) -> None:
    userprofile = tmp_path / "windows-user"
    shell_home = tmp_path / "cadence-profile"
    destination = resolve_destination(
        "claude",
        scope="user",
        environ={"HOME": str(shell_home), "USERPROFILE": str(userprofile)},
    )

    # The production host is Windows; USERPROFILE is the OS user root while
    # HOME may be supplied by an embedded shell or another application.
    if __import__("os").name == "nt":
        assert destination == userprofile / ".claude" / "skills" / SKILL_ID
    else:
        assert destination == shell_home / ".claude" / "skills" / SKILL_ID


def test_project_destinations_are_host_specific(tmp_path: Path) -> None:
    project = tmp_path / "repo"

    assert resolve_destination("codex", scope="project", project_root=project) == (
        project / ".agents" / "skills" / SKILL_ID
    )
    assert resolve_destination("claude", scope="project", project_root=project) == (
        project / ".claude" / "skills" / SKILL_ID
    )
    assert resolve_destination("dsh", scope="project", project_root=project) == (
        project / ".dsh" / "skills" / SKILL_ID
    )
    assert resolve_destination("cursor", scope="project", project_root=project) == (
        project / ".cursor" / "skills" / SKILL_ID
    )
    assert resolve_destination("gemini", scope="project", project_root=project) == (
        project / ".gemini" / "skills" / SKILL_ID
    )
    assert resolve_destination("opencode", scope="project", project_root=project) == (
        project / ".opencode" / "skills" / SKILL_ID
    )
    assert resolve_destination("pi", scope="project", project_root=project) == (
        project / ".pi" / "skills" / SKILL_ID
    )


def test_custom_destination_is_explicit_and_no_shell_is_required(tmp_path: Path) -> None:
    destination = resolve_destination(
        "custom", scope="user", destination=tmp_path / "custom-skills"
    )
    assert destination == tmp_path / "custom-skills" / SKILL_ID

    with pytest.raises(ValueError, match="custom host"):
        resolve_destination("custom", scope="user", home=tmp_path)

    zcode = resolve_destination(
        "zcode", scope="user", destination=tmp_path / "zcode-skills"
    )
    assert zcode == tmp_path / "zcode-skills" / SKILL_ID
    with pytest.raises(ValueError, match="zcode host"):
        resolve_destination("zcode", scope="user", home=tmp_path)


def test_source_skill_has_portable_minimum_metadata() -> None:
    source = find_skill_source(SOURCE)
    report = validate_skill_directory(source)

    assert report["skill_id"] == SKILL_ID
    assert int(report["bytes"]) > 0
    assert len(str(report["sha256"])) == 64


def test_core_skill_body_does_not_bind_to_one_host() -> None:
    content = (SOURCE / "SKILL.md").read_text(encoding="utf-8").lower()
    for host_specific_fragment in (
        "powershell",
        "bash",
        "d:\\",
        "c:\\",
        ".claude/",
        ".dsh/",
        ".codex/",
        "ctx.tools",
        "mcp",
    ):
        assert host_specific_fragment not in content


def test_install_and_check_are_repeatable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dry_run = install_skill(
        "claude",
        scope="project",
        project_root=project,
        source=SOURCE,
        dry_run=True,
    )
    assert dry_run["dry_run"] is True
    assert not Path(str(dry_run["destination"])).exists()

    installed = install_skill(
        "claude",
        scope="project",
        project_root=project,
        source=SOURCE,
    )
    assert installed["installed"]["skill_id"] == SKILL_ID

    checked = check_install(
        "claude",
        scope="project",
        project_root=project,
        source=SOURCE,
    )
    assert checked["match"] is True
    assert checked["source"]["sha256"] == checked["destination"]["sha256"]


def test_every_known_host_can_install_a_project_skill(tmp_path: Path) -> None:
    for target in sorted(name for name in HOSTS if name not in {"custom", "zcode"}):
        project = tmp_path / target
        installed = install_skill(
            target,
            scope="project",
            project_root=project,
            source=SOURCE,
        )
        assert installed["installed"]["skill_id"] == SKILL_ID
        checked = check_install(
            target,
            scope="project",
            project_root=project,
            source=SOURCE,
        )
        assert checked["match"] is True


def test_install_does_not_overwrite_without_explicit_force(tmp_path: Path) -> None:
    project = tmp_path / "project"
    kwargs = {
        "target": "dsh",
        "scope": "project",
        "project_root": project,
        "source": SOURCE,
    }
    install_skill(**kwargs)
    with pytest.raises(FileExistsError, match="already exists"):
        install_skill(**kwargs)

    replaced = install_skill(**kwargs, force=True)
    assert replaced["replaced"] is True


def test_cli_dry_run_uses_machine_readable_exit_code(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "install",
            "--target",
            "agents",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path / "repo"),
            "--source",
            str(SOURCE),
            "--dry-run",
            "--json",
        ]
    )
    assert exit_code == 0
    assert '"dry_run": true' in capsys.readouterr().out


def test_cli_reports_invalid_install_without_traceback(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "install",
            "--target",
            "claude",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path / "repo"),
            "--source",
            str(tmp_path / "missing"),
        ]
    )
    assert exit_code == 2
    assert "could not find SKILL.md" in capsys.readouterr().err
