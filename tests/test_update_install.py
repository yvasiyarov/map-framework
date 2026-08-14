"""Exact package installation and installed-provider refresh boundaries."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import mapify_cli.update_state as update_state_module
from mapify_cli.update_install import (
    InstallKind,
    PackageUpdateError,
    ProjectRefreshError,
    build_package_install_command,
    detect_install_kind,
    install_exact_version,
    installed_providers,
    refresh_installed_providers,
    resolve_mapify_executable,
    run_command,
)
from mapify_cli.update_state import project_update_lock
from mapify_cli.update_versions import StableVersion


@pytest.mark.parametrize(
    ("module_file", "expected"),
    [
        (
            (
                "/home/user/.local/share/uv/tools/mapify-cli/lib/python3.11/"
                "site-packages/mapify_cli/__init__.py"
            ),
            InstallKind.UV_TOOL,
        ),
        (
            "/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
            InstallKind.PIP,
        ),
        (
            r"C:\venv\Lib\site-packages\mapify_cli\__init__.py",
            InstallKind.PIP,
        ),
        (
            "/usr/lib/python3/dist-packages/mapify_cli/__init__.py",
            InstallKind.PIP,
        ),
        ("/workspace/map-framework/src/mapify_cli/__init__.py", InstallKind.SOURCE),
    ],
)
def test_install_kind_matches_public_upgrade_classification(
    module_file: str, expected: InstallKind
) -> None:
    assert detect_install_kind(module_file) is expected


def test_uv_tool_exact_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )

    command = build_package_install_command(
        InstallKind.UV_TOOL, StableVersion(3, 26, 0)
    )

    assert command == [
        "/usr/bin/uv",
        "tool",
        "install",
        "--force",
        "mapify-cli==3.26.0",
    ]


def test_pip_exact_command() -> None:
    command = build_package_install_command(
        InstallKind.PIP,
        StableVersion(3, 26, 0),
        python_executable="/venv/bin/python",
    )

    assert command == [
        "/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "mapify-cli==3.26.0",
    ]


def test_source_has_no_install_command() -> None:
    assert (
        build_package_install_command(InstallKind.SOURCE, StableVersion(3, 26, 0))
        is None
    )


def test_install_exact_command_uses_project_and_300_second_timeout(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], Path, float]] = []

    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert "MAP_UPDATE_PARENT_LEASE" not in env
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "installed", "")

    install_exact_version(
        tmp_path,
        StableVersion(3, 26, 0),
        module_file="/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
        runner=runner,
    )

    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "mapify-cli==3.26.0",
            ],
            tmp_path,
            300.0,
        )
    ]


def test_install_exact_command_failure_has_bounded_actionable_stderr(
    tmp_path: Path,
) -> None:
    stderr = "installer failed: " + ("x" * 10_000) + "END-OF-STDERR"

    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        return subprocess.CompletedProcess(command, 23, "", stderr)

    with pytest.raises(PackageUpdateError) as exc_info:
        install_exact_version(
            tmp_path,
            StableVersion(3, 26, 0),
            module_file="/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
            runner=runner,
        )

    message = str(exc_info.value)
    assert "exit 23" in message
    assert "installer failed:" in message
    assert "END-OF-STDERR" not in message
    assert len(message) < 5_000


def test_source_install_exact_command_explains_manual_update(tmp_path: Path) -> None:
    with pytest.raises(PackageUpdateError, match="source checkout"):
        install_exact_version(
            tmp_path,
            StableVersion(3, 26, 0),
            module_file="/workspace/map-framework/src/mapify_cli/__init__.py",
        )


def test_install_exact_command_timeout_is_actionable(tmp_path: Path) -> None:
    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        raise subprocess.TimeoutExpired(command, timeout, stderr="network stalled")

    with pytest.raises(PackageUpdateError, match="timed out after 300 seconds"):
        install_exact_version(
            tmp_path,
            StableVersion(3, 26, 0),
            module_file="/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
            runner=runner,
        )


def test_install_exact_command_missing_runner_executable_is_actionable(
    tmp_path: Path,
) -> None:
    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, timeout, env
        raise FileNotFoundError("python vanished")

    with pytest.raises(PackageUpdateError, match="could not be started") as exc_info:
        install_exact_version(
            tmp_path,
            StableVersion(3, 26, 0),
            module_file="/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
            runner=runner,
        )

    assert "python vanished" in str(exc_info.value)


def test_run_command_captures_text_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 7, "out", "err")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_command(["tool", "arg"], tmp_path, 300.0, env={"SAFE": "1"})

    assert result.returncode == 7
    assert observed == {
        "command": ["tool", "arg"],
        "cwd": tmp_path,
        "timeout": 300.0,
        "check": False,
        "capture_output": True,
        "text": True,
        "env": {"SAFE": "1"},
    }


def test_package_install_strips_parent_lease_from_child_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = getattr(
        update_state_module,
        "MAP_UPDATE_PARENT_LEASE_ENV",
        "MAP_UPDATE_PARENT_LEASE",
    )
    monkeypatch.setenv(env_name, "ambient-forged-value")
    observed: dict[str, str] = {}

    def runner(
        command: list[str],
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        observed.update(env)
        return subprocess.CompletedProcess(command, 0, "", "")

    install_exact_version(
        tmp_path,
        StableVersion(3, 26, 0),
        module_file="/venv/lib/python3.11/site-packages/mapify_cli/__init__.py",
        runner=runner,
    )

    assert env_name not in observed


def test_refresh_passes_active_parent_lease_only_in_provider_child_environment(
    tmp_path: Path,
) -> None:
    env_name = getattr(
        update_state_module,
        "MAP_UPDATE_PARENT_LEASE_ENV",
        "MAP_UPDATE_PARENT_LEASE",
    )
    calls: list[tuple[list[str], Mapping[str, str]]] = []

    def runner(
        command: list[str],
        cwd: Path,
        timeout: float,
        env: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        calls.append((command, env))
        return subprocess.CompletedProcess(command, 0, "", "")

    with project_update_lock(tmp_path, timeout_s=0.0) as lease:
        refreshed = refresh_installed_providers(
            tmp_path,
            ("claude", "codex"),
            mapify_executable="/bin/mapify",
            runner=runner,
        )

    assert refreshed == ("claude", "codex")
    assert len(calls) == 2
    for command, env in calls:
        assert lease.token not in command
        assert env[env_name] == lease.token


def test_dual_provider_detection_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("", encoding="utf-8")
    (tmp_path / ".agents" / "skills").mkdir(parents=True)

    assert installed_providers(tmp_path) == ("claude", "codex")


def test_provider_detection_requires_complete_layouts(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("", encoding="utf-8")

    assert installed_providers(tmp_path) == ()

    (tmp_path / ".agents" / "skills").mkdir(parents=True)

    assert installed_providers(tmp_path) == ("codex",)


def test_refresh_runs_fresh_mapify_init_for_both_providers(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path, float]] = []

    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        assert "MAP_UPDATE_PARENT_LEASE" not in env
        calls.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "", "")

    refreshed = refresh_installed_providers(
        tmp_path,
        ("claude", "codex"),
        mapify_executable="/bin/mapify",
        runner=runner,
    )

    assert refreshed == ("claude", "codex")
    assert calls == [
        (
            [
                "/bin/mapify",
                "init",
                ".",
                "--force",
                "--no-git",
                "--provider",
                "claude",
                "--refresh-existing",
            ],
            tmp_path,
            300.0,
        ),
        (
            [
                "/bin/mapify",
                "init",
                ".",
                "--force",
                "--no-git",
                "--provider",
                "codex",
                "--refresh-existing",
            ],
            tmp_path,
            300.0,
        ),
    ]


def test_refresh_failure_reports_completed_and_pending_providers(
    tmp_path: Path,
) -> None:
    stderr = "refresh failed: " + ("x" * 10_000) + "END-OF-STDERR"

    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout, env
        if command[command.index("--provider") + 1] == "claude":
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 19, "", stderr)

    with pytest.raises(ProjectRefreshError) as exc_info:
        refresh_installed_providers(
            tmp_path,
            ("claude", "codex"),
            mapify_executable="/bin/mapify",
            runner=runner,
        )

    error = exc_info.value
    assert error.refreshed_providers == ("claude",)
    assert error.pending_providers == ("codex",)
    assert "codex" in str(error)
    assert "exit 19" in str(error)
    assert "refresh failed:" in str(error)
    assert "END-OF-STDERR" not in str(error)
    assert len(str(error)) < 5_000


def test_refresh_timeout_preserves_full_pending_state(tmp_path: Path) -> None:
    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env
        raise subprocess.TimeoutExpired(command, timeout, stderr="too slow")

    with pytest.raises(ProjectRefreshError) as exc_info:
        refresh_installed_providers(
            tmp_path,
            ("claude", "codex"),
            mapify_executable="/bin/mapify",
            runner=runner,
        )

    error = exc_info.value
    assert error.refreshed_providers == ()
    assert error.pending_providers == ("claude", "codex")
    assert "timed out after 300 seconds" in str(error)


def test_refresh_missing_executable_preserves_full_pending_state(
    tmp_path: Path,
) -> None:
    def runner(
        command: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
    ) -> subprocess.CompletedProcess[str]:
        del command, cwd, timeout, env
        raise FileNotFoundError("mapify vanished")

    with pytest.raises(ProjectRefreshError) as exc_info:
        refresh_installed_providers(
            tmp_path,
            ("claude", "codex"),
            mapify_executable="/bin/mapify",
            runner=runner,
        )

    error = exc_info.value
    assert error.refreshed_providers == ()
    assert error.pending_providers == ("claude", "codex")
    assert "could not be started" in str(error)
    assert "mapify vanished" in str(error)


def test_resolve_mapify_prefers_current_interpreter_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    mapify = tmp_path / ("mapify.exe" if os.name == "nt" else "mapify")
    python.touch()
    mapify.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda name: "/other/mapify")

    assert resolve_mapify_executable() == str(mapify)


def test_resolve_mapify_falls_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    python.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(
        shutil, "which", lambda name: "/path/bin/mapify" if name == "mapify" else None
    )

    assert resolve_mapify_executable() == "/path/bin/mapify"


def test_resolve_mapify_missing_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    python.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(ProjectRefreshError, match="mapify executable") as exc_info:
        resolve_mapify_executable()

    assert "current Python environment" in str(exc_info.value)


def test_refresh_resolution_failure_preserves_all_pending_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / ("python.exe" if os.name == "nt" else "python")
    python.touch()
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(ProjectRefreshError) as exc_info:
        refresh_installed_providers(tmp_path, ("claude", "codex"))

    assert exc_info.value.refreshed_providers == ()
    assert exc_info.value.pending_providers == ("claude", "codex")
