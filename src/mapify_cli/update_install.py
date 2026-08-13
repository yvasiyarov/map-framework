"""Exact package installation and installed-provider refresh boundaries."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from mapify_cli.update_versions import StableVersion

COMMAND_TIMEOUT_SECONDS = 300.0
MAX_ERROR_OUTPUT_CHARS = 4_096

CommandRunner = Callable[[list[str], Path, float], subprocess.CompletedProcess[str]]


class InstallKind(StrEnum):
    """Supported mapify-cli installation layouts."""

    UV_TOOL = "uv-tool"
    PIP = "pip"
    SOURCE = "source"


class PackageUpdateError(RuntimeError):
    """Exact package installation failed."""


class ProjectRefreshError(RuntimeError):
    """One or more installed provider surfaces could not be refreshed."""

    def __init__(
        self,
        message: str,
        *,
        refreshed_providers: tuple[str, ...] = (),
        pending_providers: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.refreshed_providers = refreshed_providers
        self.pending_providers = pending_providers


def run_command(
    command: list[str], cwd: Path, timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run one bounded command while capturing text output for diagnostics."""
    return subprocess.run(
        command,
        cwd=cwd,
        timeout=timeout,
        check=False,
        capture_output=True,
        text=True,
    )


def detect_install_kind(module_file: str | Path) -> InstallKind:
    """Classify a package module path like the existing public upgrade command."""
    package_path = str(Path(module_file).resolve()).replace("\\", "/")
    if "/uv/tools/" in package_path:
        return InstallKind.UV_TOOL
    if "/site-packages/" in package_path or "/dist-packages/" in package_path:
        return InstallKind.PIP
    return InstallKind.SOURCE


def build_package_install_command(
    kind: InstallKind,
    version: StableVersion,
    *,
    python_executable: str | None = None,
) -> list[str] | None:
    """Build the exact, argument-array package installation command."""
    requirement = f"mapify-cli=={version}"
    if kind is InstallKind.UV_TOOL:
        uv = shutil.which("uv")
        if uv is None:
            return None
        return [uv, "tool", "install", "--force", requirement]
    if kind is InstallKind.PIP:
        return [
            python_executable or sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            requirement,
        ]
    return None


def _bounded_stderr(stderr: str | bytes | None) -> str:
    """Return bounded subprocess diagnostics suitable for an exception message."""
    if isinstance(stderr, bytes):
        value = stderr.decode(errors="replace").strip()
    else:
        value = (stderr or "").strip()
    if not value:
        return "no stderr output"
    return value[:MAX_ERROR_OUTPUT_CHARS]


def install_exact_version(
    project_path: Path,
    version: StableVersion,
    *,
    module_file: str | Path | None = None,
    runner: CommandRunner = run_command,
) -> None:
    """Install one exact stable mapify-cli version in the current environment."""
    package_module = module_file or Path(__file__).with_name("__init__.py")
    install_kind = detect_install_kind(package_module)
    command = build_package_install_command(install_kind, version)
    if command is None:
        if install_kind is InstallKind.SOURCE:
            raise PackageUpdateError(
                "mapify-cli is running from a source checkout; update the checkout "
                "manually before refreshing this project"
            )
        raise PackageUpdateError(
            "mapify-cli is installed as a uv tool, but the uv executable could not "
            "be found; install uv or update mapify-cli manually"
        )

    try:
        result = runner(command, project_path, COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise PackageUpdateError(
            f"Exact mapify-cli {version} installation timed out after "
            f"{COMMAND_TIMEOUT_SECONDS:g} seconds: {_bounded_stderr(exc.stderr)}"
        ) from exc
    except OSError as exc:
        raise PackageUpdateError(
            f"Exact mapify-cli {version} installation could not be started: {exc}"
        ) from exc
    if result.returncode != 0:
        raise PackageUpdateError(
            f"Exact mapify-cli {version} installation failed with exit "
            f"{result.returncode}: {_bounded_stderr(result.stderr)}"
        )


def installed_providers(project_path: Path) -> tuple[str, ...]:
    """Return complete installed provider layouts in canonical order."""
    providers: list[str] = []
    if (project_path / ".claude" / "skills").is_dir():
        providers.append("claude")
    if (project_path / ".codex" / "config.toml").is_file() and (
        project_path / ".agents" / "skills"
    ).is_dir():
        providers.append("codex")
    return tuple(providers)


def resolve_mapify_executable() -> str:
    """Resolve mapify from the current interpreter environment before PATH."""
    executable_name = "mapify.exe" if os.name == "nt" else "mapify"
    environment_mapify = Path(sys.executable).with_name(executable_name)
    if environment_mapify.is_file():
        return str(environment_mapify)

    path_mapify = shutil.which("mapify")
    if path_mapify is not None:
        return path_mapify

    raise ProjectRefreshError(
        "Could not find the mapify executable in the current Python environment "
        "or on PATH; reinstall mapify-cli in this environment and retry"
    )


def refresh_installed_providers(
    project_path: Path,
    providers: Sequence[str],
    *,
    mapify_executable: str | Path | None = None,
    runner: CommandRunner = run_command,
) -> tuple[str, ...]:
    """Refresh each installed provider in a newly launched mapify process."""
    pending = tuple(providers)
    if mapify_executable is not None:
        executable = str(mapify_executable)
    else:
        try:
            executable = resolve_mapify_executable()
        except ProjectRefreshError as exc:
            raise ProjectRefreshError(str(exc), pending_providers=pending) from exc
    refreshed: list[str] = []

    for index, provider in enumerate(pending):
        command = [
            executable,
            "init",
            ".",
            "--force",
            "--no-git",
            "--provider",
            provider,
            "--refresh-existing",
        ]
        remaining = pending[index:]
        try:
            result = runner(command, project_path, COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ProjectRefreshError(
                f"Refreshing the {provider} provider timed out after "
                f"{COMMAND_TIMEOUT_SECONDS:g} seconds: "
                f"{_bounded_stderr(exc.stderr)}",
                refreshed_providers=tuple(refreshed),
                pending_providers=remaining,
            ) from exc
        except OSError as exc:
            raise ProjectRefreshError(
                f"Refreshing the {provider} provider could not be started: {exc}",
                refreshed_providers=tuple(refreshed),
                pending_providers=remaining,
            ) from exc

        if result.returncode != 0:
            raise ProjectRefreshError(
                f"Refreshing the {provider} provider failed with exit "
                f"{result.returncode}: {_bounded_stderr(result.stderr)}",
                refreshed_providers=tuple(refreshed),
                pending_providers=remaining,
            )
        refreshed.append(provider)

    return tuple(refreshed)
