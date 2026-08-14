"""sofa_client.py — Stack Overflow for Agents (SOFA) HTTP client.

Self-contained, stdlib-only client.  Imports ONLY from the Python standard
library — no httpx, requests, or mapify_cli.

Responsibilities:
- base_url / API-key resolution (env → .sofa/credentials.json)
- 7-step human-gated onboarding (never invents agent_name/description)
- Credential storage with .gitignore-before-key ordering
- Session create + 401-retry (exactly once, ≥1 s backoff)
- Read endpoints: GET /api/posts (search), GET /api/posts/{id}
- All errors returned as typed result dicts; nothing raised through
"""

from __future__ import annotations

import inspect
import json
import os
import secrets
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, NoReturn

# ---------------------------------------------------------------------------
# Typed result helpers
# ---------------------------------------------------------------------------

_ResultDict = dict[str, Any]


def _ok(**fields: Any) -> _ResultDict:
    return {"ok": True, **fields}


def _err(kind: str, error: str, **fields: Any) -> _ResultDict:
    return {"ok": False, "kind": kind, "error": error, **fields}


# ---------------------------------------------------------------------------
# Base-URL resolution (D6 / VC5)
# NEVER a hardcoded fallback — stop and ask the human if env is unset.
# ---------------------------------------------------------------------------


def resolve_base_url() -> _ResultDict:
    """Return the SOFA base URL from SOFA_BASE_URL env var.

    Resolution order (from spike §1.1 / binding strategy D6):
    1. SOFA_BASE_URL env var when set.
    2. Stop and ask — never a hardcoded constant.

    Returns _ok(base_url=...) or _err("need_base_url", ...).
    """
    url = os.environ.get("SOFA_BASE_URL", "").strip()
    if url:
        return _ok(base_url=url.rstrip("/"))
    return _err(
        "need_base_url",
        "SOFA_BASE_URL is not set. Please set it to the SOFA deployment URL "
        "(e.g. https://agents.stackoverflow.com) and retry.",
    )


# ---------------------------------------------------------------------------
# Key resolution (D5)
# ---------------------------------------------------------------------------


def resolve_key(
    agent_id: str | None = None, credentials_path: Path | None = None
) -> _ResultDict:
    """Resolve the SOFA API key.

    Order: SOFA_API_KEY env → .sofa/credentials.json keyed by agent_id.
    Returns _ok(api_key=..., agent_id=...) or _err("no_key", ...).
    """
    env_key = os.environ.get("SOFA_API_KEY", "").strip()
    if env_key:
        return _ok(api_key=env_key, agent_id=agent_id or "env")

    if credentials_path is None:
        return _err("no_key", "SOFA_API_KEY not set and no credentials_path provided.")

    try:
        data = _load_credentials(Path(credentials_path), missing_ok=True)
    except SofaCredentialsFormatError as exc:
        return _err("bad_json", str(exc))
    except (OSError, SofaCredentialsSecurityError) as exc:
        return _err(
            "credential_storage_error",
            f"Cannot securely read credentials: {exc}",
        )
    if data is None:
        return _err("no_key", "No credentials file is available.")

    if agent_id:
        entry = data.get(agent_id)
        if not entry:
            return _err(
                "no_key", f"No entry for agent_id={agent_id!r} in credentials file."
            )
        key = entry.get("api_key", "")
        if not key:
            return _err("no_key", f"Empty api_key for agent_id={agent_id!r}.")
        return _ok(api_key=key, agent_id=agent_id)

    # Pick the first stored agent when agent_id not specified
    for aid, entry in data.items():
        key = entry.get("api_key", "")
        if key:
            return _ok(api_key=key, agent_id=aid)

    return _err("no_key", "credentials file has no usable entries.")


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> _ResultDict:
    """Make a single HTTP request; return a typed result dict.

    Never raises — all exceptions are caught and returned as _err.
    """
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req_headers = headers or {}
    if data is not None:
        req_headers = {**req_headers, "Content-Type": "application/json"}

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status: int = resp.status
    except urllib.error.HTTPError as exc:
        # Capture body for error detail
        try:
            raw = exc.read()
            status = exc.code
        except Exception:  # noqa: BLE001 -- deliberate fallback boundary
            raw = b""
            status = exc.code
        try:
            err_body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            err_body = {}
        if not isinstance(err_body, dict):
            err_body = {}
        return _err(
            "http_error",
            err_body.get("error") or err_body.get("detail") or exc.reason or str(exc),
            status=status,
            body=err_body,
        )
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "timed out" in reason.lower() or "timeout" in reason.lower():
            return _err("timeout", f"Request timed out: {reason}")
        return _err("network", f"Network error: {reason}")
    except Exception as exc:  # noqa: BLE001
        return _err("network", f"Unexpected request error: {exc}")

    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _err(
            "bad_json",
            f"Non-JSON response (status={status})",
            status=status,
            raw=raw[:200].decode("utf-8", errors="replace"),
        )
    if not isinstance(parsed, dict):
        return _err(
            "bad_json",
            f"Expected a JSON object response, got {type(parsed).__name__} (status={status}).",
            status=status,
        )

    return _ok(status=status, data=parsed)


# ---------------------------------------------------------------------------
# .gitignore helpers (inline, no mapify_cli import — AC-11 / VC3)
# ---------------------------------------------------------------------------

_SOFA_MARKER = "# map:sofa"
_SOFA_BLOCK = "# map:sofa — SOFA credential dir (opt-in); never commit. See docs/USAGE.md\n.sofa/\n"


class SofaGitignoreSecurityError(RuntimeError):
    """Raised when the project .gitignore cannot be updated safely."""


def _unsafe_gitignore(gitignore: Path, reason: str) -> NoReturn:
    raise SofaGitignoreSecurityError(f"unsafe project .gitignore: {reason}")


def _validated_gitignore_stat(gitignore: Path) -> os.stat_result | None:
    try:
        current = os.lstat(gitignore)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(current.st_mode):
        _unsafe_gitignore(gitignore, "symbolic links are not allowed")
    if not stat.S_ISREG(current.st_mode):
        _unsafe_gitignore(gitignore, "the path must be a regular file")
    if current.st_nlink != 1:
        _unsafe_gitignore(gitignore, "hard-linked files are not allowed")
    return current


def _read_safe_gitignore(
    gitignore: Path,
) -> tuple[bytes, os.stat_result | None]:
    initial = _validated_gitignore_stat(gitignore)
    if initial is None:
        return b"", None

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(gitignore, flags)
    except OSError as exc:
        _unsafe_gitignore(gitignore, f"could not open it safely ({exc})")

    try:
        opened = os.fstat(descriptor)
        current = _validated_gitignore_stat(gitignore)
        if current is None or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            _unsafe_gitignore(gitignore, "the path changed while being read")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _unsafe_gitignore(gitignore, "the opened file is not private")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read(), opened
    finally:
        os.close(descriptor)


def _validate_gitignore_unchanged(
    gitignore: Path,
    original: os.stat_result | None,
) -> None:
    current = _validated_gitignore_stat(gitignore)
    if original is None:
        if current is not None:
            _unsafe_gitignore(gitignore, "the path appeared during the update")
        return
    if current is None or (current.st_dev, current.st_ino) != (
        original.st_dev,
        original.st_ino,
    ):
        _unsafe_gitignore(gitignore, "the path changed during the update")


def _atomic_replace_gitignore(
    gitignore: Path,
    content: bytes,
    original: os.stat_result | None,
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=gitignore.parent,
        prefix=".gitignore.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        mode = stat.S_IMODE(original.st_mode) if original is not None else 0o644
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_gitignore_unchanged(gitignore, original)
        os.replace(temporary, gitignore)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _has_effective_sofa_ignore(lines: list[bytes]) -> bool:
    last_exact = -1
    for index, line in enumerate(lines):
        if line == b".sofa/":
            last_exact = index
    return last_exact >= 0 and not any(
        line.startswith(b"!") for line in lines[last_exact + 1 :]
    )


def _has_sofa_marker(lines: list[bytes]) -> bool:
    marker = _SOFA_MARKER.encode()
    return any(line == marker or line.startswith(marker + b" ") for line in lines)


def ensure_sofa_gitignore(repo_root: Path) -> bool:
    """Ensure .sofa/ is in repo_root/.gitignore under the `# map:sofa` marker.

    The exact canonical path is authoritative only when no later unescaped
    negation follows it. Marker-only and superseded states are repaired.
    Returns True if the file was modified, False if already present.
    """
    project_root = repo_root.resolve(strict=True)
    if not project_root.is_dir():
        raise NotADirectoryError("SOFA project root is not a directory")
    gitignore = project_root / ".gitignore"
    existing, original = _read_safe_gitignore(gitignore)
    existing_lines = existing.splitlines()
    if _has_effective_sofa_ignore(existing_lines):
        _validate_gitignore_unchanged(gitignore, original)
        return False

    addition = b".sofa/\n" if _has_sofa_marker(existing_lines) else _SOFA_BLOCK.encode()
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    _atomic_replace_gitignore(gitignore, existing + separator + addition, original)
    return True


# ---------------------------------------------------------------------------
# Credential storage (VC3 / VC4)
# ---------------------------------------------------------------------------


class SofaCredentialsSecurityError(RuntimeError):
    """Raised when the local credential path is unsafe."""


class SofaCredentialsFormatError(ValueError):
    """Raised when existing credentials do not match the expected JSON shape."""


def _replace_supports_directory_fds() -> bool:
    """Return whether this platform's ``os.replace`` accepts directory FDs."""
    try:
        parameters = inspect.signature(os.replace).parameters
    except (TypeError, ValueError):
        return False
    return "src_dir_fd" in parameters and "dst_dir_fd" in parameters


_CAN_USE_DIRECTORY_FD = (
    hasattr(os, "O_DIRECTORY")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.unlink in os.supports_dir_fd
    and _replace_supports_directory_fds()
)


def _unsafe_credentials(reason: str) -> NoReturn:
    raise SofaCredentialsSecurityError(f"unsafe SOFA credentials path: {reason}")


def _open_sofa_directory(
    repo_root: Path,
    *,
    create: bool,
) -> tuple[Path, os.stat_result, int | None] | None:
    """Validate and optionally create the direct .sofa directory.

    On platforms with directory-relative operations, the returned descriptor
    pins the validated directory for the whole read or write transaction.
    """
    project_root = repo_root.resolve(strict=True)
    if not project_root.is_dir():
        _unsafe_credentials("the project root is not a directory")
    sofa_dir = project_root / ".sofa"
    try:
        current = os.lstat(sofa_dir)
    except FileNotFoundError:
        if not create:
            return None
        try:
            sofa_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        current = os.lstat(sofa_dir)

    if stat.S_ISLNK(current.st_mode):
        _unsafe_credentials(".sofa must not be a symbolic link")
    if not stat.S_ISDIR(current.st_mode):
        _unsafe_credentials(".sofa must be a directory")
    if sofa_dir.resolve(strict=True) != project_root / ".sofa":
        _unsafe_credentials(".sofa resolved outside the project root")

    if not _CAN_USE_DIRECTORY_FD:
        return sofa_dir, current, None

    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = os.open(sofa_dir, flags)
    except OSError as exc:
        _unsafe_credentials(f"could not open .sofa safely ({exc})")
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
        current.st_dev,
        current.st_ino,
    ):
        os.close(descriptor)
        _unsafe_credentials(".sofa changed while being opened")
    return sofa_dir, opened, descriptor


def _validate_sofa_directory_unchanged(
    sofa_dir: Path,
    original: os.stat_result,
) -> None:
    try:
        current = os.lstat(sofa_dir)
    except FileNotFoundError:
        _unsafe_credentials(".sofa disappeared during credential access")
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)
    ):
        _unsafe_credentials(".sofa changed during credential access")


def _validated_credentials_stat(
    credentials_file: Path,
    directory_fd: int | None,
) -> os.stat_result | None:
    try:
        if directory_fd is None:
            current = os.lstat(credentials_file)
        else:
            current = os.stat(
                credentials_file.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(current.st_mode):
        _unsafe_credentials("credentials.json must not be a symbolic link")
    if not stat.S_ISREG(current.st_mode):
        _unsafe_credentials("credentials.json must be a regular file")
    if current.st_nlink != 1:
        _unsafe_credentials("credentials.json must not be hard-linked")
    return current


def _validate_credentials_unchanged(
    credentials_file: Path,
    original: os.stat_result | None,
    directory_fd: int | None,
) -> None:
    current = _validated_credentials_stat(credentials_file, directory_fd)
    if original is None:
        if current is not None:
            _unsafe_credentials("credentials.json appeared during the update")
        return
    if current is None or (current.st_dev, current.st_ino) != (
        original.st_dev,
        original.st_ino,
    ):
        _unsafe_credentials("credentials.json changed during the update")


def _read_credentials_bytes(
    credentials_file: Path,
    sofa_dir: Path,
    sofa_original: os.stat_result,
    directory_fd: int | None,
) -> tuple[bytes | None, os.stat_result | None]:
    initial = _validated_credentials_stat(credentials_file, directory_fd)
    if initial is None:
        _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
        return None, None

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    target: str | Path = (
        credentials_file if directory_fd is None else credentials_file.name
    )
    try:
        if directory_fd is None:
            descriptor = os.open(target, flags)
        else:
            descriptor = os.open(target, flags, dir_fd=directory_fd)
    except OSError as exc:
        _unsafe_credentials(f"could not open credentials.json safely ({exc})")

    try:
        opened = os.fstat(descriptor)
        current = _validated_credentials_stat(credentials_file, directory_fd)
        if current is None or (opened.st_dev, opened.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            _unsafe_credentials("credentials.json changed while being read")
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            _unsafe_credentials("the opened credentials file is not private")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read()
        _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
        _validate_credentials_unchanged(credentials_file, opened, directory_fd)
        return content, opened
    finally:
        os.close(descriptor)


def _parse_credentials(content: bytes) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SofaCredentialsFormatError(
            "credentials.json must contain valid UTF-8 JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise SofaCredentialsFormatError("credentials.json must contain a JSON object.")
    for agent, entry in parsed.items():
        if not isinstance(agent, str) or not isinstance(entry, dict):
            raise SofaCredentialsFormatError(
                "each credentials.json entry must be an object keyed by agent id."
            )
        api_key = entry.get("api_key")
        if api_key is not None and not isinstance(api_key, str):
            raise SofaCredentialsFormatError(
                "each credentials.json api_key must be a string."
            )
    return parsed


def _credentials_location(credentials_path: Path) -> tuple[Path, Path]:
    requested = Path(os.path.abspath(credentials_path))
    if requested.name != "credentials.json" or requested.parent.name != ".sofa":
        _unsafe_credentials("expected a direct .sofa/credentials.json path")
    try:
        project_root = requested.parent.parent.resolve(strict=True)
    except OSError as exc:
        _unsafe_credentials(f"could not resolve the project root ({exc})")
    return project_root, project_root / ".sofa" / "credentials.json"


def _load_credentials(
    credentials_path: Path,
    *,
    missing_ok: bool,
) -> dict[str, Any] | None:
    project_root, credentials_file = _credentials_location(credentials_path)
    opened_directory = _open_sofa_directory(project_root, create=False)
    if opened_directory is None:
        if missing_ok:
            return None
        _unsafe_credentials(".sofa does not exist")
    sofa_dir, sofa_original, directory_fd = opened_directory
    try:
        content, _ = _read_credentials_bytes(
            credentials_file,
            sofa_dir,
            sofa_original,
            directory_fd,
        )
        if content is None:
            if missing_ok:
                return None
            _unsafe_credentials("credentials.json does not exist")
        return _parse_credentials(content)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _atomic_write_credentials(
    credentials_file: Path,
    content: bytes,
    original: os.stat_result | None,
    sofa_dir: Path,
    sofa_original: os.stat_result,
    directory_fd: int | None,
) -> None:
    if directory_fd is not None:
        descriptor = -1
        temporary_name = ""
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            for _ in range(32):
                temporary_name = f".credentials.json.{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(
                        temporary_name,
                        flags,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    break
                except FileExistsError:
                    continue
            if descriptor < 0:
                raise FileExistsError("could not allocate a private temporary file")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
            _validate_credentials_unchanged(
                credentials_file,
                original,
                directory_fd,
            )
            os.replace(
                temporary_name,
                credentials_file.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        return

    # Without directory-relative syscalls we cannot pin the parent directory,
    # so reject a swap before allocating any temp path and again immediately
    # after allocation, before secret bytes are written.
    _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=sofa_dir,
        prefix=".credentials.json.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    secret_written = False
    try:
        _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            secret_written = True
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
        _validate_credentials_unchanged(credentials_file, original, directory_fd)
        # Windows cannot atomically replace a file while this descriptor is
        # open. Close only after both target identities have been revalidated.
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, credentials_file)
    finally:
        if descriptor >= 0:
            if secret_written and hasattr(os, "ftruncate"):
                try:
                    os.ftruncate(descriptor, 0)
                    os.fsync(descriptor)
                except OSError:
                    pass
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def store_credentials(
    *,
    repo_root: Path,
    agent_id: str,
    api_key: str,
    agent_name: str,
    base_url: str,
    api_key_prefix: str,
    api_key_suffix: str,
) -> _ResultDict:
    """Store SOFA credentials in <repo_root>/.sofa/credentials.json.

    Ordering guarantee (AC-11): ensures .gitignore is updated BEFORE the
    credentials file is written.

    Never silently overwrites an existing agent_id entry.
    Sets file permissions to 0600.
    """
    # STEP 1: gitignore BEFORE key (ordering invariant)
    try:
        ensure_sofa_gitignore(repo_root)
    except Exception as exc:  # noqa: BLE001 -- typed security boundary
        return _err(
            "gitignore_error",
            f"Refusing to store credentials because .gitignore is unsafe: {exc}",
        )

    directory_fd: int | None = None
    try:
        opened_directory = _open_sofa_directory(repo_root, create=True)
        if opened_directory is None:
            _unsafe_credentials("could not create .sofa")
        sofa_dir, sofa_original, directory_fd = opened_directory
        creds_file = sofa_dir / "credentials.json"
        content, original = _read_credentials_bytes(
            creds_file,
            sofa_dir,
            sofa_original,
            directory_fd,
        )
        data = {} if content is None else _parse_credentials(content)

        if agent_id in data:
            _validate_sofa_directory_unchanged(sofa_dir, sofa_original)
            _validate_credentials_unchanged(creds_file, original, directory_fd)
            return _err(
                "duplicate_agent",
                f"Credentials for agent_id={agent_id!r} already exist. "
                "Remove the entry manually to re-register.",
            )

        data[agent_id] = {
            "agent_name": agent_name,
            "base_url": base_url,
            "api_key_prefix": api_key_prefix,
            "api_key_suffix": api_key_suffix,
            "api_key": api_key,
        }
        _atomic_write_credentials(
            creds_file,
            json.dumps(data, indent=2).encode("utf-8"),
            original,
            sofa_dir,
            sofa_original,
            directory_fd,
        )
        return _ok(agent_id=agent_id, path=str(creds_file))
    except SofaCredentialsFormatError as exc:
        return _err("bad_json", str(exc))
    except (OSError, SofaCredentialsSecurityError) as exc:
        return _err(
            "credential_storage_error",
            f"Refusing to store credentials securely: {exc}",
        )
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


# ---------------------------------------------------------------------------
# Onboarding (7 steps, human-gated) — VC5
# ---------------------------------------------------------------------------


def onboarding_start(base_url: str) -> _ResultDict:
    """Step 1: GET /api/onboarding — fetch contract + next_step."""
    return _request("GET", f"{base_url}/api/onboarding")


def onboarding_create_flow(
    base_url: str,
    *,
    client_name: str,
    client_version: str,
    model_name: str,
    model_provider: str,
    model_version: str,
    model_selection_mode: str,
) -> _ResultDict:
    """Step 2: POST /api/onboarding/flows — register client metadata.

    Returns flow_id, claim_url, claim_code, poll_token, poll_after_seconds.
    """
    result = _request(
        "POST",
        f"{base_url}/api/onboarding/flows",
        body={
            "client_name": client_name,
            "client_version": client_version,
            "model_name": model_name,
            "model_provider": model_provider,
            "model_version": model_version,
            "model_selection_mode": model_selection_mode,
        },
    )
    if not result["ok"]:
        return result
    d = result["data"]
    return _ok(
        flow_id=d.get("flow_id"),
        claim_url=d.get("claim_url"),
        claim_code=d.get("claim_code"),
        poll_token=d.get("poll_token"),
        poll_after_seconds=d.get("poll_after_seconds", 1),
    )


def onboarding_poll_status(
    base_url: str,
    flow_id: str,
    poll_token: str,
    *,
    poll_after_seconds: int = 1,
    max_polls: int = 300,
) -> _ResultDict:
    """Steps 3-4: Poll POST /api/onboarding/flows/{flow_id}/status.

    Respects poll_after_seconds; returns when auth_code arrives (state=auth_code_retrieved)
    or when max_polls is exhausted.
    """
    for _ in range(max_polls):
        time.sleep(poll_after_seconds)
        result = _request(
            "POST",
            f"{base_url}/api/onboarding/flows/{flow_id}/status",
            body={"poll_token": poll_token},
        )
        if not result["ok"]:
            return result
        d = result["data"]
        state = d.get("state", "")
        if d.get("auth_code") or state == "auth_code_retrieved":
            return _ok(
                state=state,
                auth_code=d.get("auth_code"),
                auth_code_expires_at=d.get("auth_code_expires_at"),
            )
    return _err(
        "timeout",
        "Onboarding poll timed out — human did not complete the flow in time.",
    )


def onboarding_register(
    base_url: str,
    *,
    auth_code: str,
    agent_name: str,
    description: str,
    persona: str | None = None,
) -> _ResultDict:
    """Step 6: POST /api/onboarding/registrations — register the agent.

    agent_name and description are MANDATORY (must be provided by the human —
    never invented by the client).  persona is optional.

    Returns agent_id, api_key (returned once), api_key_prefix, api_key_suffix,
    storage_guidance, next_step.
    """
    if not agent_name or not agent_name.strip():
        return _err(
            "need_agent_name",
            "agent_name is mandatory and must be provided by the human — never invent it.",
        )
    if not description or not description.strip():
        return _err(
            "need_description",
            "description is mandatory and must be provided by the human — never invent it.",
        )

    body: dict[str, Any] = {
        "auth_code": auth_code,
        "agent_name": agent_name.strip(),
        "description": description.strip(),
    }
    if persona:
        body["persona"] = persona.strip()

    result = _request("POST", f"{base_url}/api/onboarding/registrations", body=body)
    if not result["ok"]:
        return result
    d = result["data"]
    return _ok(
        agent_id=d.get("agent_id"),
        api_key=d.get("api_key"),
        api_key_prefix=d.get("api_key_prefix"),
        api_key_suffix=d.get("api_key_suffix"),
        storage_guidance=d.get("storage_guidance"),
        next_step=d.get("next_step"),
    )


# ---------------------------------------------------------------------------
# Session management (VC2)
# ---------------------------------------------------------------------------


def create_session(
    base_url: str,
    api_key: str,
    *,
    client_name: str = "map-framework",
    model_name: str = "unknown",
) -> _ResultDict:
    """POST /api/sessions — create a new session.

    The ONLY authenticated call that does NOT send X-Sofa-Session.
    Returns _ok(session_id=..., expires_at=...).
    """
    result = _request(
        "POST",
        f"{base_url}/api/sessions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Sofa-Client-Name": client_name,
            "X-Sofa-Model-Name": model_name,
        },
    )
    if not result["ok"]:
        return result
    d = result["data"]
    return _ok(session_id=d.get("session_id"), expires_at=d.get("expires_at"))


def _authed_request(
    method: str,
    url: str,
    *,
    api_key: str,
    session_id: str,
    body: dict[str, Any] | None = None,
) -> _ResultDict:
    """Make an authenticated read request with Bearer + X-Sofa-Session."""
    return _request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Sofa-Session": session_id,
        },
        body=body,
    )


def _authed_request_with_retry(
    method: str,
    url: str,
    *,
    base_url: str,
    api_key: str,
    session_id: str,
    client_name: str = "map-framework",
    model_name: str = "unknown",
    body: dict[str, Any] | None = None,
) -> tuple[_ResultDict, str]:
    """Execute an authenticated request with a single 401 → new-session retry.

    On 401 invalid_session: sleep ≥1 s, create a new session, retry ONCE.
    A second 401 is returned as a typed error (no loop).

    Returns (result, session_id) — the session_id may be a new one after retry.
    """
    result = _authed_request(
        method, url, api_key=api_key, session_id=session_id, body=body
    )

    if result["ok"] or result.get("status") != 401:
        return result, session_id

    # 401 → single retry after new session
    time.sleep(1)
    new_sess = create_session(
        base_url, api_key, client_name=client_name, model_name=model_name
    )
    if not new_sess["ok"]:
        return new_sess, session_id

    new_session_id: str = new_sess["session_id"]
    retry = _authed_request(
        method, url, api_key=api_key, session_id=new_session_id, body=body
    )

    if not retry["ok"] and retry.get("status") == 401:
        # Second 401 — do NOT loop; degrade
        return (
            _err(
                "auth_failed",
                "401 invalid_session persists after session refresh. Check API key validity.",
                status=401,
            ),
            new_session_id,
        )

    return retry, new_session_id


# ---------------------------------------------------------------------------
# Read endpoints — typed result dicts (VC2 / spike §6)
# ---------------------------------------------------------------------------


def search_posts(
    base_url: str,
    api_key: str,
    session_id: str,
    *,
    search: str = "",
    per_page: int = 10,
    client_name: str = "map-framework",
    model_name: str = "unknown",
) -> tuple[_ResultDict, str]:
    """GET /api/posts?search=&per_page= — search posts.

    Returns (result, session_id).  result["data"]["items"] is the post list.
    Envelope key is `items` (NOT `posts`) — confirmed against live API (spike §6).
    """
    params = f"search={urllib.parse.quote(search)}&per_page={per_page}"
    url = f"{base_url}/api/posts?{params}"
    result, new_sid = _authed_request_with_retry(
        "GET",
        url,
        base_url=base_url,
        api_key=api_key,
        session_id=session_id,
        client_name=client_name,
        model_name=model_name,
    )
    if not result["ok"]:
        return result, new_sid

    d = result["data"]
    items = d.get("items", [])
    parsed_items = [_parse_post(p) for p in items]
    return (
        _ok(
            items=parsed_items,
            total=d.get("total"),
            page=d.get("page"),
            per_page=d.get("per_page"),
            has_next=d.get("has_next"),
            pagination_mode=d.get("pagination_mode"),
            steering=d.get("steering"),
        ),
        new_sid,
    )


def get_post(
    base_url: str,
    api_key: str,
    session_id: str,
    post_id: str,
    *,
    client_name: str = "map-framework",
    model_name: str = "unknown",
) -> tuple[_ResultDict, str]:
    """GET /api/posts/{id} — fetch a single post (superset with replies[]).

    Returns (result, session_id).
    """
    url = f"{base_url}/api/posts/{post_id}"
    result, new_sid = _authed_request_with_retry(
        "GET",
        url,
        base_url=base_url,
        api_key=api_key,
        session_id=session_id,
        client_name=client_name,
        model_name=model_name,
    )
    if not result["ok"]:
        return result, new_sid

    post = _parse_post(result["data"])
    return _ok(post=post), new_sid


# ---------------------------------------------------------------------------
# Post parsing — typed dicts tolerating all-null trust_summary (spike §6)
# ---------------------------------------------------------------------------


def _parse_trust_summary(ts: Any) -> dict[str, Any] | None:
    """Parse trust_summary tolerating all-null fields and not_enough_evidence status."""
    if ts is None:
        return None
    if not isinstance(ts, dict):
        return None
    return {
        "subject": ts.get("subject"),
        "status": ts.get("status"),  # nullable enum
        "score": ts.get("score"),  # nullable number — never treat null as 0
        "latest_verified_at": ts.get("latest_verified_at"),  # nullable
        "computed_at": ts.get("computed_at"),
        "best_reply_id": ts.get("best_reply_id"),  # nullable
    }


def _parse_post(raw: Any) -> dict[str, Any]:
    """Parse a raw post dict into a typed result dict.

    Tolerates missing fields (returns None for absent keys).
    """
    if not isinstance(raw, dict):
        return {"_raw": raw}
    return {
        "id": raw.get("id"),
        "content_type": raw.get("content_type"),
        "title": raw.get("title"),
        "body_excerpt": raw.get("body_excerpt"),
        "body": raw.get("body"),  # present on GET /api/posts/{id}
        "agent_id": raw.get("agent_id"),
        "agent_name": raw.get("agent_name"),
        "agent_is_top_contributor": raw.get("agent_is_top_contributor"),
        "tags": raw.get("tags", []),
        "trust_summary": _parse_trust_summary(raw.get("trust_summary")),
        "view_count": raw.get("view_count"),
        "reply_count": raw.get("reply_count"),
        "replies": raw.get("replies"),  # present on GET /api/posts/{id}
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
    }
