from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from app import state
from app.monitor import FatalHsmError, role_expectations, poll_once

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60


class Poller:
    """Runs poll_once() for one HSM on a timer, in its own thread. Stops itself on a
    fatal (connection/auth) failure rather than retrying forever - resume manually via
    start()/check_now(). check_now() also doubles as "resume a stopped poller"."""

    def __init__(
        self,
        entry: dict[str, Any],
        global_config: dict[str, Any],
        role_expectations: dict[str, dict[str, Any]],
        username: str,
        password: str,
        interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.name = entry["name"]
        self.entry = entry
        self.global_config = global_config
        self.role_expectations = role_expectations
        self.username = username
        self.password = password
        self.interval = interval

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_alive():
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._run, name=f"poller-{self.name}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()  # wake it immediately instead of waiting out the interval

    def check_now(self) -> None:
        """Poll immediately. Resumes the poller first if it had fatally stopped."""
        if not self.is_alive():
            self.start()
            return
        self._wake.set()

    def _run(self) -> None:
        state.update(self.name, {"thread_status": "running"})

        while not self._stop.is_set():
            try:
                result = poll_once(
                    self.entry,
                    self.global_config,
                    self.role_expectations,
                    self.username,
                    self.password,
                )
                result["thread_status"] = "running"
                result["last_checked"] = time.time()
                state.update(self.name, result)
            except FatalHsmError as exc:
                logger.warning("%s: fatal, stopping poller: %s", self.name, exc)
                state.update(
                    self.name,
                    {
                        "problems": [
                            {
                                "severity": "fatal",
                                "kind": "connection_failed",
                                "message": "could not connect to the hsm - monitoring thread stopped",
                                "hsm": self.name,
                            }
                        ],
                        "thread_status": "stopped",
                        "last_checked": time.time(),
                    },
                )
                return

            self._wake.wait(self.interval)
            self._wake.clear()

        state.update(self.name, {"thread_status": "stopped"})


POLLERS: dict[str, Poller] = {}


def init_pollers(config: dict[str, Any]) -> None:
    """Build one Poller per configured HSM and start it. Safe to call once at app
    startup; calling again is a no-op for HSMs that already have a poller."""
    username = os.environ["LUNA_USERNAME"]
    password = os.environ["LUNA_PASSWORD"]
    global_config = config.get("global", {})
    expectations = role_expectations(config)

    for entry in config.get("hsms", []):
        name = entry["name"]
        state.seed(name)
        if name in POLLERS:
            continue
        poller = Poller(entry, global_config, expectations, username, password)
        POLLERS[name] = poller
        poller.start()
