from __future__ import annotations

import json
import os
import queue
import stat
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

import pytest

from mapify_cli.update_state import (
    UpdateLockBusy,
    UpdateLockSecurityError,
    UpdateState,
    automatic_check_due,
    project_update_lock,
    read_update_state,
    write_update_state,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)

LOCK_HOLDER = """
import sys
from pathlib import Path

from mapify_cli.update_state import project_update_lock

with project_update_lock(Path(sys.argv[1]), timeout_s=0.0):
    print("READY", flush=True)
    input()
"""


def test_state_round_trip_is_project_local(tmp_path: Path) -> None:
    state = UpdateState(
        last_attempt_at="2026-08-13T11:00:00Z",
        last_installed_version="3.25.1",
        pending_providers=("codex",),
    )

    write_update_state(tmp_path, state)

    assert read_update_state(tmp_path) == state
    assert (tmp_path / ".map" / "update-state.json").is_file()


def test_state_write_replaces_the_previous_document(tmp_path: Path) -> None:
    write_update_state(tmp_path, UpdateState(last_observed_version="3.25.1"))

    write_update_state(
        tmp_path,
        UpdateState(last_observed_version="3.26.0", pending_refresh=True),
    )

    payload = json.loads(
        (tmp_path / ".map" / "update-state.json").read_text(encoding="utf-8")
    )
    assert payload == {
        "last_attempt_at": None,
        "last_installed_version": None,
        "last_observed_version": "3.26.0",
        "pending_providers": [],
        "pending_refresh": True,
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"schema_version": 2}',
        '{"schema_version": true}',
        '{"schema_version": 1, "last_attempt_at": 123}',
        '{"schema_version": 1, "last_observed_version": []}',
        '{"schema_version": 1, "last_installed_version": false}',
        '{"schema_version": 1, "pending_refresh": 1}',
        '{"schema_version": 1, "pending_providers": ["codex", 2]}',
    ],
)
def test_corrupt_state_becomes_default_cache_miss(
    tmp_path: Path,
    payload: str,
) -> None:
    path = tmp_path / ".map" / "update-state.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    assert read_update_state(tmp_path) == UpdateState()


def test_missing_state_is_a_default_cache_miss(tmp_path: Path) -> None:
    assert read_update_state(tmp_path) == UpdateState()


@pytest.mark.parametrize(
    ("last_attempt_at", "expected"),
    [
        (None, True),
        ("invalid", True),
        ("2026-08-12T11:59:59Z", True),
        ("2026-08-12T12:00:00Z", True),
        ("2026-08-12T12:00:01Z", False),
    ],
)
def test_automatic_check_due_uses_rolling_24_hours(
    last_attempt_at: str | None,
    expected: bool,
) -> None:
    state = UpdateState(last_attempt_at=last_attempt_at)

    assert automatic_check_due(state, NOW) is expected


def test_second_project_lock_is_busy(tmp_path: Path) -> None:
    with (
        project_update_lock(tmp_path, timeout_s=0.0),
        pytest.raises(UpdateLockBusy),
        project_update_lock(tmp_path, timeout_s=0.0),
    ):
        raise AssertionError("contender must not acquire")


def test_lock_refuses_symlink(tmp_path: Path) -> None:
    map_dir = tmp_path / ".map"
    map_dir.mkdir()
    (map_dir / "target").touch()
    (map_dir / "update.lock").symlink_to(map_dir / "target")

    with (
        pytest.raises(UpdateLockSecurityError),
        project_update_lock(tmp_path, timeout_s=0.0),
    ):
        raise AssertionError("symlink lock must not open")


def test_lock_file_persists_with_owner_only_permissions(tmp_path: Path) -> None:
    lock_path = tmp_path / ".map" / "update.lock"

    with project_update_lock(tmp_path, timeout_s=0.0):
        assert lock_path.is_file()

    assert lock_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def _readline_with_timeout(stream: IO[Any], timeout_s: float) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)

    def read_line() -> None:
        result.put(cast(str, stream.readline()))

    reader = threading.Thread(target=read_line, daemon=True)
    reader.start()
    try:
        return result.get(timeout=timeout_s)
    except queue.Empty as exc:
        raise TimeoutError("lock holder did not become ready") from exc


@pytest.mark.timeout(10)
def test_separate_process_lock_contention_is_busy(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert _readline_with_timeout(process.stdout, 5.0) == "READY\n"

        with (
            pytest.raises(UpdateLockBusy),
            project_update_lock(tmp_path, timeout_s=0.0),
        ):
            raise AssertionError("parent must not acquire the child lock")

        assert process.stdin is not None
        process.stdin.write("\n")
        process.stdin.flush()
        assert process.wait(timeout=5.0) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)

    assert process.stderr is not None
    assert process.stderr.read() == ""
