from __future__ import annotations

import contextlib
import errno
import importlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

if os.name == "nt":
    msvcrt: Any = importlib.import_module("msvcrt")
else:
    import fcntl


STATE_SCHEMA_VERSION = 1
STATE_RELATIVE_PATH = Path(".map/update-state.json")
UPDATE_INTERVAL = timedelta(hours=24)


class UpdateLockBusy(RuntimeError):
    """Raised when another process holds a project's update lock."""


class UpdateLockSecurityError(RuntimeError):
    """Raised when the project lock path cannot be opened safely."""


@dataclass(frozen=True)
class UpdateState:
    schema_version: int = STATE_SCHEMA_VERSION
    last_attempt_at: str | None = None
    last_observed_version: str | None = None
    last_installed_version: str | None = None
    pending_refresh: bool = False
    pending_providers: tuple[str, ...] = ()


def _state_from_payload(payload: Any) -> UpdateState | None:
    if not isinstance(payload, dict):
        return None

    allowed_keys = {
        "schema_version",
        "last_attempt_at",
        "last_observed_version",
        "last_installed_version",
        "pending_refresh",
        "pending_providers",
    }
    if set(payload) != allowed_keys:
        return None
    if type(payload.get("schema_version")) is not int:
        return None
    if payload["schema_version"] != STATE_SCHEMA_VERSION:
        return None

    for field_name in (
        "last_attempt_at",
        "last_observed_version",
        "last_installed_version",
    ):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, str):
            return None

    pending_refresh = payload.get("pending_refresh", False)
    if type(pending_refresh) is not bool:
        return None

    pending_providers = payload.get("pending_providers", [])
    if not isinstance(pending_providers, list) or not all(
        isinstance(provider, str) for provider in pending_providers
    ):
        return None

    return UpdateState(
        schema_version=payload["schema_version"],
        last_attempt_at=payload.get("last_attempt_at"),
        last_observed_version=payload.get("last_observed_version"),
        last_installed_version=payload.get("last_installed_version"),
        pending_refresh=pending_refresh,
        pending_providers=tuple(pending_providers),
    )


def read_update_state(project_path: Path) -> UpdateState:
    state_path = project_path / STATE_RELATIVE_PATH
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return UpdateState()
    return _state_from_payload(payload) or UpdateState()


def write_update_state(project_path: Path, state: UpdateState) -> None:
    state_path = project_path / STATE_RELATIVE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=state_path.parent,
        prefix=f".{state_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(
                {
                    "schema_version": state.schema_version,
                    "last_attempt_at": state.last_attempt_at,
                    "last_observed_version": state.last_observed_version,
                    "last_installed_version": state.last_installed_version,
                    "pending_refresh": state.pending_refresh,
                    "pending_providers": list(state.pending_providers),
                },
                temp_file,
                sort_keys=True,
            )
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, state_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def pending_refresh_state(
    project_path: Path,
    provider: str,
) -> UpdateState | None:
    """Return state only when it authorizes recovery for ``provider``."""
    state = read_update_state(project_path)
    if (
        state.pending_refresh
        and bool(state.last_installed_version)
        and provider in state.pending_providers
    ):
        return state
    return None


def complete_pending_provider_refresh(
    project_path: Path,
    provider: str,
) -> UpdateState:
    """Atomically remove one successfully refreshed provider from pending state.

    This transition deliberately does not acquire the project update lock: updater
    child processes run while their parent owns that lock.
    """
    state = pending_refresh_state(project_path, provider)
    if state is None:
        raise RuntimeError(
            f"Pending MAP refresh state no longer includes provider '{provider}'."
        )
    remaining = tuple(
        pending_provider
        for pending_provider in state.pending_providers
        if pending_provider != provider
    )
    completed = replace(
        state,
        pending_refresh=bool(remaining),
        pending_providers=remaining,
    )
    write_update_state(project_path, completed)
    return completed


def automatic_check_due(state: UpdateState, now: datetime) -> bool:
    previous_raw = state.last_attempt_at
    if previous_raw is None or not previous_raw.endswith("Z"):
        return True
    try:
        previous = datetime.fromisoformat(f"{previous_raw[:-1]}+00:00")
        elapsed = now - previous
    except (TypeError, ValueError):
        return True
    return elapsed >= UPDATE_INTERVAL


if os.name == "nt":

    def _try_lock(fd: int) -> None:
        # Intent: Windows byte-range locks need a persistent byte to lock.
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(exc.errno, str(exc)) from exc
            raise

    def _unlock(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            return

else:

    def _try_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise BlockingIOError(exc.errno, str(exc)) from exc
            raise

    def _unlock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            return


@contextlib.contextmanager
def project_update_lock(
    project_path: Path,
    *,
    timeout_s: float,
) -> Generator[None, None, None]:
    lock_path = project_path / ".map" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow == 0 and lock_path.is_symlink():
        raise UpdateLockSecurityError(str(lock_path))
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UpdateLockSecurityError(str(lock_path)) from exc
        raise

    acquired = False
    try:
        if nofollow == 0:
            # Intent: Match path and descriptor identities to detect path swaps.
            path_stat = os.lstat(lock_path)
            fd_stat = os.fstat(fd)
            if stat.S_ISLNK(path_stat.st_mode) or (
                path_stat.st_dev,
                path_stat.st_ino,
            ) != (fd_stat.st_dev, fd_stat.st_ino):
                raise UpdateLockSecurityError(str(lock_path))
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)

        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _try_lock(fd)
                acquired = True
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise UpdateLockBusy(str(lock_path)) from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        yield
    finally:
        try:
            if acquired:
                _unlock(fd)
        finally:
            os.close(fd)
