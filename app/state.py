from __future__ import annotations

import copy
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}

# Rolling per-HSM event log - both poller threads and the setup flow append to this
# as things happen, rather than a caller batching everything up and only reporting it
# at the very end (which loses all progress if something fails partway through).
LOG_MAX_ENTRIES = 200


def seed(name: str) -> None:
    """Give an HSM a placeholder entry before its first poll completes, so routes
    never 404/crash on a name that's configured but hasn't reported in yet.

    "role_problems"/"client_problems" are separate keys, not one shared "problems"
    list - the roles poller and clients poller are independent threads that each
    call update() with only the fields they own, and dict.update() only touches
    the keys given to it. Sharing one key would let one poller's update silently
    erase the other's findings. Combined into one "problems" view at read time."""
    with _LOCK:
        _CACHE.setdefault(
            name,
            {
                "name": name,
                "id": None,
                "raw": None,
                "partitions": [],
                "role_problems": [],
                "thread_status": "starting",
                "last_checked": None,
                "partition_clients": {},
                "client_problems": [],
                "clients_thread_status": "starting",
                "clients_last_checked": None,
                "log": [],
            },
        )


def update(name: str, fields: dict[str, Any]) -> None:
    with _LOCK:
        entry = _CACHE.setdefault(name, {"name": name})
        entry.update(fields)


def log_event(name: str, message: str) -> None:
    """Append one timestamped entry to this HSM's rolling log. Unlike update() (a
    whole-field merge), this does its own read-modify-write under the lock, so
    concurrent appends from different threads (pollers, the setup flow) never lose
    an entry to a race the way two racing `entry["log"] = entry["log"] + [...]`
    reads-then-writes could."""
    with _LOCK:
        entry = _CACHE.setdefault(name, {"name": name})
        log = entry.setdefault("log", [])
        log.append({"timestamp": time.time(), "message": message})
        del log[:-LOG_MAX_ENTRIES]


def get(name: str) -> dict[str, Any] | None:
    # Deep copy, not dict(entry): callers (routes, templates) must never be able to
    # mutate the nested "problems"/"partitions" lists a poller thread is concurrently
    # about to replace.
    with _LOCK:
        entry = _CACHE.get(name)
        return copy.deepcopy(entry) if entry is not None else None


def get_all() -> list[dict[str, Any]]:
    with _LOCK:
        return [copy.deepcopy(entry) for entry in _CACHE.values()]


def _reset_for_tests() -> None:
    """Test-only: clear the module-level cache between tests."""
    with _LOCK:
        _CACHE.clear()
