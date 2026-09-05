from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "skills"
    / ".system"
    / "skill-installer"
    / "scripts"
    / "github_utils.py"
)
INSTALLER_PATH = MODULE_PATH.parent / "install-skill-from-github.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("github_utils_under_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_installer():
    # The installer imports its sibling github_utils module by name.
    sys.path.insert(0, str(MODULE_PATH.parent))
    spec = importlib.util.spec_from_file_location(
        "install_skill_from_github_under_test", INSTALLER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_github_request_sets_auth_header_and_timeout(monkeypatch) -> None:
    github_utils = _load_module()
    observed = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(request, timeout):
        observed["request"] = request
        observed["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setattr(github_utils.urllib.request, "urlopen", fake_urlopen)

    assert github_utils.github_request("https://api.github.com/test", "test-agent") == b"ok"
    assert observed["timeout"] == github_utils.DEFAULT_TIMEOUT_SECONDS
    assert observed["request"].get_header("Authorization") == "token secret-token"
    assert observed["request"].get_header("User-agent") == "test-agent"


def test_github_request_rejects_non_positive_timeout() -> None:
    github_utils = _load_module()

    try:
        github_utils.github_request("https://api.github.com/test", "test-agent", timeout=0)
    except ValueError as exc:
        assert str(exc) == "timeout must be greater than zero"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("non-positive timeout should fail")


def test_installer_converts_git_timeout_to_install_error(monkeypatch) -> None:
    installer = _load_installer()

    def timeout_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(installer.subprocess, "run", timeout_run)

    try:
        installer._run_git(["git", "status"])
    except installer.InstallError as exc:
        assert "timed out" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("git timeout should become InstallError")


def test_installer_converts_download_network_error(monkeypatch, tmp_path) -> None:
    installer = _load_installer()

    def fail_request(_url):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(installer, "_request", fail_request)

    try:
        installer._download_repo_zip("owner", "repo", "main", str(tmp_path))
    except installer.InstallError as exc:
        assert "Download failed: offline" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("network failure should become InstallError")
